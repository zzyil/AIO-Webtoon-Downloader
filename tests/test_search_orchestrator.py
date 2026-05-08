"""Tests for sites/search_orchestrator.py — particularly the audit fix
that moved `all_hits` initialization to BEFORE the `if not eligible and
not all_hits:` early-return.

Cross-file: search_all is invoked by aio_search_cli.run_search_mode when
the user passes --search. The early-return path triggers when every
search-capable handler is filtered out by --seeded-only or the probe-
failure cache, and the user provided no URL-mode seed hits.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from sites.search_orchestrator import search_all


def _empty_handler_iter():
    """Yields no handlers — simulates --seeded-only filtering everything."""
    return iter([])


def test_search_all_returns_empty_when_no_eligible_and_no_seed():
    """Reproduces the audit-flagged NameError scenario.

    When iter_search_capable_handlers returns nothing AND seed_hits is
    None, the early-return at search_orchestrator.py:940 must evaluate
    `not all_hits` without crashing. Pre-fix, all_hits was defined on
    line 943 (AFTER the check) so this raised
        NameError: name 'all_hits' is not defined
    """
    with patch("sites.iter_search_capable_handlers", _empty_handler_iter):
        with patch("sites.get_handler_by_name", lambda _: None):
            scraper_factory = MagicMock(return_value=MagicMock())
            make_request = MagicMock()
            result = search_all(
                "anything",
                scraper_factory,
                make_request,
                seed_hits=None,
            )
            assert result == []
            # scraper_factory should never be called when no handlers
            # are eligible and no seed hits.
            scraper_factory.assert_not_called()


def test_search_all_returns_empty_when_no_eligible_and_empty_seed():
    """Same as above but with seed_hits=[] (explicit empty rather than
    None) — exercise the `list(seed_hits) if seed_hits else []` branch
    on the truthy-empty side."""
    with patch("sites.iter_search_capable_handlers", _empty_handler_iter):
        with patch("sites.get_handler_by_name", lambda _: None):
            scraper_factory = MagicMock(return_value=MagicMock())
            make_request = MagicMock()
            result = search_all(
                "anything",
                scraper_factory,
                make_request,
                seed_hits=[],
            )
            assert result == []


def test_search_all_processes_seed_hits_when_no_eligible():
    """When eligible is empty BUT seed_hits is non-empty, the search
    should NOT short-circuit — the seed hits are scored and emitted."""
    from sites.base import SearchHit

    seed = SearchHit(
        site="mangadex",
        title="Test Series",
        url="https://mangadex.org/title/abcd",
        cover=None,
        alt_titles=[],
        year=None,
        language=None,
        chapter_count_hint=10,
        raw_score=1.0,
    )
    with patch("sites.iter_search_capable_handlers", _empty_handler_iter):
        with patch("sites.get_handler_by_name", lambda _: None):
            scraper_factory = MagicMock(return_value=MagicMock())
            make_request = MagicMock()
            result = search_all(
                "Test Series",
                scraper_factory,
                make_request,
                seed_hits=[seed],
                # Pass a high min_match to keep this test focused on the
                # early-return path. The seed's title matches the query
                # exactly so rapidfuzz scores 1.0.
                min_match=0.5,
            )
            # We get one candidate with one source (the seed itself).
            assert len(result) == 1
            assert len(result[0].sources) == 1
            assert result[0].sources[0].site == "mangadex"


def test_search_all_handles_blocked_handlers():
    """When all eligible handlers are filtered out (e.g. probe-failure
    cache blocks every host), and no seed hits are provided, return []
    without spawning workers."""
    from sites.base import BaseSiteHandler
    from sites.search_orchestrator import ProbeFailureCache

    # Mock handler whose primary domain is "blocked.example".
    class _MockHandler(BaseSiteHandler):
        name = "mocked"
        domains = ("blocked.example",)
        def search(self, *args, **kwargs):
            return []
        def fetch_comic_context(self, *a, **kw): pass
        def get_chapters(self, *a, **kw): return []
        def get_chapter_images(self, *a, **kw): return []
        def get_group_name(self, *a, **kw): return None

    handler = _MockHandler()
    cache = ProbeFailureCache(threshold=1, ttl_s=3600)
    cache.record_failure("blocked.example")  # one failure, threshold=1

    with patch(
        "sites.iter_search_capable_handlers",
        lambda: iter([handler]),
    ):
        with patch("sites.get_handler_by_name", lambda _: None):
            scraper_factory = MagicMock(return_value=MagicMock())
            make_request = MagicMock()
            result = search_all(
                "anything",
                scraper_factory,
                make_request,
                seed_hits=None,
                probe_failure_cache=cache,
            )
            assert result == []
            # Handler was filtered — search() should NOT have been invoked.
            scraper_factory.assert_not_called()
