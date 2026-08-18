"""Example projects: the curated catalog behind the start page's Example projects view, and the
downloader that fetches one into the per-user cache.

An example is a complete standalone `.hype` bundle (results included) published as a GitHub
release asset on the app repo; the catalog (`hype_app/data/examples.json`) and the tile
thumbnails (`www/examples/<id>.jpg`) ship inside the app, so an app build can only ever list
examples it can open. Opening a downloaded example is the existing import path (desktop:
`_import_bundle_to`, cloud: `_apply_project`), so nothing here knows about the workspace.

Design points, all pinned by tests/test_examples.py:
* Catalog validation is strict at load time (unique ids, allow-listed URL prefix, 64-hex sha,
  positive size, thumbnail present, openable format version); a bad entry fails loud in tests
  and is skipped in the app rather than crashing the start page.
* `fetch` streams to `<id>-<sha8>.hype.part` with HTTP Range resume, hashes incrementally, and
  only renames into place when size AND sha256 match. Cancel keeps the `.part` for resume; a
  hash or size mismatch deletes it. HTTP 404 is its own error (`ExampleGone`): the asset was
  removed after this app build shipped its catalog.
* Everything network-facing goes through an injectable httpx transport so the whole flow is
  unit tested offline with `httpx.MockTransport` (the services/http.py convention).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from . import bundle, recents
from .services.http import RetryPolicy

#: Only assets under this prefix are ever fetched (the catalog is data shipped in the payload,
#: but a typo'd or tampered URL must still not point the app at an arbitrary host).
EXAMPLES_URL_PREFIX = "https://github.com/USACE-WRISES/hype-app/releases/download/"
#: Local/E2E override: when set, every asset URL is rewritten to <base>/<basename>. Mirrors
#: HYPE_MANIFEST_URL for the desktop shell.
BASE_URL_ENV = "HYPE_EXAMPLES_BASE_URL"
CATALOG_SCHEMA = 1
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CHUNK = 256 * 1024
_TRANSIENT = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
              httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)


class ExampleError(Exception):
    """Base class; str(err) is safe to show to the user."""


class ExampleGone(ExampleError):
    """HTTP 404/410: the asset was removed after this app build shipped its catalog."""

    def __init__(self, ex: "Example"):
        super().__init__(f"{ex.title} is no longer available for download. Update HYPE for "
                         "the current list of example projects.")
        self.example = ex


class ExampleCancelled(ExampleError):
    """Cancelled by the user; the partial file is kept for a later resume."""


class ExampleCorrupt(ExampleError):
    """Size or checksum mismatch after a complete download; the partial file was deleted."""


@dataclass(frozen=True)
class Example:
    id: str
    title: str
    description: str
    tags: tuple[str, ...]
    size_bytes: int
    sha256: str
    url: str
    thumbnail: str                 # path under www/, e.g. "examples/SS01208.jpg"
    published: str = ""            # ISO date
    format_version: int = bundle.FORMAT_VERSION
    min_app_version: str = ""      # hide from apps older than this ("" = any)
    desktop_only: bool = False
    credit: str = ""
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def sha8(self) -> str:
        return self.sha256[:8]

    @property
    def stem(self) -> str:
        """Default project name for the imported copy: the title, made filesystem-safe."""
        s = re.sub(r"[^\w\- ]+", "", self.title, flags=re.UNICODE).strip()
        s = re.sub(r"\s+", " ", s)
        return s or self.id

    @property
    def size_display(self) -> str:
        return human_size(self.size_bytes)


def human_size(n: int | float) -> str:
    """`41 MB`, `1.2 GB`, `640 KB`: one decimal only above the unit boundary where it matters."""
    n = float(max(0, n))
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            v = n / div
            return f"{v:.1f} {unit}" if v < 10 and unit == "GB" else f"{v:.0f} {unit}"
    return f"{int(n)} B"


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def catalog_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "examples.json"


def www_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "www"


def _validate(raw: dict, *, seen: set[str], www: Path) -> Example:
    """One catalog row → Example, or ValueError naming the field."""
    ex_id = str(raw.get("id") or "")
    if not _ID_RE.match(ex_id):
        raise ValueError(f"bad id {ex_id!r}")
    if ex_id in seen:
        raise ValueError(f"duplicate id {ex_id!r}")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError(f"{ex_id}: empty title")
    size = raw.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"{ex_id}: size_bytes must be a positive integer")
    sha = str(raw.get("sha256") or "").lower()
    if not _SHA_RE.match(sha):
        raise ValueError(f"{ex_id}: sha256 must be 64 hex chars")
    url = str(raw.get("url") or "")
    if not url.startswith(EXAMPLES_URL_PREFIX):
        raise ValueError(f"{ex_id}: url must start with {EXAMPLES_URL_PREFIX}")
    thumb = str(raw.get("thumbnail") or "")
    if not thumb or ".." in thumb or thumb.startswith(("/", "\\")):
        raise ValueError(f"{ex_id}: bad thumbnail path")
    if not (www / thumb).is_file():
        raise ValueError(f"{ex_id}: thumbnail {thumb} not found under www/")
    fmt = raw.get("format_version", bundle.FORMAT_VERSION)
    if not isinstance(fmt, int) or fmt > bundle.FORMAT_VERSION:
        raise ValueError(f"{ex_id}: format_version {fmt} is newer than this app can open")
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(f"{ex_id}: tags must be a list of strings")
    known = {"id", "title", "description", "tags", "size_bytes", "sha256", "url", "thumbnail",
             "published", "format_version", "min_app_version", "desktop_only", "credit"}
    return Example(
        id=ex_id, title=title, description=str(raw.get("description") or "").strip(),
        tags=tuple(tags), size_bytes=size, sha256=sha, url=url, thumbnail=thumb,
        published=str(raw.get("published") or ""), format_version=fmt,
        min_app_version=str(raw.get("min_app_version") or ""),
        desktop_only=bool(raw.get("desktop_only", False)), credit=str(raw.get("credit") or ""),
        extra={k: v for k, v in raw.items() if k not in known})


def parse_catalog(text: str, *, www: Path | None = None, strict: bool = True) -> list[Example]:
    """Parse + validate catalog JSON. strict=True raises on the first bad row (tests, tooling);
    strict=False skips bad rows (the app must still open its start page)."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or doc.get("schema") != CATALOG_SCHEMA:
        raise ValueError(f"examples catalog schema must be {CATALOG_SCHEMA}")
    rows = doc.get("examples")
    if not isinstance(rows, list):
        raise ValueError("examples must be a list")
    www = www or www_dir()
    out: list[Example] = []
    seen: set[str] = set()
    for raw in rows:
        try:
            ex = _validate(raw if isinstance(raw, dict) else {}, seen=seen, www=www)
        except ValueError:
            if strict:
                raise
            continue
        seen.add(ex.id)
        out.append(ex)
    return out


