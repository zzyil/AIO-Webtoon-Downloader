"""Regression guard for the image-request `Accept` header (2026-08-03).

WHAT BROKE: cloudscraper seeds every session with a *document* Accept
(`text/html,...;q=0.9,image/webp,image/apng,*/*;q=0.8`), and curl_cffi's
`impersonate` does the same. WordPress.com content-negotiates image URLs on
that header — `<sub>.files.wordpress.com/...jpg` answers `200 text/html` with a
~19 KB attachment WRAPPER page when text/html outranks image/*, and the real
JPEG otherwise (`Vary: Accept`). Every image fetch in the tree therefore asked
for HTML, and manhuaplus' WordPress-hosted back catalogue (roughly < ch.1000,
subdomain varies PER CHAPTER) downloaded as markup. It was misdiagnosed as a
retired host; the host is fine, the request was wrong.

These tests reproduce that negotiation against a local server so the fix can't
regress without a red test. The server is deliberately dumb — it only branches
on Accept, which is the single behavior under test.

Cross-file: sites/_image_io.py:IMAGE_ACCEPT is the constant; the senders are
aio-dl.py:_try_download_url, sites/base.py (_fast_dl_build_headers,
_fetch_probe_item_bytes_ex, _probe_cover_image) and
sites/mangadex.py:_fetch_image_blob. Grep IMAGE_ACCEPT.
"""

from __future__ import annotations

import importlib
import io
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from sites._image_io import (
    IMAGE_ACCEPT,
    IMAGE_ACCEPT_HEADERS,
    looks_like_real_image,
)

aio = importlib.import_module("aio-dl")


