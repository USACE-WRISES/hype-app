"""hype_app.examples — the Example Projects catalog + downloader behind the start page.

Offline by construction: every network path runs through httpx.MockTransport (the
services/http.py convention), and the cache lives under a per-test HYPE_DATA_ROOT.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import httpx
import pytest

from hype_app import bundle, examples

ROOT = Path(__file__).resolve().parents[1]
GOOD_URL = examples.EXAMPLES_URL_PREFIX + "examples-1/TEST.hype"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPE_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv(examples.BASE_URL_ENV, raising=False)
    examples.set_cache_dir(None)
    return tmp_path


def _payload(n: int = 300_000) -> bytes:
    return bytes(range(256)) * (n // 256) + b"x" * (n % 256)


def _example(tmp_path, data: bytes, **over) -> examples.Example:
    www = tmp_path / "www"
    (www / "examples").mkdir(parents=True, exist_ok=True)
    (www / "examples" / "TEST.jpg").write_bytes(b"\xff\xd8\xff")
    row = {"id": "TEST", "title": "Test River, TEST", "description": "d", "tags": ["a"],
           "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
           "url": GOOD_URL, "thumbnail": "examples/TEST.jpg"}
    row.update(over)
    return examples.parse_catalog(json.dumps({"schema": 1, "examples": [row]}), www=www)[0]


class _Server:
    """A MockTransport that serves one asset with optional Range support, redirects,
    a 404 mode, and a hook to trip cancel mid-stream."""

    def __init__(self, data: bytes, *, ranges: bool = True, redirect: bool = False,
                 gone: bool = False, on_chunk=None):
        self.data, self.ranges, self.redirect, self.gone = data, ranges, redirect, gone
        self.on_chunk = on_chunk
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gone:
            return httpx.Response(404, text="Not Found")
        if self.redirect and request.url.host == "github.com":
            return httpx.Response(302, headers={"Location": "https://objects.example/x.hype"})
        rng = request.headers.get("Range")
        if rng and self.ranges:
            start = int(rng.split("=")[1].rstrip("-"))
            body = self.data[start:]
            status, hdr = 206, {"Content-Range": f"bytes {start}-{len(self.data)-1}/{len(self.data)}"}
        else:
            body, status, hdr = self.data, 200, {}

        def gen():
            for i in range(0, len(body), 65536):
                if self.on_chunk:
                    self.on_chunk(i)
                yield body[i:i + 65536]
        return httpx.Response(status, headers=hdr, stream=httpx.ByteStream(b"".join(gen()))
                              if not self.on_chunk else _IterStream(gen()))


class _IterStream(httpx.SyncByteStream):
    def __init__(self, it):
        self._it = it

    def __iter__(self):
        yield from self._it


# ------------------------------------------------------------------ catalog validation

def test_shipped_catalog_is_valid_and_thumbnails_exist():
    rows = examples.load_catalog(strict=True)
    assert rows, "hype_app/data/examples.json is empty"
    for ex in rows:
        assert (ROOT / "www" / ex.thumbnail).is_file()
        assert ex.url.startswith(examples.EXAMPLES_URL_PREFIX)
        assert ex.format_version <= bundle.FORMAT_VERSION
        assert ex.size_bytes > 0 and len(ex.sha256) == 64
        assert "—" not in ex.title + ex.description + ex.credit, "no em dashes in UI copy"


def test_shipped_catalog_ids_are_unique_and_lean():
    rows = examples.load_catalog(strict=True)
    ids = [r.id for r in rows]
    assert len(ids) == len(set(ids))
    assert all(r.size_bytes < 100 * 1024 * 1024 for r in rows), "examples are meant to be lean"


@pytest.mark.parametrize("bad, msg", [
    ({"url": "https://evil.example/x.hype"}, "url must start with"),
    ({"sha256": "abc"}, "sha256"),
    ({"size_bytes": 0}, "size_bytes"),
    ({"thumbnail": "examples/missing.jpg"}, "not found"),
    ({"format_version": bundle.FORMAT_VERSION + 1}, "newer"),
    ({"id": "../x"}, "bad id"),
])
def test_bad_rows_fail_strict_and_skip_lenient(tmp_path, bad, msg):
    with pytest.raises(ValueError, match=msg):
        _example(tmp_path, b"x" * 10, **bad)
    www = tmp_path / "www"
    row = {"id": "TEST", "title": "T", "size_bytes": 10, "sha256": "0" * 64, "url": GOOD_URL,
           "thumbnail": "examples/TEST.jpg"}
    row.update(bad)
    assert examples.parse_catalog(json.dumps({"schema": 1, "examples": [row]}), www=www,
                                  strict=False) == []


def test_min_app_version_and_desktop_only_filters(tmp_path, root, monkeypatch):
    www = tmp_path / "www"
    (www / "examples").mkdir(parents=True)
    (www / "examples" / "TEST.jpg").write_bytes(b"x")
    base = {"title": "T", "size_bytes": 10, "sha256": "0" * 64, "url": GOOD_URL,
            "thumbnail": "examples/TEST.jpg"}
    cat = {"schema": 1, "examples": [
        dict(base, id="old", min_app_version="1.0.0"),
        dict(base, id="new", min_app_version="9.9.9"),
        dict(base, id="dsk", desktop_only=True)]}
    p = tmp_path / "cat.json"
    p.write_text(json.dumps(cat))
    # the validator resolves thumbnails against the REAL www dir; point it at the fixture
    monkeypatch.setattr(examples, "www_dir", lambda: www)
    ids = [e.id for e in examples.load_catalog(p, app_version="1.0.5", desktop=True)]
    assert ids == ["old", "dsk"]
    ids = [e.id for e in examples.load_catalog(p, app_version="1.0.5", desktop=False)]
    assert ids == ["old"]


def test_human_size_and_stem():
    assert examples.human_size(16747993) == "16 MB"
    assert examples.human_size(1.3 * 1024 ** 3) == "1.3 GB"
    assert examples.human_size(512) == "512 B"
    assert examples.human_size(2048) == "2 KB"
    ex = examples.Example(id="X", title="San Saba River, SS01208 (v2)!", description="", tags=(),
                          size_bytes=1, sha256="0" * 64, url=GOOD_URL, thumbnail="t")
    assert ex.stem == "San Saba River SS01208 v2"


# --------------------------------------------------------------------------- fetch

def test_fetch_happy_path_writes_cache_and_reports_progress(tmp_path, root):
    data = _payload()
    ex = _example(tmp_path, data)
    srv = _Server(data)
    seen: list[tuple[int, int]] = []
    out = examples.fetch(ex, progress=lambda d, t: seen.append((d, t)),
                         transport=httpx.MockTransport(srv))
    assert out == examples.cached_path(ex)
    assert out.read_bytes() == data
    assert examples.is_cached(ex)
    assert not examples.part_path(ex).exists()
    assert seen[-1] == (len(data), len(data))
    assert out.parent == tmp_path / "examples"          # HYPE_DATA_ROOT/examples
    # a second fetch is a cache hit: no request at all
    n = len(srv.requests)
    assert examples.fetch(ex, transport=httpx.MockTransport(srv)) == out
    assert len(srv.requests) == n


def test_fetch_follows_redirects(tmp_path, root):
    data = _payload()
    ex = _example(tmp_path, data)
    srv = _Server(data, redirect=True)
    examples.fetch(ex, transport=httpx.MockTransport(srv))
    assert [r.url.host for r in srv.requests] == ["github.com", "objects.example"]


def test_fetch_resumes_a_partial_with_range(tmp_path, root):
    data = _payload()
    ex = _example(tmp_path, data)
    part = examples.part_path(ex)
    part.parent.mkdir(parents=True)
    part.write_bytes(data[:100_000])
    srv = _Server(data)
    examples.fetch(ex, transport=httpx.MockTransport(srv))
    assert srv.requests[0].headers.get("Range") == "bytes=100000-"
    assert examples.cached_path(ex).read_bytes() == data


def test_fetch_restarts_when_server_ignores_range(tmp_path, root):
    data = _payload()
    ex = _example(tmp_path, data)
    part = examples.part_path(ex)
    part.parent.mkdir(parents=True)
    part.write_bytes(b"junk" * 1000)
    examples.fetch(ex, transport=httpx.MockTransport(_Server(data, ranges=False)))
    assert examples.cached_path(ex).read_bytes() == data


def test_fetch_sha_mismatch_deletes_partial(tmp_path, root):
    data = _payload()
    ex = _example(tmp_path, data, sha256="f" * 64)
    with pytest.raises(examples.ExampleCorrupt):
        examples.fetch(ex, transport=httpx.MockTransport(_Server(data)))
    assert not examples.part_path(ex).exists()
    assert not examples.cached_path(ex).exists()


def test_fetch_size_mismatch_is_corrupt(tmp_path, root):
    data = _payload()
    ex = _example(tmp_path, data, size_bytes=len(data) + 5)
    with pytest.raises(examples.ExampleCorrupt):
        examples.fetch(ex, transport=httpx.MockTransport(_Server(data)))
    assert not examples.part_path(ex).exists()


def test_fetch_cancel_keeps_partial_for_resume(tmp_path, root):
    # iter_bytes re-chunks the server's 64 KB pieces into the client's 256 KB reads, so trip
    # the cancel well past the first client chunk: at least one chunk lands before the check.
    data = _payload(1_200_000)
    ex = _example(tmp_path, data)
    cancel = threading.Event()

    def trip(offset):
        if offset >= 600_000:
            cancel.set()
    with pytest.raises(examples.ExampleCancelled):
        examples.fetch(ex, cancel=cancel, transport=httpx.MockTransport(_Server(data, on_chunk=trip)))
    part = examples.part_path(ex)
    assert part.exists() and 0 < part.stat().st_size < len(data)
    # ...and a later fetch resumes from it
    srv = _Server(data)
    examples.fetch(ex, transport=httpx.MockTransport(srv))
    assert srv.requests[0].headers.get("Range", "").startswith("bytes=")
    assert examples.is_cached(ex)


def test_fetch_404_is_example_gone(tmp_path, root):
    ex = _example(tmp_path, _payload())
    with pytest.raises(examples.ExampleGone) as ei:
        examples.fetch(ex, transport=httpx.MockTransport(_Server(b"", gone=True)))
    assert "no longer available" in str(ei.value)
    assert "Update HYPE" in str(ei.value)


def test_fetch_retries_transient_then_fails_clean(tmp_path, root):
    ex = _example(tmp_path, _payload())
    calls = {"n": 0}

    def boom(request):
        calls["n"] += 1
        raise httpx.ConnectError("nope", request=request)
    with pytest.raises(examples.ExampleError, match="Download failed"):
        examples.fetch(ex, transport=httpx.MockTransport(boom), sleep=lambda s: None)
    assert calls["n"] == 3


def test_base_url_override_rewrites_to_basename(tmp_path, root, monkeypatch):
    ex = _example(tmp_path, b"x")
    monkeypatch.setenv(examples.BASE_URL_ENV, "http://127.0.0.1:8020/")
    assert examples.resolve_url(ex) == "http://127.0.0.1:8020/TEST.hype"


def test_remove_deletes_cache_and_partial(tmp_path, root):
    ex = _example(tmp_path, b"x" * 10)
    for p in (examples.cached_path(ex), examples.part_path(ex)):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    examples.remove(ex)
    assert not examples.cached_path(ex).exists() and not examples.part_path(ex).exists()
    examples.remove(ex)                                  # idempotent


def test_cache_dir_override(tmp_path, root):
    examples.set_cache_dir(tmp_path / "elsewhere")
    try:
        assert examples.cache_dir() == tmp_path / "elsewhere"
    finally:
        examples.set_cache_dir(None)
    assert examples.cache_dir() == tmp_path / "examples"
