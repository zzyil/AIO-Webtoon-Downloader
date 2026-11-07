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


def _isolated_probe_cache(tmp_path, **kw):
    """ProbeFailureCache that neither reads nor writes the user's real
    ~/.aio-dl/cache/probe_failures.json (its constructor loads that path and
    record_failure saves to it; a test host used to land in the real file)."""
    from sites.search_orchestrator import ProbeFailureCache
    cache = ProbeFailureCache(**kw)
    cache._state = {}
    cache._snapshot_path = str(tmp_path / "probe_failures.json")
    return cache


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


def test_search_all_handles_blocked_handlers(tmp_path):
    """When all eligible handlers are filtered out (e.g. probe-failure
    cache blocks every host), and no seed hits are provided, return []
    without spawning workers."""
    from sites.base import BaseSiteHandler

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
    cache = _isolated_probe_cache(tmp_path, threshold=1, ttl_s=3600)
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
# Official-publisher tiebreaker (2026-05-12), now step 3 of
# search_orchestrator.source_rank_key. Within a SeriesCandidate an
# OFFICIAL_PUBLISHER source in the title band (and itself >=
# IS_OFFICIAL_REQUIRES_TITLE_MATCH) ranks above aggregators regardless of
# measured quality. Closes the ********.com vs ******* case where the official
# publisher served PNGs at 720-800px and lost to toonily's upscaled JPEG.
# These tests call the shipped sort_sources (an earlier version tested a local
# replica of the comparator, which had drifted from the shipped one).
# ────────────────────────────────────────────────────────────────────────

def _sort_sources(sources):
    from sites.search_orchestrator import sort_sources
    out = list(sources)
    sort_sources(out)
    return out


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
    """linewebtoon (official, lower measured score) must outrank toonily
    (aggregator, upscaled JPEG) at equal title match."""
    linewebtoon = _make_source("linewebtoon", is_official=True,
                               seed_quality=0.92, img_quality_score=0.51)
    toonily = _make_source("toonily", is_official=False,
                           seed_quality=0.55, img_quality_score=0.60)
    result = _sort_sources([toonily, linewebtoon])
    assert result[0].site == "linewebtoon"
    assert result[1].site == "toonily"


def test_official_outside_the_title_band_does_not_jump_the_band():
    """The official preference applies inside the title band only: an official
    hit 0.15 below the best title match (TIEBREAKER_WINDOW is 0.10) ranks by
    title, so a weak-match official listing can't outrank the series itself."""
    linewebtoon = _make_source("linewebtoon", is_official=True,
                               title_match=0.85, seed_quality=0.92)
    toonily = _make_source("toonily", is_official=False,
                           title_match=1.00, seed_quality=0.55)
    result = _sort_sources([linewebtoon, toonily])
    assert result[0].site == "toonily"


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
    """Neither official, both unmeasured: seed_quality decides in the band."""
    mangadex = _make_source("mangadex", is_official=False,
                            title_match=1.0, seed_quality=0.93)
    toonily = _make_source("toonily", is_official=False,
                           title_match=1.0, seed_quality=0.55)
    result = _sort_sources([toonily, mangadex])
    assert result[0].site == "mangadex"


def test_two_officials_still_decided_by_quality():
    """Two official sources: the flag is a class tier, not a free pass, so
    quality decides among them."""
    off_hi = _make_source("a", is_official=True, seed_quality=0.92)
    off_lo = _make_source("b", is_official=True, seed_quality=0.55)
    result = _sort_sources([off_lo, off_hi])
    assert result[0].site == "a"


def test_unreachable_sinks_and_matched_outranks_unmeasured():
    """A source whose probe served nothing ranks last even with a high seed
    (its 0.0 is a reliability verdict); a matched rating outranks an
    unmeasured seed at equal title match (plan rev 3: unmatched after matched)."""
    dead = _make_source("dead", seed_quality=0.99, img_quality_score=0.0)
    dead.basis = "unreachable"
    seeded = _make_source("seeded", seed_quality=0.95)
    measured = _make_source("measured", seed_quality=0.40, img_quality_score=0.60)
    measured.basis = "matched"
    result = _sort_sources([dead, seeded, measured])
    assert [s.site for s in result] == ["measured", "seeded", "dead"]


