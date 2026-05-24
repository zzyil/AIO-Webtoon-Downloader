"""Tests for sites/mangadex.py — covers (a) the audit fix that stopped
substituting data_filenames as a placeholder for empty dataSaver, and
(b) the /at-home/server transient-5xx retry policy added 2026-05-16 to
stop silent chapter skips when the assignment API hiccups.

Cross-file: MangaDexSiteHandler._fetch_at_home_assignment is called by
get_chapter_images; the resulting saver_filenames flows into
saver_swaps_left initialization. When saver_filenames is empty, the
audit fix forces saver_swaps_left=0 so the data-saver retry phase
doesn't build 404-bound URLs. The retry policy sits one layer up:
5xx responses retry within _AT_HOME_RETRIES attempts before the
HTTPError propagates to aio-dl.py's call-site, which converts it
into a ChapterSkippedError for the strict-wrapper to handle.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from sites.mangadex import MangaDexSiteHandler


class _FakeResponse:
    """Minimal stand-in for requests.Response in scraper.get tests.

    `status_code` defaults to 200; pass a different code to simulate a 5xx
    or 4xx. `raise_for_status` mirrors requests.Response: raises HTTPError
    iff status_code >= 400. `payload` is returned by `.json()` only when
    the response is "success-shaped"; for error responses it would be
    unused (callers raise_for_status first).
    """

    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(
                f"{self.status_code} Server Error", response=self
            )
            raise err

    def json(self):
        return self._payload


def _make_scraper_returning(payload):
    """Build a MagicMock that emulates `scraper.get(...)` returning payload."""
    scraper = MagicMock()
    scraper.get.return_value = _FakeResponse(payload)
    return scraper


def _make_scraper_with_responses(*responses):
    """Build a MagicMock whose `scraper.get(...)` returns the supplied
    responses in order across successive calls. Pass _FakeResponse(...)
    instances OR exception instances (the latter will be raised when that
    call lands)."""
    scraper = MagicMock()
    side_effects = list(responses)
    scraper.get.side_effect = side_effects
    return scraper


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Stub time.sleep in the mangadex module so retry tests don't actually
    burn wall-clock waiting on the backoff. Without this, every retry test
    would idle ~7s through the real exponential schedule.
    """
    import sites.mangadex as mangadex_module
    monkeypatch.setattr(mangadex_module.time, "sleep", lambda _s: None)


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


# ---------------------------------------------------------------------------
# /at-home/server transient-5xx retry tests (added 2026-05-16).
# ---------------------------------------------------------------------------
# These cover the retry loop introduced after the user reported a silent
# Chapter 5 skip on Shuumatsu no Valkyrie when the API returned 500 once
# and the HTTPError bypassed both the strict-wrapper multi-source fallback
# and the chapter-loop's bare except. The retry absorbs transient 500s
# within ~7s worst-case so the chapter completes on MangaDex itself;
# the call-site broad-catch in aio-dl.py handles the case where retries
# are exhausted (HTTPError propagates → converted to ChapterSkippedError).


_GOOD_PAYLOAD = {
    "baseUrl": "https://node.mangadex.network",
    "chapter": {
        "hash": "abcdef0123456789",
        "data": ["1-aaaa.png", "2-bbbb.png"],
        "dataSaver": ["1-ssss.jpg", "2-tttt.jpg"],
    },
}


def test_retry_on_500_then_200_succeeds():
    """5xx response on attempt 1 retries and succeeds on attempt 2.

    Why this matters: the user-reported failure mode. MangaDex's
    /at-home/server returns 500 transiently — under the prior code the
    first HTTPError propagated immediately and the chapter was silently
    recorded as missed. With the retry, attempt 2 sees the API recover
    and the chapter downloads normally.
    """
    handler = MangaDexSiteHandler()
    scraper = _make_scraper_with_responses(
        _FakeResponse(status_code=500),
        _FakeResponse(_GOOD_PAYLOAD, status_code=200),
    )

    assignment = handler._fetch_at_home_assignment(scraper, "uuid")

    assert assignment["base_url"] == "https://node.mangadex.network"
    assert assignment["data_filenames"] == ["1-aaaa.png", "2-bbbb.png"]
    # Verify the retry actually fired by counting calls.
    assert scraper.get.call_count == 2