def load_catalog(path: Path | None = None, *, app_version: str = "",
                 desktop: bool | None = None, strict: bool = False) -> list[Example]:
    """The examples this app build should list. Missing/unreadable file → []. Entries whose
    min_app_version is newer than app_version are hidden; desktop_only entries are hidden in
    the cloud when `desktop` is False."""
    p = path or catalog_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rows = parse_catalog(text, strict=strict)
    except ValueError:
        if strict:
            raise
        return []
    out = []
    for ex in rows:
        if app_version and ex.min_app_version and _vtuple(ex.min_app_version) > _vtuple(app_version):
            continue
        if desktop is False and ex.desktop_only:
            continue
        out.append(ex)
    return out


# ---- cache -----------------------------------------------------------------------------

_CACHE_DIR: Path | None = None


def set_cache_dir(p: Path | None) -> None:
    """Override the cache location (the cloud app points it at the process temp dir: a
    container's home may be read-only and nothing there should outlive the session)."""
    global _CACHE_DIR
    _CACHE_DIR = Path(p) if p else None


def cache_dir() -> Path:
    """<data root>/examples (HYPE_DATA_ROOT → %LOCALAPPDATA%\\HYPE → ~/.hype), unless
    overridden with set_cache_dir."""
    return _CACHE_DIR or (recents.data_root() / "examples")


def cached_path(ex: Example) -> Path:
    return cache_dir() / f"{ex.id}-{ex.sha8}.hype"


def part_path(ex: Example) -> Path:
    return cached_path(ex).with_name(cached_path(ex).name + ".part")


def is_cached(ex: Example) -> bool:
    """A finished download: the file is there with the right size (the sha was checked when it
    was renamed into place, so size alone is enough to trust it afterwards)."""
    try:
        return cached_path(ex).stat().st_size == ex.size_bytes
    except OSError:
        return False