def test_alternate_edition_ranks_after_original():
    colored = _make_source("colored", seed_quality=0.9, img_quality_score=0.95)
    colored.basis, colored.edition = "matched", "alternate"
    bw = _make_source("bw", seed_quality=0.5, img_quality_score=0.6)
    bw.basis, bw.edition = "matched", "original"
    assert [s.site for s in _sort_sources([colored, bw])] == ["bw", "colored"]


def test_tie_group_orders_by_coverage_not_rating():
    """Copies in one tie group are the same scan; their rating gap is noise,
    so chapter coverage orders them."""
    a = _make_source("a", img_quality_score=0.95)
    b = _make_source("b", img_quality_score=0.90)
    for s, cov in ((a, 100), (b, 120)):
        s.basis, s.tie_group, s.match_group, s.coverage = "matched", 0, 0, cov
    assert [s.site for s in _sort_sources([a, b])] == ["b", "a"]


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


# ────────────────────────────────────────────────────────────────────────
# SiteBusyError (2026-10-17). A site whose search was blocked by THIS machine —
# mangafire's browser profile held by another AIO process — must be reported as
# a "busy" notice, never as a failure: no ProbeFailureCache strike (that path
# blocklists the host for an hour after two), and no tested_sites entry (that
# would decay a real strike the UI is tracking). Cross-file: sites/base.py
# SiteBusyError, sites/mangafire.py:search, useDownloader.js strike fold.
# ────────────────────────────────────────────────────────────────────────

def test_busy_site_is_a_notice_not_a_failure(tmp_path):
    from sites.base import BaseSiteHandler, SearchHit, SiteBusyError

    class _Busy(BaseSiteHandler):
        name = "busysite"
        domains = ("busy.example",)

        def search(self, *a, **kw):
            raise SiteBusyError("profile held", reason="profile_busy")

    class _Ok(BaseSiteHandler):
        name = "oksite"
        domains = ("ok.example",)

        def search(self, query, *a, **kw):
            return [SearchHit(site=self.name, title=query, url="https://ok.example/s",
                              cover=None, alt_titles=[], year=None, language=None,
                              chapter_count_hint=5, raw_score=1.0)]

    cache = _isolated_probe_cache(tmp_path, threshold=1, ttl_s=3600)
    diagnostics = {}
    with patch("sites.iter_search_capable_handlers", lambda: iter([_Busy(), _Ok()])):
        with patch("sites.get_handler_by_name", lambda _: None):
            result = search_all(
                "Some Series",
                MagicMock(return_value=MagicMock()),
                MagicMock(),
                probe_failure_cache=cache,
                diagnostics=diagnostics,
            )

    assert [s.site for c in result for s in c.sources] == ["oksite"]
    busy = [h for h in diagnostics["site_health"] if h["site"] == "busysite"]
    assert len(busy) == 1 and busy[0]["status"] == "busy", diagnostics["site_health"]
    assert busy[0]["reason"] == "profile_busy"
    assert "busysite" not in diagnostics["tested_sites"]
    assert "oksite" in diagnostics["tested_sites"]
    assert cache.is_blocked("busy.example") is False, "contention struck the host"


# ────────────────────────────────────────────────────────────────────────
# Anchored admission (2026-10-17): a hit below the title-match floor joins a
# candidate when one of its titles has exactly the merge key of a title an
# ADMITTED hit carries. See the ANCHORED ADMISSION block in search_all.
# ────────────────────────────────────────────────────────────────────────