def test_retry_on_503_then_200_succeeds():
    """Sanity: every 5xx (not just 500) is treated as transient.

    Mihon's behavior + the existing classify_response_failure in aio-dl.py
    treat the 5xx band uniformly as retryable; the handler-internal retry
    should match that policy. 503 + 502 + 504 all surfaced in the user's
    log over the run, all worth retrying.
    """
    handler = MangaDexSiteHandler()
    scraper = _make_scraper_with_responses(
        _FakeResponse(status_code=503),
        _FakeResponse(_GOOD_PAYLOAD, status_code=200),
    )

    assignment = handler._fetch_at_home_assignment(scraper, "uuid")
    assert assignment["base_url"] == "https://node.mangadex.network"
    assert scraper.get.call_count == 2


def test_retry_5xx_exhausted_raises_http_error():
    """All _AT_HOME_RETRIES attempts return 5xx → HTTPError propagates.

    The caller (aio-dl.py:_process_chapter_impl ~line 6349) catches this
    via the `except Exception` branch added at the same time as the retry
    and converts to ChapterSkippedError so the strict wrapper engages
    multi-source fallback. We don't simulate that here — we just verify
    the handler propagates instead of silently swallowing.
    """
    handler = MangaDexSiteHandler()
    # Build a list of N 500-responses where N == retry budget.
    responses = [
        _FakeResponse(status_code=500)
        for _ in range(handler._AT_HOME_RETRIES)
    ]
    scraper = _make_scraper_with_responses(*responses)

    with pytest.raises(requests.HTTPError):
        handler._fetch_at_home_assignment(scraper, "uuid")

    # Every attempt was made (no early exit).
    assert scraper.get.call_count == handler._AT_HOME_RETRIES


def test_no_retry_on_404():
    """4xx fails fast — a 404 means the chapter genuinely doesn't exist at
    MD@H (e.g. post-DMCA wipe), retrying would just burn the 40 req/min
    rate-limit budget without recovering anything.
    """
    handler = MangaDexSiteHandler()
    # Single 404 — if retry kicked in on 4xx, the side_effect list would
    # be exhausted and StopIteration would surface instead of HTTPError.
    scraper = _make_scraper_with_responses(_FakeResponse(status_code=404))

    with pytest.raises(requests.HTTPError):
        handler._fetch_at_home_assignment(scraper, "uuid")

    # Exactly one call: no retry on 4xx.
    assert scraper.get.call_count == 1


def test_no_retry_on_403():
    """4xx fails fast for 403 too — same rationale as 404.

    Mostly defensive: MD doesn't currently 403 on /at-home/server in
    normal operation, but if a future ToS change introduced it (e.g.
    forced-login for adult content), we don't want to burn retries.
    """
    handler = MangaDexSiteHandler()
    scraper = _make_scraper_with_responses(_FakeResponse(status_code=403))

    with pytest.raises(requests.HTTPError):
        handler._fetch_at_home_assignment(scraper, "uuid")
    assert scraper.get.call_count == 1


def test_retry_on_network_exception_then_200():
    """A network exception (e.g. connection timeout, SSL handshake failure)
    counts as transient just like a 5xx. Recovers within the budget.
    """
    handler = MangaDexSiteHandler()
    scraper = _make_scraper_with_responses(
        requests.ConnectionError("connection reset"),
        _FakeResponse(_GOOD_PAYLOAD, status_code=200),
    )

    assignment = handler._fetch_at_home_assignment(scraper, "uuid")
    assert assignment["base_url"] == "https://node.mangadex.network"
    assert scraper.get.call_count == 2


def test_retry_network_exception_exhausted_propagates():
    """All retries hit network errors → the LAST exception propagates."""
    handler = MangaDexSiteHandler()
    responses = [
        requests.ConnectionError(f"reset {i}")
        for i in range(handler._AT_HOME_RETRIES)
    ]
    scraper = _make_scraper_with_responses(*responses)

    with pytest.raises(requests.ConnectionError):
        handler._fetch_at_home_assignment(scraper, "uuid")
    assert scraper.get.call_count == handler._AT_HOME_RETRIES


def test_first_attempt_success_no_retry():
    """Smoke: a 200 on the first attempt doesn't trigger retry machinery.

    Guards against accidentally setting up a wrong loop boundary that
    would issue a second call even on success.
    """
    handler = MangaDexSiteHandler()
    scraper = _make_scraper_returning(_GOOD_PAYLOAD)

    assignment = handler._fetch_at_home_assignment(scraper, "uuid")
    assert assignment["base_url"] == "https://node.mangadex.network"
    assert scraper.get.call_count == 1


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
