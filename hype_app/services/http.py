"""Reliable HTTP client for USGS/NRCS (revision spec §5.6).

One `ServiceClient` per service. Guarantees:
* Bounded process-wide concurrency (a shared semaphore) so a burst of lookups can't hammer USGS.
* Separate connect and read timeouts.
* Retries ONLY on connect/read timeouts, connection errors, HTTP 429, and transient 5xx —
  never on other 4xx. Honors Retry-After; bounded exponential backoff with jitter.
* Payload-shape validation even on HTTP 200 (a 200 with the wrong body is a `PayloadError`).
* Cancellation between attempts via a caller-supplied predicate (the app's Cancel button).
* An immutable JSON snapshot cache keyed by (method, url, params, service_version).

`sleep`/`rand`/`transport` are injectable so the whole retry/backoff/cancel machinery is unit
tested offline with `httpx.MockTransport` and no real waiting.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

# One shared limiter across every ServiceClient in the process (§5.6 "limit process-wide
# concurrent USGS requests"). Sized small — these are heavyweight geoprocessing calls.
_GLOBAL_SEMAPHORE = threading.BoundedSemaphore(4)


class ServiceError(Exception):
    """Base class; str(err) is safe to surface to the user."""


class ServiceTimeout(ServiceError):
    pass


class ServiceCancelled(ServiceError):
    pass


class RateLimited(ServiceError):
    pass


class PayloadError(ServiceError):
    """HTTP 200 (or other 2xx) but the body failed shape validation."""


class ServiceHTTPError(ServiceError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    backoff_base: float = 0.5      # seconds
    backoff_max: float = 8.0
    jitter: float = 0.25           # +/- fraction of the computed delay

    def delay(self, attempt: int, rand: Callable[[], float]) -> float:
        """Delay before retry `attempt` (1-based): exp backoff, capped, jittered."""
        base = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        return max(0.0, base * (1.0 + self.jitter * (rand() * 2.0 - 1.0)))


_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_EXC = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
                  httpx.PoolTimeout, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)


class ServiceClient:
    def __init__(self, *, base_url: str = "", connect_timeout: float = 10.0,
                 read_timeout: float = 60.0, retry: RetryPolicy | None = None,
                 user_agent: str = "HYPE/2026.07 (hyporheic-explorer)",
                 transport: httpx.BaseTransport | None = None,
                 cache_dir: str | Path | None = None,
                 semaphore: threading.BoundedSemaphore | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 rand: Callable[[], float] = random.random) -> None:
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._rand = rand
        self._sem = semaphore or _GLOBAL_SEMAPHORE
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ServiceClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- cache -------------------------------------------------------------
    def _cache_path(self, cache_key: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json"

    def _cache_read(self, cache_key: str | None):
        if not cache_key:
            return None
        p = self._cache_path(cache_key)
        if p and p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return None

    def _cache_write(self, cache_key: str | None, data: Any) -> None:
        if not cache_key:
            return
        p = self._cache_path(cache_key)
        if p:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                p.write_text(json.dumps(data), encoding="utf-8")
            except (OSError, TypeError):
                pass

    # ---- request -----------------------------------------------------------
    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        val = resp.headers.get("Retry-After")
        if not val:
            return None
        try:
            return float(val)                      # delta-seconds form only (date form ignored)
        except ValueError:
            return None

    def _check_cancel(self, cancel: Callable[[], bool] | None) -> None:
        if cancel and cancel():
            raise ServiceCancelled("Request cancelled.")

    def get_json(self, url: str, *, params: dict | None = None,
                 validate: Callable[[Any], None] | None = None,
                 cancel: Callable[[], bool] | None = None,
                 cache_key: str | None = None,
                 service_version: str = "") -> Any:
        """GET JSON with retries/backoff/cancel/validation/cache. Returns parsed JSON."""
        key = None
        if cache_key is not None:
            key = f"GET|{url}|{json.dumps(params, sort_keys=True)}|{service_version}|{cache_key}"
            cached = self._cache_read(key)
            if cached is not None:
                return cached

        data = self._request_with_retries("GET", url, params=params, cancel=cancel)
        if validate is not None:
            validate(data)                          # raises PayloadError on bad shape
        self._cache_write(key, data)
        return data

    def post_json(self, url: str, *, data: Any = None, params: dict | None = None,
                  headers: dict | None = None, validate: Callable[[Any], None] | None = None,
                  cancel: Callable[[], bool] | None = None) -> Any:
        out = self._request_with_retries("POST", url, params=params, json_body=data,
                                         headers=headers, cancel=cancel)
        if validate is not None:
            validate(out)
        return out

    def _request_with_retries(self, method: str, url: str, *, params=None, json_body=None,
                              headers=None, cancel=None) -> Any:
        attempt = 0
        last_exc: Exception | None = None
        while attempt < self.retry.max_attempts:
            attempt += 1
            self._check_cancel(cancel)
            try:
                with self._sem:
                    self._check_cancel(cancel)
                    resp = self._client.request(method, url, params=params, json=json_body,
                                                headers=headers)
            except _TRANSIENT_EXC as e:
                last_exc = ServiceTimeout(f"{type(e).__name__} contacting {url}") \
                    if isinstance(e, (httpx.TimeoutException,)) else \
                    ServiceError(f"{type(e).__name__} contacting {url}")
                if attempt >= self.retry.max_attempts:
                    break
                self._backoff(attempt, None, cancel)
                continue
            except httpx.HTTPError as e:            # non-transient network error -> no retry
                raise ServiceError(f"{type(e).__name__} contacting {url}") from e

            if resp.status_code in _RETRY_STATUSES:
                ra = self._retry_after(resp)
                if resp.status_code == 429:
                    last_exc = RateLimited(f"Rate limited by {url} (HTTP 429).")
                else:
                    last_exc = ServiceHTTPError(resp.status_code,
                                                f"Server error {resp.status_code} from {url}.")
                if attempt >= self.retry.max_attempts:
                    break
                self._backoff(attempt, ra, cancel)
                continue

            if resp.status_code >= 400:
                raise ServiceHTTPError(resp.status_code,
                                       f"HTTP {resp.status_code} from {url}: "
                                       f"{resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as e:
                raise PayloadError(f"Non-JSON response from {url}.") from e

        assert last_exc is not None
        raise last_exc

    def _backoff(self, attempt: int, retry_after: float | None,
                 cancel: Callable[[], bool] | None) -> None:
        delay = self.retry.delay(attempt, self._rand)
        if retry_after is not None:
            delay = max(delay, retry_after)
        self._check_cancel(cancel)
        self._sleep(delay)
        self._check_cancel(cancel)


__all__ = [
    "ServiceClient", "RetryPolicy", "ServiceError", "ServiceTimeout", "ServiceCancelled",
    "RateLimited", "PayloadError", "ServiceHTTPError",
]