def _run_with_hits(query, per_site):
    """search_all over fake handlers; *per_site* maps site name -> list of
    (title, alt_titles). Returns the candidates."""
    from sites.base import BaseSiteHandler, SearchHit

    def _make(name, rows):
        class _H(BaseSiteHandler):
            def search(self, *a, **kw):
                return [
                    SearchHit(site=name, title=t, url=f"https://{name}.example/{i}",
                              cover=None, alt_titles=list(alts), year=None,
                              language=None, chapter_count_hint=5, raw_score=1.0)
                    for i, (t, alts) in enumerate(rows)
                ]
        _H.name = name
        _H.domains = (f"{name}.example",)
        return _H()

    handlers = [_make(n, rows) for n, rows in per_site.items()]
    with patch("sites.iter_search_capable_handlers", lambda: iter(handlers)):
        with patch("sites.get_handler_by_name", lambda _: None):
            return search_all(query, MagicMock(return_value=MagicMock()), MagicMock())


def _where(cands, site):
    """(candidate index, source) for *site*, or None."""
    for i, c in enumerate(cands):
        for s in c.sources:
            if s.site == site:
                return i, s
    return None


def test_all_caps_listing_joins_its_correctly_spelled_twin():
    # The floor's scorer is case-sensitive: "CHAINSAW MAN" scores 0.25.
    cands = _run_with_hits("Chainsaw Man", {
        "alpha": [("Chainsaw Man", [])],
        "beta": [("CHAINSAW MAN", [])],
    })
    a, b = _where(cands, "alpha"), _where(cands, "beta")
    assert b is not None, "the all-caps listing was dropped"
    assert a[0] == b[0], "anchored hit landed in a different candidate"
    assert b[1].title_match == a[1].title_match, "did not inherit the anchor's score"


def test_romaji_word_break_variant_is_anchored_by_an_alt_title():
    # mangahub's only title for Railgun; another site carries "Toaru" as an alt.
    cands = _run_with_hits("A Certain Scientific Railgun", {
        "alpha": [("A Certain Scientific Railgun", ["Toaru Kagaku no Railgun"])],
        "beta": [("To Aru Kagaku no Railgun", [])],
    })
    a, b = _where(cands, "alpha"), _where(cands, "beta")
    assert b is not None and a[0] == b[0]


def test_below_floor_hit_without_an_anchor_stays_out():
    cands = _run_with_hits("Chainsaw Man", {
        "alpha": [("Chainsaw Man", [])],
        "beta": [("Completely Different Story", [])],
    })
    assert _where(cands, "beta") is None


def test_anchored_hit_never_welds_two_candidates():
    # beta's title matches one admitted candidate and its alt another. It may
    # join ONE of them (the better-scoring) but must not merge the two.
    cands = _run_with_hits("Chainsaw Man", {
        "alpha": [("Chainsaw Man", []), ("Chainsaw Man Devil War", [])],
        "beta": [("CHAINSAW MAN", ["CHAINSAW MAN DEVIL WAR"])],
    })
    titles = sorted(c.canonical_title for c in cands if any(s.site == "alpha" for s in c.sources))
    assert titles == ["Chainsaw Man", "Chainsaw Man Devil War"], titles
    b = _where(cands, "beta")
    assert b is not None and cands[b[0]].canonical_title == "Chainsaw Man"


def test_anchored_hits_do_not_chain():
    # gamma matches only beta's alt; beta is itself only anchored, so gamma
    # has no admitted voucher.
    cands = _run_with_hits("Chainsaw Man", {
        "alpha": [("Chainsaw Man", [])],
        "beta": [("CHAINSAW MAN", ["Qwyx Blorpt"])],
        "gamma": [("QWYX BLORPT", [])],
    })
    assert _where(cands, "beta") is not None
    assert _where(cands, "gamma") is None


# ────────────────────────────────────────────────────────────────────────
# Verified zero count sinks (plan sync-temp/plans/mangadex-link-outs.md).
# MangaDex Goblin Slayer: 203 rows, all publisher link-outs; its search()
# counts with includeExternalUrl=0, so the count is 0. No peer hint or
# lastChapter is needed for the sink.
# ────────────────────────────────────────────────────────────────────────

