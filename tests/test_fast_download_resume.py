"""Coverage for resumed image retries on the fast download path.

WHY THIS EXISTS (2026-10-20 library refresh, Tower of God ch 284 / 453): LINE
Webtoon's CDN served one image per chapter at ~3-5 KB/s on every attempt, and
each retry restarted from byte 0 under the per-page ceiling, so a ~1 MB page
could never finish and the chapter went to --multi-source. Retries now ask for
the rest of the bytes (sites/base.py, grep _PartialImage). The risk that
introduces is a page assembled from two different objects, so most tests here
are about the server lying: ignoring Range, answering with another object,
the wrong offset, the wrong total, a compressed body, a 416. In every case the
page must come out byte-identical to ONE object or not at all.

Live-socket tests against a local scripted server, because the behaviour under
test is what curl_cffi hands back from a transfer it aborted (exc.response),
which no mock reproduces. Unit tests drive _PartialImage with fake responses
for the cases a socket can't stage deterministically.

Cross-file: sites/base.py (_PartialImage, fast_download_images._fetch_one),
tests/test_fast_download_timeouts.py (the stall/ceiling semantics this builds
on).
"""

from __future__ import annotations

import gzip
import os
import random
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import pytest

from sites.base import (
    _CURL_CFFI_AVAILABLE,
    _FAST_DL_RESUME_OVERLAP,
    BaseSiteHandler,
    _PartialImage,
)

curl_only = pytest.mark.skipif(
    not _CURL_CFFI_AVAILABLE, reason="curl_cffi not installed"
)


def _image(seed: int, size: int = 200_000) -> bytes:
    """A JPEG-headed body of incompressible bytes; `seed` picks the object."""
    rnd = random.Random(seed)
    head = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return head + bytes(rnd.getrandbits(8) for _ in range(size - len(head)))


A = _image(1)
B = _image(2)  # same length as A, different object
CUT = int(len(A) * 0.4)  # where a stalled first transfer goes silent


