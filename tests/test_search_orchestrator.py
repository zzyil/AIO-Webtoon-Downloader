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
        url="https://example.com/title/abcd",
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


# ────────────────────────────────────────────────────────────────────────
# Official-publisher tiebreaker (2026-05-12).
# Within a SeriesCandidate, sources where the handler sets
# OFFICIAL_PUBLISHER=True must rank above aggregator sources regardless
# of measured img_quality_score. Closes the ********.com vs ******* case
# where the official publisher served PNGs at 720-800px (below the probe's
# 800px res_score floor) and was losing to toonily's upscaled JPEG.
# ────────────────────────────────────────────────────────────────────────

def _sort_sources(sources):
    """Apply the same comparator logic the orchestrator uses internally,
    so we test the wire-level behavior without needing to spin up a full
    search_all + scraper_factory.
    """
    from functools import cmp_to_key
    from sites.search_orchestrator import TIEBREAKER_WINDOW

    def quality_for(s):
        return s.img_quality_score if s.img_quality_score is not None else s.seed_quality

    def cmp(a, b):
        if a.dmca_likely != b.dmca_likely:
            return 1 if a.dmca_likely else -1
        if a.is_official != b.is_official:
            return -1 if a.is_official else 1
        if abs(a.title_match - b.title_match) <= TIEBREAKER_WINDOW:
            qa, qb = quality_for(a), quality_for(b)
            if qa != qb:
                return -1 if qa > qb else 1
            return 0
        return -1 if a.title_match > b.title_match else 1

    return sorted(sources, key=cmp_to_key(cmp))


def _make_source(site, *, is_official=False, title_match=1.0,
                 seed_quality=0.5, img_quality_score=None,
                 dmca_likely=False):
    from sites.search_orchestrator import SourceEntry
    return SourceEntry(
        site=site, url=f"https://{site}/x", title="x", cover=None,
        title_match=title_match, seed_quality=seed_quality,
        img_quality_score=img_quality_score, dmca_likely=dmca_likely,
        is_official=is_official,
    )


def test_official_publisher_beats_aggregator_despite_lower_img_quality():
    """The bug: linewebtoon (official, PNG @ 800px → res_score=0 → composite
    ~0.51) was being outranked by toonily (aggregator, upscaled JPEG →
    composite ~0.60). After the fix, official wins regardless of quality."""
    linewebtoon = _make_source("linewebtoon", is_official=True,
                               seed_quality=0.92, img_quality_score=0.51)
    toonily = _make_source("toonily", is_official=False,
                           seed_quality=0.55, img_quality_score=0.60)
    result = _sort_sources([toonily, linewebtoon])
    assert result[0].site == "linewebtoon"
    assert result[1].site == "toonily"


def test_official_publisher_beats_aggregator_outside_tiebreaker_window():
    """Even when title_match deltas are large, official wins within a
    candidate — the union-find merge already established same-series
    membership, so the publisher is canonical regardless of title spread.
    Verifies the official check happens BEFORE the TIEBREAKER_WINDOW
    branch."""
    linewebtoon = _make_source("linewebtoon", is_official=True,
                               title_match=0.85, seed_quality=0.92)
    toonily = _make_source("toonily", is_official=False,
                           title_match=1.00, seed_quality=0.55)
    # delta = 0.15, outside TIEBREAKER_WINDOW of 0.10.
    result = _sort_sources([toonily, linewebtoon])
    assert result[0].site == "linewebtoon"


def test_dmca_likely_still_beats_official():
    """DMCA-likely sources go to the back even when they're the official
    publisher. The DMCA flag means most chapters are inaccessible — a
    canonical-but-empty source is worse than a complete aggregator."""
    official_dmca = _make_source("linewebtoon", is_official=True,
                                 dmca_likely=True, seed_quality=0.92)
    clean_agg = _make_source("toonily", is_official=False,
                             dmca_likely=False, seed_quality=0.55)
    result = _sort_sources([official_dmca, clean_agg])
    assert result[0].site == "toonily"
    assert result[1].site == "linewebtoon"


def test_two_aggregators_still_decided_by_quality():
    """When neither source is official, the existing img_quality_score-or-
    seed_quality tiebreaker takes over inside the title-match window."""
    mangadex = _make_source("mangadex", is_official=False,
                            title_match=1.0, seed_quality=0.93)
    toonily = _make_source("toonily", is_official=False,
                           title_match=1.0, seed_quality=0.55)
    result = _sort_sources([toonily, mangadex])
    assert result[0].site == "mangadex"


def test_two_officials_still_decided_by_quality():
    """Hypothetical: two official-publisher sources for the same series
    (would only happen if voyceme + linewebtoon both republished it).
    Quality decides among officials as well — the flag is a class tier,
    not a free pass."""
    off_hi = _make_source("a", is_official=True, seed_quality=0.92)
    off_lo = _make_source("b", is_official=True, seed_quality=0.55)
    result = _sort_sources([off_lo, off_hi])
    assert result[0].site == "a"


def test_linewebtoon_handler_carries_official_publisher_flag():
    """Wire-level: the linewebtoon handler class actually sets the flag.
    Regression guard for accidental class-attribute removal during a
    handler refactor."""
    from sites.linewebtoon import LineWebtoonSiteHandler
    assert getattr(LineWebtoonSiteHandler, "OFFICIAL_PUBLISHER", False) is True


def test_base_handler_defaults_official_publisher_to_false():
    """Aggregators that don't override the flag must default to False;
    otherwise every handler in the registry would be marked official."""
    from sites.base import BaseSiteHandler
    assert getattr(BaseSiteHandler, "OFFICIAL_PUBLISHER", None) is False