def test_source_sinks_on_verified_zero_count_only():
    from sites.search_orchestrator import SourceEntry, _source_sinks

    def src(count):
        return SourceEntry(site="s", url="u", title="t", cover=None,
                           title_match=1.0, seed_quality=0.9,
                           actual_chapter_count=count)

    assert _source_sinks(src(0)) is True
    assert _source_sinks(src(None)) is False  # unmeasured never sinks
    assert _source_sinks(src(5)) is False


def test_zero_count_source_sinks_below_peers_without_a_peer_hint():
    """The zero-count site is an official publisher (would otherwise rank
    first) and no site exposes a chapter_count_hint, so the cross-site
    dmca/count-outlier check can't fire; the zero count alone sinks it."""
    from sites.base import BaseSiteHandler, SearchHit

    def _make(name, count, official):
        class _H(BaseSiteHandler):
            OFFICIAL_PUBLISHER = official

            def search(self, query, *a, **kw):
                return [SearchHit(site=name, title=query, url=f"https://{name}.example/s",
                                  cover=None, alt_titles=[], year=None, language=None,
                                  chapter_count_hint=None, raw_score=1.0,
                                  actual_chapter_count=count)]
        _H.name = name
        _H.domains = (f"{name}.example",)
        return _H()

    handlers = [_make("zerosite", 0, True), _make("realsite", 113, False)]
    with patch("sites.iter_search_capable_handlers", lambda: iter(handlers)):
        with patch("sites.get_handler_by_name", lambda _: None):
            cands = search_all("Goblin Slayer", MagicMock(return_value=MagicMock()), MagicMock())
    sites = [s.site for s in cands[0].sources]
    assert sites == ["realsite", "zerosite"]


def test_a_source_that_served_nothing_does_not_set_the_title_band():
    """best_title_of ignores unreachable sources: a dead exact-title hit must
    not push every live source out of band 0 (where quality and the official
    preference decide)."""
    from sites.search_orchestrator import best_title_of
    dead = _make_source("dead", title_match=1.0)
    dead.basis = "unreachable"
    live = _make_source("live", title_match=0.85)
    assert abs(best_title_of([dead, live]) - 0.85) < 1e-9


# ────────────────────────────────────────────────────────────────────────
# Other-name queries (2026-11-08): in a URL-seeded search (or with
# title_variants), a site whose search for the seed title found no exact match
# is searched again under the seed's ASCII alt names. Live case: MangaFire's
# "Monster Tale" is Bakemonogatari, which mangahub/mangapill/weebcentral list
# only under that name. See _search_other_names in search_all.
# ────────────────────────────────────────────────────────────────────────

def _hit(site, title, url, alts=()):
    from sites.base import SearchHit
    return SearchHit(site=site, title=title, url=url, cover=None,
                     alt_titles=list(alts), year=None, language=None,
                     chapter_count_hint=5, raw_score=1.0)


def _indexed_site(name, index):
    """Fake handler whose search returns index[query] (else []) and records
    every query it was sent on .queries."""
    from sites.base import BaseSiteHandler

    class _H(BaseSiteHandler):
        def search(self, query, *a, **kw):
            self.queries.append(query)
            rows = index.get(query, [])
            if isinstance(rows, Exception):
                raise rows
            return [_hit(name, t, f"https://{name}.example/{t.replace(' ', '-')}")
                    for t in rows]

    _H.name = name
    _H.domains = (f"{name}.example",)
    h = _H()
    h.queries = []
    return h


_MONSTER_SEED_ALTS = ["Bakemonogatari", "Истории монстров",
                      "Những Câu Chuyện Huyền Bí", "Monogatari Series", "Bake"]


def _monster_seed():
    return _hit("seedsite", "Monster Tale", "https://seed.example/monster-tale",
                alts=_MONSTER_SEED_ALTS)


