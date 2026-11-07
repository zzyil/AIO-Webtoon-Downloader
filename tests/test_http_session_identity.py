"""Regression guards for the shared HTTP session's browser identity.

WHAT BROKE (2026-10-31): every run built its session with cloudscraper asked
for macOS Chrome, and cloudscraper picks a random UA from a 2016-2019 list.
atsu.moe's Cloudflare blocked all of the macOS picks with 403, so atsu update
checks, downloads and search failed while the site loaded fine in a browser.
sites/http_session.py now pins one modern UA + Accept for every session.

Guards: the identity is modern and identical every time, it survives a missing
cloudscraper, handlers' configure_session still overrides it, and no entry
point builds its own cloudscraper session again.

Cross-file: sites/http_session.py (grep create_http_session),
sites/browser_identity.py:FALLBACK_UA. No network.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

import sites.http_session as http_session
from sites.browser_identity import FALLBACK_UA
from sites.mangadex import MangaDexSiteHandler

ROOT = Path(__file__).resolve().parent.parent


def _chrome_major(ua: str) -> int:
    return int(re.search(r"Chrome/(\d+)", ua).group(1))


def test_session_presents_one_modern_identity():
    first = http_session.create_http_session()
    ua = first.headers["User-Agent"]
    assert ua == FALLBACK_UA
    assert _chrome_major(ua) >= 120, f"UA must not be ancient: {ua}"
    # The exact family atsu.moe blocked: old Chrome on macOS 10.x.
    assert "Macintosh" not in ua
    assert "image/avif" in first.headers["Accept"], "modern UA needs a modern Accept"
    # cloudscraper randomises per session; the pin must not.
    for _ in range(5):
        again = http_session.create_http_session()
        assert again.headers["User-Agent"] == ua
        assert again.headers["Accept"] == first.headers["Accept"]


def test_identity_holds_without_cloudscraper(monkeypatch):
    monkeypatch.setattr(http_session, "cloudscraper", None)
    s = http_session.create_http_session()
    assert type(s) is requests.Session
    assert s.headers["User-Agent"] == FALLBACK_UA


def test_handler_configure_session_still_wins():
    # mangadex must NOT impersonate a browser (its API rules); configure_session
    # runs after the factory in every entry point, so its UA replaces the pin.
    s = http_session.create_http_session()
    MangaDexSiteHandler().configure_session(s, None)
    assert s.headers["User-Agent"].startswith("AIO-Webtoon-Downloader/")


def test_no_entry_point_builds_its_own_cloudscraper_session():
    for name in ("aio-dl.py", "aio_search_cli.py", "api.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "create_scraper(" not in src, f"{name} bypasses create_http_session"
        assert "create_http_session()" in src, f"{name} lost the shared session"