def remove(ex: Example) -> None:
    """Delete the cached copy and any partial (never the imported project). Non-fatal."""
    for p in (cached_path(ex), part_path(ex)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# ---- download --------------------------------------------------------------------------

def resolve_url(ex: Example) -> str:
    """The URL to fetch: the catalog's, or <HYPE_EXAMPLES_BASE_URL>/<basename> for local E2E."""
    base = os.environ.get(BASE_URL_ENV, "").strip()
    if not base:
        return ex.url
    return base.rstrip("/") + "/" + ex.url.rsplit("/", 1)[-1]


ProgressFn = Callable[[int, int], None]     # (bytes_done, bytes_total)


def fetch(ex: Example, *, progress: ProgressFn | None = None,
          cancel: threading.Event | None = None,
          transport: httpx.BaseTransport | None = None,
          retry: RetryPolicy | None = None,
          sleep: Callable[[float], None] | None = None) -> Path:
    """Download `ex` into the cache and return the finished path.

    Streams to a `.part` sibling, resuming with a Range request when one exists; verifies size
    and sha256 before the atomic rename. Raises ExampleGone (404/410), ExampleCancelled (the
    `.part` is kept), ExampleCorrupt (mismatch; `.part` deleted), or ExampleError for other
    HTTP/network failures after the connect-phase retries.
    """
    if is_cached(ex):
        return cached_path(ex)
    dest = cached_path(ex)
    part = part_path(ex)
    dest.parent.mkdir(parents=True, exist_ok=True)
    policy = retry or RetryPolicy(max_attempts=3, backoff_base=0.5, backoff_max=4.0)
    _sleep = sleep or time.sleep
    url = resolve_url(ex)
    client = httpx.Client(follow_redirects=True,
                          timeout=httpx.Timeout(60.0, connect=10.0),
                          headers={"User-Agent": "HYPE-examples/1"},
                          transport=transport)
    try:
        attempt = 0
        while True:
            attempt += 1
            try:
                _stream_once(client, url, ex, part, progress, cancel)
                break
            except _TRANSIENT as exc:
                if attempt >= policy.max_attempts:
                    raise ExampleError(f"Download failed: {exc.__class__.__name__}. Check the "
                                       "connection and try again.") from exc
                _sleep(policy.delay(attempt, lambda: 0.5))
    finally:
        client.close()
    # ---- verify + rename (never trust a byte count alone)
    try:
        size = part.stat().st_size
    except OSError as exc:
        raise ExampleError("Download failed: the partial file vanished.") from exc
    digest = _sha256_of(part)
    if size != ex.size_bytes or digest != ex.sha256:
        try:
            part.unlink()
        except OSError:
            pass
        raise ExampleCorrupt(f"{ex.title} did not download correctly (checksum mismatch). "
                             "Try the download again.")
    os.replace(part, dest)
    return dest


def _sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stream_once(client: httpx.Client, url: str, ex: Example, part: Path,
                 progress: ProgressFn | None, cancel: threading.Event | None) -> None:
    """One streaming attempt: resume from the current `.part` length if the server honors
    Range (206), else start over (200)."""
    have = 0
    try:
        have = part.stat().st_size
    except OSError:
        have = 0
    if have >= ex.size_bytes and have > 0:
        # A stale, over-long partial (a rebuilt asset under an old name): start over.
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have > 0 else {}
    with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code in (404, 410):
            raise ExampleGone(ex)
        if resp.status_code == 416:            # range not satisfiable: partial is junk
            part.unlink(missing_ok=True)
            raise httpx.ReadError("range not satisfiable")   # transient path → retry clean
        if resp.status_code == 206 and have > 0:
            mode = "ab"
        elif resp.status_code == 200:
            mode = "wb"
            have = 0
        else:
            raise ExampleError(f"Download failed: HTTP {resp.status_code}.")
        total = ex.size_bytes
        if progress:
            progress(have, total)
        with part.open(mode) as fh:
            for chunk in resp.iter_bytes(_CHUNK):
                if cancel is not None and cancel.is_set():
                    raise ExampleCancelled("Download cancelled.")
                fh.write(chunk)
                have += len(chunk)
                if progress:
                    progress(min(have, total), total)


__all__ = ["EXAMPLES_URL_PREFIX", "BASE_URL_ENV", "CATALOG_SCHEMA", "Example", "ExampleError",
           "ExampleGone", "ExampleCancelled", "ExampleCorrupt", "human_size", "catalog_path",
           "parse_catalog", "load_catalog", "set_cache_dir", "cache_dir", "cached_path",
           "part_path", "is_cached", "remove", "resolve_url", "fetch"]