# ────────────────────────────────────────────────────────────────────────
# Scripted server: each path names a scenario; the Nth request to a path
# gets the Nth step. Every request's Range header is recorded.
# ────────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: dict = {}
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, status, body, *, headers=None, stall_after=None):
        self.send_response(status)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Accept-Ranges", "bytes")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if "Content-Length" not in (headers or {}):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if stall_after is None:
                self.wfile.write(body)
                self.wfile.flush()
            else:
                self.wfile.write(body[:stall_after])
                self.wfile.flush()
                time.sleep(8)  # silent: the client's stall detector aborts
        except Exception:
            pass

    def _range206(self, obj, start=None, total=None):
        rng = self.headers.get("Range") or ""
        first = int(rng.split("=")[1].rstrip("-")) if rng else 0
        start = first if start is None else start
        total = len(obj) if total is None else total
        body = obj[start:]
        self._send(206, body, headers={
            "Content-Range": f"bytes {start}-{start + len(body) - 1}/{total}",
            "Content-Length": str(len(body)),
        })

    def do_GET(self):
        path = self.path.split("?")[0]
        scenario = path.strip("/").split("/")[0]
        with self.lock:
            seen = self.log.setdefault(path, [])
            seen.append(self.headers.get("Range"))
            n = len(seen)
        # Most scenarios open with the stalled transfer of A; the ones with
        # their own first step are excluded.
        if n == 1 and scenario not in ("plain", "gzip", "page"):
            self._send(200, A, stall_after=CUT)
            return
        if scenario == "resume":
            self._range206(A)
        elif scenario == "page":
            # Two pages, two objects: /page/1 is A, /page/2 is B.
            obj = A if path.endswith("/1") else B
            if n == 1:
                self._send(200, obj, stall_after=CUT)
            else:
                self._range206(obj)
        elif scenario == "ignores":
            self._send(200, A)
        elif scenario == "changed":
            # The object behind the URL changed between attempts.
            if self.headers.get("Range"):
                self._range206(B)
            else:
                self._send(200, B)
        elif scenario == "offset":
            if self.headers.get("Range"):
                self._range206(A, start=1000)
            else:
                self._send(200, A)
        elif scenario == "total":
            if self.headers.get("Range"):
                self._range206(A, total=len(A) + 10)
            else:
                self._send(200, A)
        elif scenario == "r416":
            if self.headers.get("Range"):
                self._send(416, b"", headers={"Content-Range": f"bytes */{len(A)}"})
            else:
                self._send(200, A)
        elif scenario == "gzip":
            if n == 1:
                gz = gzip.compress(A)
                self._send(200, gz, headers={"Content-Encoding": "gzip"},
                           stall_after=len(gz) // 2)
            else:
                self._send(200, A)
        elif scenario == "plain":
            self._send(200, A)
        else:
            self.send_error(404)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


@pytest.fixture()
def server():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    _Handler.log = {}
    srv = _Server(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        srv.shutdown()
        srv.server_close()


class _LocalHandler(BaseSiteHandler):
    SUPPORTS_FAST_DOWNLOAD = _CURL_CFFI_AVAILABLE
    name = "localtest"
    domains = ("127.0.0.1",)


def _fetch(base_url, paths, *, attempts=3):
    """Download `paths` as one chapter; returns {path: bytes or None}."""
    tmp = tempfile.mkdtemp(prefix="fastdl_resume_")
    try:
        tasks = [(i, base_url + p, tmp, "1_%04d.jpg" % i) for i, p in enumerate(paths)]
        results = _LocalHandler().fast_download_images(
            tasks, concurrency=4, timeout=1.0, attempts=attempts
        )
        out = {}
        for (i, p), (_, got) in zip(enumerate(paths), results):
            if got:
                with open(got, "rb") as fh:
                    out[p] = fh.read()
            else:
                out[p] = None
        leftovers = [f for f in os.listdir(tmp) if f.startswith(".pending_")]
        assert leftovers == [], "pending files left behind: %r" % leftovers
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────
# The fix
# ────────────────────────────────────────────────────────────────────────

@curl_only
def test_a_stalled_page_resumes_from_what_it_already_has(server):
    """The Tower of God case: the first transfer dies part-way; the retry asks
    for the rest only (minus the overlap window) and the page is exact."""
    got = _fetch(server, ["/resume/x"])
    assert got["/resume/x"] == A
    assert _Handler.log["/resume/x"] == [None, f"bytes={CUT - _FAST_DL_RESUME_OVERLAP}-"]


@curl_only
def test_resumed_pages_never_swap_bytes_between_pages(server):
    """Two pages of one chapter stalling together each resume into their OWN
    object: the held bytes are per page, so no page can be built from
    another's."""
    got = _fetch(server, ["/page/1", "/page/2"])
    assert got["/page/1"] == A
    assert got["/page/2"] == B


@curl_only
def test_a_clean_page_sends_no_range(server):
    got = _fetch(server, ["/plain/x"])
    assert got["/plain/x"] == A
    assert _Handler.log["/plain/x"] == [None]


# ────────────────────────────────────────────────────────────────────────
# The server lies: the page must be ONE object or a miss
# ────────────────────────────────────────────────────────────────────────

@curl_only
def test_a_server_that_ignores_range_gives_the_whole_body_not_a_splice(server):
    """swebtoon-phinf answered a Range request on a cold object with 200 + the
    full body (live probe 2026-10-27). Appending that would double the page's
    first 40%."""
    got = _fetch(server, ["/ignores/x"])
    assert got["/ignores/x"] == A


@curl_only
def test_a_changed_object_is_detected_and_fetched_whole(server):
    """Same length, different bytes: only the overlap window can tell. The
    resume is rejected and the third request asks for the whole object."""
    got = _fetch(server, ["/changed/x"])
    assert got["/changed/x"] == B
    ranges = _Handler.log["/changed/x"]
    assert ranges[0] is None and ranges[1] is not None and ranges[2] is None


@curl_only
def test_a_range_at_the_wrong_offset_is_rejected(server):
    got = _fetch(server, ["/offset/x"])
    assert got["/offset/x"] == A
    assert _Handler.log["/offset/x"][2] is None


@curl_only
def test_a_range_with_a_different_total_is_rejected(server):
    got = _fetch(server, ["/total/x"])
    assert got["/total/x"] == A
    assert _Handler.log["/total/x"][2] is None


@curl_only
def test_a_416_restarts_instead_of_failing_the_page(server):
    """416 is a 4xx, which the fast path otherwise treats as permanent; for a
    resume it only means the offset was refused."""
    got = _fetch(server, ["/r416/x"])
    assert got["/r416/x"] == A
    assert _Handler.log["/r416/x"][2] is None


@curl_only
def test_a_compressed_body_is_never_resumed(server):
    """curl hands back DECODED bytes, whose offsets mean nothing to Range."""
    got = _fetch(server, ["/gzip/x"])
    assert got["/gzip/x"] == A
    assert _Handler.log["/gzip/x"] == [None, None]


@curl_only
def test_a_rejected_resume_with_no_attempts_left_is_a_miss(server):
    """With the budget spent on the stall and the bad resume, the page is
    missing (the chapter gate in aio-dl.py counts it), never half-written."""
    got = _fetch(server, ["/changed/y"], attempts=2)
    assert got["/changed/y"] is None


# ────────────────────────────────────────────────────────────────────────
# _PartialImage directly
# ────────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, content, headers=None):
        self.status_code = status
        self.content = content
        self.headers = {k.title(): v for k, v in (headers or {}).items()}

    # curl_cffi's Headers is case-insensitive; the class looks up Title-Case.


def _held(data_len=CUT):
    p = _PartialImage()
    p.absorb_failure(_Resp(200, A[:data_len], {"Content-Length": str(len(A))}), None)
    return p


def test_a_body_without_a_declared_length_is_not_held():
    p = _PartialImage()
    p.absorb_failure(_Resp(200, A[:CUT]), None)
    assert p.resume_from() is None


def test_a_206_that_dies_inside_the_overlap_keeps_the_held_bytes():
    p = _held()
    start = p.resume_from()
    short = A[start:start + 100]
    p.absorb_failure(_Resp(206, short, {"Content-Range": f"bytes {start}-{len(A) - 1}/{len(A)}"}), start)
    assert p.data == A[:CUT]


def test_a_206_that_dies_inside_the_overlap_with_wrong_bytes_resets():
    p = _held()
    start = p.resume_from()
    p.absorb_failure(_Resp(206, B[start:start + 100], {"Content-Range": f"bytes {start}-{len(A) - 1}/{len(A)}"}), start)
    assert p.resume_from() is None and p.data == b""


def test_a_failed_206_extends_the_held_bytes_for_the_next_attempt():
    p = _held()
    start = p.resume_from()
    more = A[start:CUT + 10_000]
    p.absorb_failure(_Resp(206, more, {"Content-Range": f"bytes {start}-{len(A) - 1}/{len(A)}"}), start)
    assert p.data == A[:CUT + 10_000]
    assert p.resume_from() == CUT + 10_000 - _FAST_DL_RESUME_OVERLAP


def test_an_unasked_206_is_rejected():
    p = _PartialImage()
    assert p.complete(_Resp(206, A, {"Content-Range": f"bytes 0-{len(A) - 1}/{len(A)}"}), None) is None


def test_a_206_longer_than_the_declared_total_is_rejected():
    p = _held()
    start = p.resume_from()
    body = A[start:] + b"extra"
    assert p.complete(_Resp(206, body, {"Content-Range": f"bytes {start}-{len(A) - 1}/{len(A)}"}), start) is None
    assert p.data == b""


def test_complete_returns_exactly_the_object():
    p = _held()
    start = p.resume_from()
    assert p.complete(_Resp(206, A[start:], {"Content-Range": f"bytes {start}-{len(A) - 1}/{len(A)}"}), start) == A
    assert p.data == b"" and p.resume_from() is None
