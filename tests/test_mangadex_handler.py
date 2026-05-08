"""Tests for sites/mangadex.py — particularly the audit fix that stopped
substituting data_filenames as a placeholder for empty dataSaver.

Cross-file: MangaDexSiteHandler._fetch_at_home_assignment is called by
get_chapter_images; the resulting saver_filenames flows into
saver_swaps_left initialization. When saver_filenames is empty, the
audit fix forces saver_swaps_left=0 so the data-saver retry phase
doesn't build 404-bound URLs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sites.mangadex import MangaDexSiteHandler


class _FakeResponse:
    """Minimal stand-in for requests.Response in scraper.get tests."""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_scraper_returning(payload):
    """Build a MagicMock that emulates `scraper.get(...)` returning payload."""
    scraper = MagicMock()
    scraper.get.return_value = _FakeResponse(payload)
    return scraper


def test_assignment_keeps_saver_empty_when_api_returns_empty():
    """Audit fix: when the at-home/server response has dataSaver=[], the
    handler must NOT substitute data_filenames into saver_filenames.
    Pre-fix behavior copied data_filenames into saver_filenames so the
    eventual /data-saver/ URL would 404 every page (data and dataSaver
    use different per-page hashes).
    """
    handler = MangaDexSiteHandler()
    scraper = _make_scraper_returning({
        "baseUrl": "https://node.mangadex.network",
        "chapter": {
            "hash": "abcdef0123456789",
            "data": ["1-aaaa.png", "2-bbbb.png", "3-cccc.png"],
            "dataSaver": [],  # <-- empty saver list
        },
    })

    assignment = handler._fetch_at_home_assignment(scraper, "chapter-uuid")

    assert assignment["base_url"] == "https://node.mangadex.network"
    assert assignment["file_hash"] == "abcdef0123456789"
    assert assignment["data_filenames"] == ["1-aaaa.png", "2-bbbb.png", "3-cccc.png"]
    # The fix: saver_filenames stays EMPTY rather than aliasing data.
    assert assignment["saver_filenames"] == []


def test_assignment_keeps_saver_when_api_returns_real_list():
    """Sanity check: when dataSaver IS populated, saver_filenames should
    pass through verbatim — the audit fix only affects the empty case."""
    handler = MangaDexSiteHandler()
    scraper = _make_scraper_returning({
        "baseUrl": "https://node.mangadex.network",
        "chapter": {
            "hash": "h",
            "data": ["1-d.png"],
            "dataSaver": ["1-s.jpg"],
        },
    })

    assignment = handler._fetch_at_home_assignment(scraper, "chapter-uuid")
    assert assignment["data_filenames"] == ["1-d.png"]
    assert assignment["saver_filenames"] == ["1-s.jpg"]


def test_assignment_raises_on_missing_data():
    """Sanity: incomplete payload (no data filenames) is treated as a
    parse failure regardless of saver state."""
    import pytest

    handler = MangaDexSiteHandler()
    scraper = _make_scraper_returning({
        "baseUrl": "https://node.mangadex.network",
        "chapter": {
            "hash": "h",
            "data": [],  # empty data list = no usable URLs
            "dataSaver": [],
        },
    })

    with pytest.raises(RuntimeError, match="incomplete payload"):
        handler._fetch_at_home_assignment(scraper, "chapter-uuid")


def test_extension_from_filename_recognizes_png_jpg_webp():
    """The extension sniffer derives a sensible default from the filename
    suffix; falls back to .jpg when missing or unknown."""
    handler = MangaDexSiteHandler()
    assert handler._extension_from_filename("1-abc.png") == ".png"
    assert handler._extension_from_filename("2-def.jpg") == ".jpg"
    assert handler._extension_from_filename("3-ghi.webp") == ".webp"
    # Unknown extension → fallback to .jpg
    assert handler._extension_from_filename("4-jkl.exe") == ".jpg"
    # Empty / no extension → fallback to .jpg
    assert handler._extension_from_filename("") == ".jpg"
    assert handler._extension_from_filename("noext") == ".jpg"
    # Trailing dot → fallback
    assert handler._extension_from_filename("trailing.") == ".jpg"