def _search(handlers, query="Monster Tale", **kw):
    with patch("sites.iter_search_capable_handlers", lambda: iter(handlers)):
        with patch("sites.get_handler_by_name", lambda _: None):
            return search_all(query, MagicMock(return_value=MagicMock()),
                              MagicMock(), **kw)


def test_site_indexing_only_the_other_name_joins_the_seeded_candidate():
    site = _indexed_site("romajisite", {
        "Monster Tale": ["The Bride of a Monster"],
        "Bakemonogatari": ["Bakemonogatari"],
    })
    cands = _search([site], seed_hits=[_monster_seed()])
    where = _where(cands, "romajisite")
    seed_at = _where(cands, "seedsite")
    assert where is not None and seed_at is not None
    assert where[0] == seed_at[0], "recovered entry landed in another candidate"
    assert where[1].title == "Bakemonogatari"
    # Stops at the first name that yields an exact match.
    assert site.queries == ["Monster Tale", "Bakemonogatari"]


def test_exact_primary_match_is_not_searched_again():
    site = _indexed_site("englishsite", {"Monster Tale": ["Monster Tale"]})
    _search([site], seed_hits=[_monster_seed()])
    assert site.queries == ["Monster Tale"]


def test_only_ascii_names_are_sent_and_at_most_two():
    site = _indexed_site("emptysite", {})
    _search([site], seed_hits=[_monster_seed()])
    # Cyrillic and Vietnamese skipped; "Bake" is long enough to be a variant
    # but the cap of 2 stops before it.
    assert site.queries == ["Monster Tale", "Bakemonogatari", "Monogatari Series"]


def test_keyword_search_sends_only_the_query():
    site = _indexed_site("plainsite", {})
    _search([site], query="Bakemonogatari")
    assert site.queries == ["Bakemonogatari"]


def test_timing_out_primary_skips_the_other_names():
    site = _indexed_site("deadsite", {})
    # per_site_timeout_s=0: every primary counts as having hit the timeout.
    _search([site], seed_hits=[_monster_seed()], per_site_timeout_s=0.0)
    assert site.queries == ["Monster Tale"]


def test_failing_other_name_query_does_not_strike_the_host(tmp_path):
    site = _indexed_site("flakysite", {
        "Bakemonogatari": RuntimeError("search endpoint 500"),
        "Monogatari Series": ["Bakemonogatari"],
    })
    cache = _isolated_probe_cache(tmp_path, threshold=1, ttl_s=3600)
    diagnostics = {}
    cands = _search([site], seed_hits=[_monster_seed()],
                    probe_failure_cache=cache, diagnostics=diagnostics)
    assert site.queries == ["Monster Tale", "Bakemonogatari", "Monogatari Series"]
    assert _where(cands, "flakysite") is not None, "next name not tried"
    assert cache.is_blocked("flakysite.example") is False
    assert not [h for h in diagnostics["site_health"] if h["site"] == "flakysite"]


def test_title_variants_alone_drive_queries_and_scoring():
    """The direct-URL --multi-source path has no seed hit, only the primary
    page's alt_names (aio_search_cli passes them as title_variants)."""
    site = _indexed_site("romajisite", {"Bakemonogatari": ["Bakemonogatari"]})
    cands = _search([site], title_variants=["Bakemonogatari"])
    assert site.queries == ["Monster Tale", "Bakemonogatari"]
    where = _where(cands, "romajisite")
    assert where is not None and where[1].title_match >= 1.0


def test_primary_alt_names_reads_the_handler_context():
    from types import SimpleNamespace
    from aio_search_cli import _primary_alt_names

    assert _primary_alt_names(SimpleNamespace(comic={"alt_names": ["A b", " ", 3]})) == ["A b"]
    assert _primary_alt_names(SimpleNamespace(comic={"alt_names": "Solo"})) == ["Solo"]
    assert _primary_alt_names(SimpleNamespace(comic={})) == []
    assert _primary_alt_names(SimpleNamespace(comic=None)) == []
    assert _primary_alt_names(object()) == []