# cloudscraper's real session default, verbatim — the header that caused this.
CLOUDSCRAPER_DOCUMENT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/webp,image/apng,*/*;q=0.8"
)

# WordPress.com's wrapper is ~19 KB; size matters only in that it sails past
# any byte-count heuristic, which is why the markup sniff has to be the gate.
WRAPPER_HTML = (
    b"<!DOCTYPE html>\n<html lang=\"vi\">\n<head><title>anh</title>\n"
    + b"<!-- padding -->\n" * 1200
    + b"</head><body><img src=\"x.jpg\"></body></html>"
)


def _make_jpeg() -> bytes:
    """A small but genuinely-decodable JPEG (magic + parseable SOF dims)."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


JPEG_BYTES = _make_jpeg()


def _prefers_html(accept: str) -> bool:
    """Mirror of the negotiation: does text/html outrank every image type?

    WordPress.com's actual rule is q-value based; the shipped document Accept
    puts text/html at an implicit q=1.0 ahead of `image/webp`, and the image
    Accept leads with `image/avif`. Comparing first-occurrence position
    reproduces the observed branch for both without a full q-parser.
    """
    accept = (accept or "").lower()
    if "text/html" not in accept:
        return False
    img = re.search(r"image/(?!\*)", accept)
    return img is None or accept.index("text/html") < img.start()


class _NegotiatingHandler(BaseHTTPRequestHandler):
    """Serves the wrapper page or the JPEG for the SAME URL, per Accept."""

    def do_GET(self):  # noqa: N802 - stdlib API
        accept = self.headers.get("Accept", "")
        type(self).seen_accepts.append(accept)
        if _prefers_html(accept):
            body, ctype = WRAPPER_HTML, "text/html; charset=utf-8"
        else:
            body, ctype = JPEG_BYTES, "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Vary", "Accept")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # keep pytest output clean


@pytest.fixture
def server():
    """Threaded one-shot server. Yields (base_url, seen_accepts)."""
    _NegotiatingHandler.seen_accepts = []
    httpd = HTTPServer(("127.0.0.1", 0), _NegotiatingHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", _NegotiatingHandler.seen_accepts
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


# ────────────────────────────────────────────────────────────────────────
# The constant itself
# ────────────────────────────────────────────────────────────────────────

def test_image_accept_outranks_html():
    """The one property that matters. If this inverts, wrapper pages return."""
    assert not _prefers_html(IMAGE_ACCEPT)
    assert "text/html" not in IMAGE_ACCEPT


def test_document_accept_is_what_triggers_the_bug():
    """Guards the test's own premise: the header we replaced really does
    select the wrapper branch. Without this, a broken _prefers_html could make
    every other test here pass vacuously."""
    assert _prefers_html(CLOUDSCRAPER_DOCUMENT_ACCEPT)


def test_wrapper_page_is_rejected_by_the_body_gate():
    """The download-side backstop still works: even a 19 KB HTML body served
    as 200 under an image URL is not an image. Belt and braces — the Accept
    fix stops us asking for it, this stops us storing it."""
    assert not looks_like_real_image(WRAPPER_HTML, content_type="text/html")
    assert looks_like_real_image(JPEG_BYTES, content_type="image/jpeg")


# ────────────────────────────────────────────────────────────────────────
# End-to-end through the real download path
# ────────────────────────────────────────────────────────────────────────

def test_dl_image_gets_the_jpeg_not_the_wrapper(server):
    """THE regression test. A session carrying cloudscraper's document Accept
    must still land real JPEG bytes, because dl_image overrides Accept
    per-request."""
    base_url, seen = server
    session = requests.Session()
    session.headers["Accept"] = CLOUDSCRAPER_DOCUMENT_ACCEPT

    tmp = tempfile.mkdtemp(prefix="aio_accept_test_")
    path = aio.dl_image(f"{base_url}/2020/07/004-5.jpg", tmp, "1_0001.jpg", session)

    assert path is not None, "download failed entirely"
    with open(path, "rb") as fh:
        head = fh.read(4)
    assert head[:2] == b"\xff\xd8", f"got non-JPEG bytes: {head!r}"
    assert os.path.getsize(path) == len(JPEG_BYTES)
    # And it asked correctly on the very first request — no wasted wrapper
    # fetch followed by a variant-cascade rescue.
    assert seen, "server saw no request"
    assert not _prefers_html(seen[0]), f"first request asked for HTML: {seen[0]!r}"


def test_dl_image_does_not_mutate_session_accept(server):
    """The override must stay per-request. Hoisting it onto session.headers
    would break the HTML page fetches that share this session (chapter lists,
    series metadata) — the reason the fix is a `headers=` kwarg."""
    base_url, _ = server
    session = requests.Session()
    session.headers["Accept"] = CLOUDSCRAPER_DOCUMENT_ACCEPT

    tmp = tempfile.mkdtemp(prefix="aio_accept_test_")
    aio.dl_image(f"{base_url}/p.jpg", tmp, "1_0001.jpg", session)

    assert session.headers["Accept"] == CLOUDSCRAPER_DOCUMENT_ACCEPT
    assert IMAGE_ACCEPT_HEADERS == {"Accept": IMAGE_ACCEPT}  # shared dict intact


def test_probe_and_cover_fetchers_send_image_accept(server):
    """The search-quality probe and the cover fallback fetch images too. Left
    on the document Accept they would score a negotiating site 0.0 on every
    page and rate it unusable for a purely request-side reason."""
    from sites.base import BaseSiteHandler, SearchHit

    base_url, _ = server
    session = requests.Session()
    session.headers["Accept"] = CLOUDSCRAPER_DOCUMENT_ACCEPT
    handler = BaseSiteHandler()

    data, timed_out = handler._fetch_probe_item_bytes_ex(f"{base_url}/p.jpg", session)
    assert not timed_out
    assert data is not None and data[:2] == b"\xff\xd8", "probe got the wrapper page"

    hit = SearchHit(
        site="t", title="t", url=f"{base_url}/s", cover=f"{base_url}/cover.jpg"
    )
    cover = handler._probe_cover_image(hit, session, None)
    assert cover is not None and cover[:2] == b"\xff\xd8", "cover got the wrapper page"
