"""Tests for aio-dl.py:make_request retry classification.

Specifically covers the 2026-05-16 fix that closed a silent-failure hole:
a 5xx response with a body >= 100 chars (e.g. MangaDex returning 503 with
a maintenance HTML page on the /manga/<uuid> endpoint) used to slip
through make_request's "Warning: Got status N but response has content,
continuing..." path. The bad response was then returned to
fetch_comic_context, whose .json() blew up with the cryptic
"Expecting value: line 1 column 1 (char 0)" the user reported. The fix
classifies the response and raises HTTPError for the 'retryable' class
so the outer retry loop engages with exponential backoff.

Cross-file: make_request's retry behavior is consumed by every site
handler that goes through it (the standard path for site metadata
fetches). The fix is symmetric with the existing origin_error branch
which already raised on 520-527 with content.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest
import requests


def _aio_dl():
    """Load aio-dl.py as a Python module. The file has a hyphen in its
    name so it can't be imported via normal `import aio_dl`; load it
    once via importlib and cache in sys.modules."""
    if "aio_dl" in sys.modules:
        return sys.modules["aio_dl"]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "aio_dl", os.path.join(here, "aio-dl.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aio_dl"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeResponse:
    """Minimal stand-in for requests.Response. status_code + text are
    enough for the classify path; .headers is empty so the Retry-After
    branch doesn't fire; raise_for_status mirrors requests' contract."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode() if text else b""
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Error", response=self
            )

    def json(self):
        import json
        return json.loads(self.text or "")


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Patch time.sleep + the make_request backoff knobs so tests don't
    burn wall-clock on the exponential schedule. Without this, the
    6-retry default would idle ~90s through real backoffs.
    """
    mod = _aio_dl()
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    # Also stub the random.uniform jitter so the delay arithmetic in
    # make_request is deterministic (cheap insurance against flakes).
    monkeypatch.setattr(mod.random, "uniform", lambda _a, _b: 1.0)


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear cross-test state so a prior test's recorded rate-limits /
    polite delays / host failures don't bleed into the current test."""
    mod = _aio_dl()
    if hasattr(mod, "_reset_host_concurrency_caps"):
        mod._reset_host_concurrency_caps()
    if hasattr(mod, "_RATE_LIMIT_SCHEDULE"):
        mod._RATE_LIMIT_SCHEDULE.clear()
    if hasattr(mod, "_HOST_POLITE_DELAY"):
        mod._HOST_POLITE_DELAY.clear()
    yield


def _make_scraper(*responses_or_excs):
    """Build a MagicMock whose .get(url, **kw) returns/raises the
    supplied items in order across successive calls."""
    scraper = MagicMock()
    scraper.get.side_effect = list(responses_or_excs)
    return scraper


# ---------------------------------------------------------------------------
# The user-reported scenario: 503 with maintenance HTML body.
# ---------------------------------------------------------------------------

# Maintenance page that lacks the rate-limit keywords (per _RL_BODY_KEYWORDS)
# so it classifies as 'retryable', not 'rate_limit'. >100 chars to trigger
# the body-size threshold the prior bug was hiding behind.
_MAINTENANCE_HTML = (
    "<html><head><title>Service Temporarily Unavailable</title></head>"
    "<body><h1>503 Service Unavailable</h1><p>MangaDex API is undergoing "
    "scheduled maintenance. Please retry shortly.</p></body></html>"
)
assert len(_MAINTENANCE_HTML) > 100, "test fixture must exceed the 100-char threshold"


def test_503_with_body_retries_and_succeeds_on_recovery():
    """503 with a maintenance HTML body >= 100 chars retries and
    eventually returns the recovered 200. Without the fix, the bad
    503 response was returned as-is.
    """
    mod = _aio_dl()
    scraper = _make_scraper(
        _FakeResponse(503, _MAINTENANCE_HTML),
        _FakeResponse(200, '{"data": {"id": "abc"}}'),
    )

    resp = mod.make_request("https://api.example.com/manga/abc", scraper)

    assert resp.status_code == 200
    assert scraper.get.call_count == 2


