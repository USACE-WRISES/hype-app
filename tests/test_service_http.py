"""Reliability tests for services/http.ServiceClient — offline via httpx.MockTransport.

Covers the §5.6 guarantees: retry only on 429/5xx/transient timeouts, Retry-After, bounded
backoff, no retry on other 4xx, payload validation, cancellation, and the snapshot cache.
"""
import threading

import httpx
import pytest

from hype_app.services.http import (
    PayloadError,
    RateLimited,
    RetryPolicy,
    ServiceCancelled,
    ServiceClient,
    ServiceHTTPError,
    ServiceTimeout,
)


def _raises(exc_factory):
    def fn(request):
        raise exc_factory(request)
    return fn


def _make_client(steps, *, sleep=None, rand=None, retry=None, **kw):
    calls = {"n": 0}

    def handler(request):
        i = calls["n"]
        calls["n"] += 1
        return steps[min(i, len(steps) - 1)](request)

    client = ServiceClient(
        base_url="https://svc.test",
        transport=httpx.MockTransport(handler),
        sleep=sleep or (lambda d: None),
        rand=rand or (lambda: 0.5),
        retry=retry or RetryPolicy(max_attempts=4, backoff_base=0.001, backoff_max=0.01),
        semaphore=threading.BoundedSemaphore(4),
        **kw)
    return client, calls


_OK = lambda r: httpx.Response(200, json={"ok": True})           # noqa: E731
_503 = lambda r: httpx.Response(503)                             # noqa: E731
_500 = lambda r: httpx.Response(500)                            # noqa: E731
_404 = lambda r: httpx.Response(404, text="nope")               # noqa: E731


def test_success_returns_json():
    client, calls = _make_client([_OK])
    assert client.get_json("/x") == {"ok": True}
    assert calls["n"] == 1


def test_retry_on_503_then_success():
    client, calls = _make_client([_503, _503, _OK])
    assert client.get_json("/x") == {"ok": True}
    assert calls["n"] == 3


def test_exhausts_retries_on_persistent_500():
    client, calls = _make_client([_500], retry=RetryPolicy(max_attempts=3, backoff_base=0.001))
    with pytest.raises(ServiceHTTPError) as ei:
        client.get_json("/x")
    assert ei.value.status == 500
    assert calls["n"] == 3


def test_no_retry_on_404():
    client, calls = _make_client([_404])
    with pytest.raises(ServiceHTTPError) as ei:
        client.get_json("/x")
    assert ei.value.status == 404
    assert calls["n"] == 1


def test_rate_limit_honors_retry_after():
    slept = []
    steps = [lambda r: httpx.Response(429, headers={"Retry-After": "2"}), _OK]
    client, _ = _make_client(steps, sleep=lambda d: slept.append(d))
    assert client.get_json("/x") == {"ok": True}
    assert max(slept) >= 2.0            # Retry-After floor honored


def test_persistent_429_raises_rate_limited():
    client, _ = _make_client([lambda r: httpx.Response(429)],
                             retry=RetryPolicy(max_attempts=2, backoff_base=0.001))
    with pytest.raises(RateLimited):
        client.get_json("/x")


def test_transient_timeout_then_success():
    steps = [_raises(lambda r: httpx.ReadTimeout("boom", request=r)), _OK]
    client, calls = _make_client(steps)
    assert client.get_json("/x") == {"ok": True}
    assert calls["n"] == 2


def test_persistent_timeout_raises_service_timeout():
    client, _ = _make_client([_raises(lambda r: httpx.ConnectTimeout("boom", request=r))],
                             retry=RetryPolicy(max_attempts=2, backoff_base=0.001))
    with pytest.raises(ServiceTimeout):
        client.get_json("/x")


def test_payload_validation_failure():
    def validate(data):
        if "watershed" not in data:
            raise PayloadError("missing watershed")
    client, _ = _make_client([_OK])
    with pytest.raises(PayloadError):
        client.get_json("/x", validate=validate)


def test_non_json_200_is_payload_error():
    client, _ = _make_client([lambda r: httpx.Response(200, text="<html>not json</html>")])
    with pytest.raises(PayloadError):
        client.get_json("/x")


def test_cancellation_before_request():
    client, calls = _make_client([_OK])
    with pytest.raises(ServiceCancelled):
        client.get_json("/x", cancel=lambda: True)
    assert calls["n"] == 0


def test_cache_hit_skips_transport(tmp_path):
    client, calls = _make_client([_OK], cache_dir=tmp_path)
    a = client.get_json("/x", cache_key="k1", service_version="v1")
    b = client.get_json("/x", cache_key="k1", service_version="v1")
    assert a == b == {"ok": True}
    assert calls["n"] == 1              # second call served from cache
