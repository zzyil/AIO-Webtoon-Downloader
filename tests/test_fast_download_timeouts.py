"""Coverage for the fast image-download path's timeout + retry semantics.

WHY THIS EXISTS (bench/linewebtoonlogs.md, 2026-08-20): curl_cffi's scalar
``timeout=`` sets only ``CurlOpt.TIMEOUT_MS`` — a TOTAL-transfer deadline —
while ``requests``/cloudscraper give the identical number per-read STALL
semantics on aio-dl.py's slow path. So one ``--http-timeout 30`` meant two
different things, and the sites routed through the fast path (linewebtoon,
mangafire) had every large page guillotined mid-transfer no matter how healthy
the connection was. 17 of the 18 failures in that log were transfers still
making progress, one at 670472 of 678424 bytes. Two such pages then failed a
chapter's zero-tolerance gate and --multi-source replaced a 120-page official
LINE Webtoon chapter with a 103-page third-party re-host.

The regression is invisible to any test that mocks the HTTP layer: the bug is
in WHICH CURL OPTIONS get set, and a mock never sets them. So these assert on
the option dicts directly, plus live-socket tests against a local server that
trickles a body slower than a total deadline would allow.

Cross-file: sites/base.py (_fast_dl_stall_options / _fast_dl_retry_options /
fast_download_images), aio-dl.py's two call sites (grep fast_attempts).
"""

from __future__ import annotations

import os
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
    _FAST_DL_CEILING_MULTIPLIER,
    _FAST_DL_STALL_BYTES_PER_SEC,
    BaseSiteHandler,
    _fast_dl_retry_options,
    _fast_dl_stall_options,
)

curl_only = pytest.mark.skipif(
    not _CURL_CFFI_AVAILABLE, reason="curl_cffi not installed"
)


# ────────────────────────────────────────────────────────────────────────
# The option dicts — the actual fix
# ────────────────────────────────────────────────────────────────────────

@curl_only
def test_stall_options_set_low_speed_not_just_a_total_deadline():
    """The heart of the fix. LOW_SPEED_LIMIT/_TIME is what distinguishes
    "this connection has stalled" from "this download has taken a while";
    without them a 700 KB page on a throttled CDN can never finish, because
    every retry restarts from byte 0 into the same fixed deadline."""
    from curl_cffi.const import CurlOpt

    opts = _fast_dl_stall_options(30.0)
    assert opts[CurlOpt.LOW_SPEED_LIMIT] == _FAST_DL_STALL_BYTES_PER_SEC
    assert opts[CurlOpt.LOW_SPEED_TIME] == 30
    # Connect must be bounded separately — a scalar timeout= leaves
    # CONNECTTIMEOUT unset entirely, so a black-holed SYN would otherwise only
    # be caught by the (much larger) ceiling.
    assert opts[CurlOpt.CONNECTTIMEOUT_MS] == 30000
    # TIMEOUT_MS must NOT be pinned here; it rides the per-request timeout= as
    # a ceiling. Setting it here would silently restore the bug.
    assert CurlOpt.TIMEOUT_MS not in opts


@curl_only
def test_stall_floor_is_far_below_a_starved_but_live_stream():
    """A starved HTTP/2 stream on a busy CDN connection still moved ~20 KB/s
    in the production log. The floor has to sit well under that or the fix
    would abort the very transfers it exists to save."""
    assert _FAST_DL_STALL_BYTES_PER_SEC < 20_000 / 4


@curl_only
def test_ceiling_covers_a_worst_case_page_at_the_slowest_observed_rate():
    """800 KB at the slowest rate seen live (17 KB/s) is ~47s. The ceiling
    exists only to stop an endless trickle, so it must not bite first."""
    worst_case_seconds = 800_000 / 17_000
    assert 30.0 * _FAST_DL_CEILING_MULTIPLIER > worst_case_seconds


@curl_only
def test_retry_options_refuse_the_pooled_connection():
    """A retry that lands back on the connection whose stream just stalled is
    not a retry. The log's smoking gun: the same URL died at exactly 131064
    bytes three times, then "HTTP/2 stream 211 was not closed cleanly"."""
    from curl_cffi.const import CurlOpt

    opts = _fast_dl_retry_options(30.0)
    assert opts[CurlOpt.FRESH_CONNECT] == 1
    assert opts[CurlOpt.FORBID_REUSE] == 1
    # Retries keep the stall semantics too.
    assert opts[CurlOpt.LOW_SPEED_LIMIT] == _FAST_DL_STALL_BYTES_PER_SEC


def test_option_builders_are_inert_without_curl_cffi(monkeypatch):
    """Handlers degrade to the cloudscraper path when curl_cffi is missing;
    the builders must not raise on the way there (Android ships without it)."""
    import sites.base as base

    monkeypatch.setattr(base, "_CURL_CFFI_AVAILABLE", False)
    assert base._fast_dl_stall_options(30.0) == {}
    assert base._fast_dl_retry_options(30.0) == {}


# ────────────────────────────────────────────────────────────────────────
# The caller contract
# ────────────────────────────────────────────────────────────────────────

