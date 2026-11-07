"""Regression guards for the 2026-10 atsu.moe outage.

WHAT BROKE: atsu.moe moved every `/static/...` asset to `cdn.atsu.moe` and
started answering `410 Gone` on its own host. The handler kept joining the
API's host-less page paths onto atsu.moe, so every page failed, and dl_image's
extension-variant cascade then sent 9 more requests per page to the same dead
host before giving up.

Two guards:
  * dl_image stops after the first URL when it gets a 410, but a 404 still runs
    the cascade (that cascade exists for wrong extensions in a site's data).
  * the atsumaru handler builds page and cover URLs on the CDN host.

Cross-file: aio-dl.py:dl_image (grep "410 Gone"), sites/atsumaru.py
(grep _STATIC_URL). Uses a local server and stub scrapers; no network.
"""

from __future__ import annotations

import importlib
import io
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from sites.atsumaru import AtsumaruSiteHandler

aio = importlib.import_module("aio-dl")


def _make_webp() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 120, 200)).save(buf, format="WEBP")
    return buf.getvalue()


WEBP_BYTES = _make_webp()


class _StatusHandler(BaseHTTPRequestHandler):
    """Answers `status` for every path except `ok_suffix`, which gets a WebP.
    Records every requested path so tests can count requests."""

    status = 410
    ok_suffix = None

    def do_GET(self):  # noqa: N802 - stdlib API
        type(self).seen.append(self.path)
        if self.ok_suffix and self.path.endswith(self.ok_suffix):
            body, ctype, code = WEBP_BYTES, "image/webp", 200
        else:
            body, ctype, code = b"<html>gone</html>", "text/html", self.status
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    def _start(status, ok_suffix=None):
        _StatusHandler.status = status
        _StatusHandler.ok_suffix = ok_suffix
        _StatusHandler.seen = []
        httpd = HTTPServer(("127.0.0.1", 0), _StatusHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        started.append((httpd, t))
        return f"http://127.0.0.1:{httpd.server_port}", _StatusHandler.seen

    started = []
    # Per-chapter host failure counts are module state; a count left by
    # another test could trip the poison fast-fail and abort the cascade early.
    aio._reset_host_failures_for_chapter()
    try:
        yield _start
    finally:
        for httpd, t in started:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)
        aio._reset_host_failures_for_chapter()


def test_410_skips_extension_variants(server):
    base_url, seen = server(410)
    tmp = tempfile.mkdtemp()
    path = aio.dl_image(f"{base_url}/static/pages/x/1.avif", tmp, "1_0001.jpg", requests.Session())
    assert path is None
    # One request: 410 is classified permanent (no retries) and the cascade is skipped.
    assert seen == ["/static/pages/x/1.avif"]


def test_404_still_runs_extension_variants(server):
    """The cascade's real job: the site's data names the wrong extension."""
    base_url, seen = server(404, ok_suffix="/static/pages/x/1.webp")
    tmp = tempfile.mkdtemp()
    path = aio.dl_image(f"{base_url}/static/pages/x/1.jpg", tmp, "1_0001.jpg", requests.Session())
    assert path is not None and path.endswith(".webp")
    assert "/static/pages/x/1.webp" in seen
    assert len(seen) > 1


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _StubScraper:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append(url)
        return _StubResponse(self.payload)


def test_atsumaru_pages_use_cdn_host():
    payload = {
        "readChapter": {
            "pages": [
                {"image": "/static/pages/grp/r9UV2/aaaa.avif"},
                {"image": "/static/pages/13zQA/LWr0Nd2N/0.webp"},
                {"image": "https://cdn.atsu.moe/static/pages/abs/1.avif"},
            ]
        }
    }
    scraper = _StubScraper(payload)
    h = AtsumaruSiteHandler()
    urls = h.get_chapter_images({"_slug": "13zQA", "_chapter_id": "r9UV2"}, scraper, None)
    assert urls == [
        "https://cdn.atsu.moe/static/pages/grp/r9UV2/aaaa.avif",
        "https://cdn.atsu.moe/static/pages/13zQA/LWr0Nd2N/0.webp",
        "https://cdn.atsu.moe/static/pages/abs/1.avif",
    ]
    # The API call itself stays on the main host.
    assert scraper.calls == ["https://atsu.moe/api/read/chapter"]


@pytest.mark.parametrize(
    "poster",
    [
        "posters/abc.png",
        "/static/posters/abc.png",
        {"image": "static/posters/abc.png"},
    ],
)
def test_atsumaru_cover_uses_cdn_host(poster):
    h = AtsumaruSiteHandler()
    assert h._parse_cover({"poster": poster}) == "https://cdn.atsu.moe/static/posters/abc.png"