def test_503_with_body_exhausts_retries_and_raises():
    """All retries return 503 → outer loop exhausts and propagates
    HTTPError. The caller (fetch_comic_context) treats this as a fatal
    'Failed to fetch comic data' rather than handing the caller an
    error response to .json()."""
    mod = _aio_dl()
    max_retries = int(getattr(mod, "_HTTP_MAX_RETRIES", 6))
    scraper = _make_scraper(
        *[_FakeResponse(503, _MAINTENANCE_HTML) for _ in range(max_retries)]
    )

    with pytest.raises(requests.HTTPError):
        mod.make_request("https://api.example.com/manga/abc", scraper)
    assert scraper.get.call_count == max_retries


def test_500_with_body_retries():
    """500 with a long body is also 'retryable' — same path as 503.
    Guards against accidentally hardcoding the fix to one status code."""
    mod = _aio_dl()
    long_body = "A" * 200  # any >100-char body, no rate-limit keyword
    scraper = _make_scraper(
        _FakeResponse(500, long_body),
        _FakeResponse(200, '{"ok": true}'),
    )

    resp = mod.make_request("https://api.example.com/manga/abc", scraper)
    assert resp.status_code == 200
    assert scraper.get.call_count == 2


def test_504_with_body_retries():
    """504 Gateway Timeout — classified as retryable per
    _classify_response_failure. Same retry path."""
    mod = _aio_dl()
    long_body = "B" * 200
    scraper = _make_scraper(
        _FakeResponse(504, long_body),
        _FakeResponse(200, '{"ok": true}'),
    )

    resp = mod.make_request("https://example.com/api", scraper)
    assert resp.status_code == 200
    assert scraper.get.call_count == 2


def test_503_small_body_also_retries():
    """503 with < 100-char body: the prior code raised here via
    raise_for_status. New code raises explicitly via the 'retryable'
    branch — either way, the retry loop should engage. Verifies the
    fix didn't accidentally change the small-body path's behavior.
    """
    mod = _aio_dl()
    scraper = _make_scraper(
        _FakeResponse(503, "Service Unavailable"),  # <100 chars
        _FakeResponse(200, '{"ok": true}'),
    )

    resp = mod.make_request("https://example.com/api", scraper)
    assert resp.status_code == 200
    assert scraper.get.call_count == 2


# ---------------------------------------------------------------------------
# 4xx behavior preserved (permanent class).
# ---------------------------------------------------------------------------


def test_404_small_body_raises_immediately():
    """4xx with a tiny body still fails fast — the 'permanent' branch
    falls through to raise_for_status. The retry loop classifies the
    raised exception and the 'permanent' branch in the outer except
    doesn't retry."""
    mod = _aio_dl()
    scraper = _make_scraper(_FakeResponse(404, "Not Found"))

    with pytest.raises(requests.HTTPError):
        mod.make_request("https://example.com/missing", scraper)
    # 4xx is 'permanent' → no retries.
    assert scraper.get.call_count == 1


def test_400_with_body_returned_for_caller_inspection():
    """4xx with a JSON-shaped body (e.g. MangaDex's structured
    validation errors) is returned without raising so the caller can
    introspect the error code. This is the documented behavior of the
    'warning, continuing' branch and the fix preserves it for the
    permanent class."""
    mod = _aio_dl()
    structured_4xx_body = (
        '{"result": "error", "errors": [{"id": "validation_failed", '
        '"status": 400, "title": "Bad request", "detail": "manga_id required"}]}'
    )
    assert len(structured_4xx_body) > 100
    scraper = _make_scraper(_FakeResponse(400, structured_4xx_body))

    resp = mod.make_request("https://api.example.com/manga", scraper)
    assert resp.status_code == 400
    assert "validation_failed" in resp.text
    # No retries — 4xx is permanent.
    assert scraper.get.call_count == 1


# ---------------------------------------------------------------------------
# Success path unchanged.
# ---------------------------------------------------------------------------


def test_first_200_is_returned_without_retry():
    """A success on the first attempt doesn't trigger retry machinery —
    guards against accidentally setting up a wrong loop boundary."""
    mod = _aio_dl()
    scraper = _make_scraper(_FakeResponse(200, '{"hello": "world"}'))

    resp = mod.make_request("https://example.com/api", scraper)
    assert resp.status_code == 200
    assert scraper.get.call_count == 1