def test_fast_download_images_exposes_an_attempts_budget():
    """The old body hardcoded ``for attempt in range(2)``, so
    --http-max-retries was honored on the slow path and silently ignored on
    the fast one — making the two sites that USE the fast path the least
    resilient in the app."""
    import inspect

    sig = inspect.signature(BaseSiteHandler.fast_download_images)
    assert "attempts" in sig.parameters
    assert sig.parameters["attempts"].default >= 2


def test_both_aio_dl_call_sites_pass_the_users_retry_budget():
    """Structural check: the foreground chapter loop AND the inter-chapter
    image prefetch must both forward --http-max-retries. A prefetch that gives
    up early costs the whole prefetched chapter (its tdir is wiped and the
    foreground re-downloads it from scratch)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "aio-dl.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("attempts=fast_attempts") == 1
    assert src.count('attempts=int(globals().get("_HTTP_MAX_RETRIES", 6))') == 1


# ────────────────────────────────────────────────────────────────────────
# The near-miss retry in _process_chapter_strict. Its ORDER (primary retries,
# resuming, before any other site) is tested by driving main() in
# tests/test_multisource_rescue.py (grep test_primary_retries_run_before).
# ────────────────────────────────────────────────────────────────────────

def _aio_dl_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "aio-dl.py"), encoding="utf-8") as fh:
        return fh.read()


def test_near_miss_backoff_is_short_relative_to_the_cdn_recovery_backoff():
    """The near-miss retry is a blip-length pause for a host that just served
    nearly the whole chapter; if it converged on the CDN-recovery wait it would
    only be one more slow rung of the long ladder."""
    import re

    src = _aio_dl_source()
    near = float(
        re.search(r'AIO_NEAR_MISS_RETRY_BACKOFF", "([0-9.]+)"', src).group(1)
    )
    long_backoff = float(
        re.search(r'AIO_INLINE_CHAPTER_BACKOFF", "([0-9.]+)"', src).group(1)
    )
    assert 0 < near <= long_backoff / 4


# ────────────────────────────────────────────────────────────────────────
# Live socket: a body that arrives slower than a total deadline allows
# ────────────────────────────────────────────────────────────────────────

_PAYLOAD = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\x00" * 60_000
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/trickle":
            # Delivered over ~4s: longer than the 2s stall budget the test
            # passes, but never actually stalled. A total-deadline client kills
            # this; a stall-detecting one rides it out.
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(_PAYLOAD)))
            self.end_headers()
            step = len(_PAYLOAD) // 8
            try:
                for i in range(0, len(_PAYLOAD), step):
                    self.wfile.write(_PAYLOAD[i:i + step])
                    self.wfile.flush()
                    time.sleep(0.5)
            except Exception:
                pass
            return
        if path == "/silent":
            # Headers, then nothing. Must die on the stall budget.
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(_PAYLOAD)))
            self.end_headers()
            try:
                self.wfile.flush()
                time.sleep(30)
            except Exception:
                pass
            return
        self.send_error(404)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


@pytest.fixture()
def trickle_server():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
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


def _fetch(base_url, path, *, timeout, attempts, count=1):
    handler = _LocalHandler()
    tmp = tempfile.mkdtemp(prefix="fastdl_test_")
    try:
        tasks = [
            (i, base_url + path, tmp, "1_%04d.jpg" % i) for i in range(count)
        ]
        started = time.monotonic()
        results = handler.fast_download_images(
            tasks, concurrency=4, timeout=timeout, attempts=attempts
        )
        elapsed = time.monotonic() - started
        landed = [p for _, p in results if p]
        return landed, [os.path.getsize(p) for p in landed], elapsed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@curl_only
def test_a_slow_but_progressing_transfer_completes(trickle_server):
    """THE regression. The body takes ~4s to arrive with a 2s budget; under
    the old total-deadline semantics this is the production failure exactly,
    including the truncated-at-N-bytes error."""
    landed, sizes, _ = _fetch(trickle_server, "/trickle", timeout=2.0, attempts=2)
    assert len(landed) == 1, "slow-but-live transfer was guillotined again"
    assert sizes == [len(_PAYLOAD)], "body was truncated"


@curl_only
def test_a_silent_connection_still_dies_on_the_timeout_budget(trickle_server):
    """The other half: loosening the deadline must not mean waiting forever. A
    connection that delivers headers and then nothing has to abort on the
    user's --http-timeout, not on the (much larger) ceiling."""
    landed, _, elapsed = _fetch(trickle_server, "/silent", timeout=2.0, attempts=1)
    assert landed == []
    assert elapsed < 2.0 * _FAST_DL_CEILING_MULTIPLIER, (
        "took %.1fs — the stall detector is not bounding a dead socket" % elapsed
    )


@curl_only
def test_a_permanent_404_does_not_consume_the_retry_budget(trickle_server):
    """record_host_failure only fires on the LAST attempt, so retrying a 4xx
    also delays the ghost-chapter detector that feeds on those uniform
    (status, body_size) signatures (grep _is_ghost_chapter_signature)."""
    landed, _, elapsed = _fetch(trickle_server, "/missing", timeout=5.0, attempts=6)
    assert landed == []
    assert elapsed < 5.0, "404 took %.1fs — it is still being retried" % elapsed
