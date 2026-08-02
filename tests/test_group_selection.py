"""Chapter-version (scanlation group) selection + MTL detection.

Covers sites/base.py's composite ranker (select_best_chapter_version /
_rank_version / build_group_census), sites/group_quality.py's MTL classifier,
and the per-handler group extraction that feeds them.

Each test names the defect it pins. The headline ones:
  - The ranker used to be `max(versions, key=lambda v: v.get("up_count", 0))`.
    Only 3 of 11 group-bearing handlers ever set `up_count`, so for the other
    8 the max() over all-zeros returned versions[0] — the winner was whatever
    order the site's API happened to answer in.
  - "Missing" and "zero" were the same value, so a site reporting no votes
    lost to one reporting a real count.
  - There was no MTL notion anywhere, so a machine translation with more
    upvotes beat a human one.
"""

from __future__ import annotations

import pytest

from sites.base import (
    BaseSiteHandler,
    GroupInfo,
    GroupSelectionPolicy,
    build_group_census,
)
from sites.group_quality import (
    MTL_CONFIRMED,
    MTL_NONE,
    MTL_SUSPECT,
    classify_mtl,
)


@pytest.fixture
def handler():
    return BaseSiteHandler()


def _v(chap="1", groups=None, **extra):
    """A minimal chapter-version dict."""
    version = {"chap": chap}
    if groups is not None:
        version["_groups"] = [
            g if isinstance(g, GroupInfo) else GroupInfo(name=g) for g in groups
        ]
    version.update(extra)
    return version


# ---------------------------------------------------------------- MTL regex


@pytest.mark.parametrize("name", [
    "MTL Sans", "100% MTL", "MTL-Scans", "MTL_Scans", "mtl.scans",
    "Triiple MTL", "Codesmith MTL Scans", "Regrettably using MTL",
    "Machine Translated", "Machiner Translate", "MachineTranslated",
    "Open Machine Translations", "skroderider machine translations",
    "AI Translations", "AI-TL", "AI tl", "AI_Translated",
    "NEET-GPT 1.0", "DeepL Diaries", "Google Translate Gang",
])
def test_confirmed_mtl_names(name):
    assert classify_mtl(name)[0] == MTL_CONFIRMED


@pytest.mark.parametrize("name", [
    # "AI" is the entire false-positive surface. 愛 romanizes as `Ai`, never
    # `AI`, so the bare-token pattern is case-sensitive against the raw name.
    "Aiden Scans", "Ai", "Rainbow Ai", "Aiko TL", "Ai Translations",
    # Word boundaries must hold.
    "Formatl", "Botan Scans", "Kaiju Translations",
    # "machine" alone is not a method claim.
    "Machine Doll Translations",
    # Ordinary groups.
    "Alpha", "MangaPlus", "LINE Webtoon", "Tsuki Translations",
])
def test_clean_names_are_not_flagged(name):
    assert classify_mtl(name)[0] == MTL_NONE


def test_bare_ai_token_is_suspect_not_confirmed():
    """Plausible as an acronym, so it demotes one tier but is never excluded."""
    assert classify_mtl("AI Scans")[0] == MTL_SUSPECT
    assert classify_mtl("AI_Scans")[0] == MTL_SUSPECT


def test_description_promotes_an_innocuously_named_group():
    """MangaDex exposes a group blurb; MTL shops routinely admit it there."""
    assert classify_mtl("Impatient Scans")[0] == MTL_NONE
    assert classify_mtl(
        "Impatient Scans", description="Uploading MTL chapters of dead series"
    )[0] == MTL_CONFIRMED


def test_description_does_not_promote_on_loose_words():
    """'bot' in prose is routine; only the CONFIRMED patterns read descriptions."""
    assert classify_mtl(
        "Real Group", description="our bot posts releases to Discord"
    )[0] == MTL_NONE


def test_empty_and_none_names():
    for value in (None, "", "   "):
        assert classify_mtl(value)[0] == MTL_NONE


# ------------------------------------------------------- canonical group model


def test_legacy_string_keys_are_read_in_precedence_order(handler):
    """Handlers that predate `_groups` must keep working unchanged."""
    for key in ("group_name", "group", "scanlator", "publisher"):
        assert handler.get_group_name({key: "Alpha"}) == "Alpha"
    assert handler.get_group_name({}) is None


def test_groups_list_wins_over_legacy_string(handler):
    version = _v(groups=["A", "B"], group_name="Legacy")
    assert handler.get_group_name(version) == "A, B"


def test_normalize_no_longer_collapses_official_like_names(handler):
    """The old normalizer rewrote any name matching
    \\b(official|webtoons?|naver)\\b to the literal "official", which merged
    LINE Originals with Canvas and made unrelated fan groups matchable by
    `--group official`."""
    assert (
        handler.get_group_match_key("LINE Webtoon")
        != handler.get_group_match_key("LINE Webtoon Canvas")
    )
    for name in ("LINE Webtoon", "Webtoon Scans", "Naver fan TL"):
        assert handler.get_group_match_key(name) != "official"


def test_group_official_matches_the_flag_not_the_name(handler):
    official = [GroupInfo(name="MangaPlus", is_official=True)]
    fan = [GroupInfo(name="Webtoon Scans")]
    assert handler.group_matches_filter(official, "official") is True
    assert handler.group_matches_filter(fan, "official") is False
    # …and a fan group still matches its own name.
    assert handler.group_matches_filter(
        fan, handler.get_group_match_key("Webtoon Scans")
    ) is True


# ------------------------------------------------------------- the ranker


def test_all_zero_upvotes_does_not_blindly_take_the_first_version(handler):
    """The original defect: max() over all-zero up_count returned versions[0]."""
    versions = [_v(groups=["MTL Sans"], id="first"), _v(groups=["Alpha"], id="second")]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "second"


def test_missing_upvote_is_not_treated_as_zero(handler):
    """A version with no up_count key must not lose to one reporting 5.

    Only one version reports, so the tier can't discriminate and goes inert.
    """
    versions = [_v(groups=["A"], id="no_key"), _v(groups=["B"], up_count=5, id="has_5")]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "no_key"


def test_upvotes_decide_only_when_comparable(handler):
    versions = [
        _v(groups=["A"], up_count=10, id="low"),
        _v(groups=["B"], up_count=5000, id="high"),
    ]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "high"


def test_upvote_bands_ignore_noise(handler):
    """412 vs 408 is noise; log2 banding ties them and falls through."""
    versions = [
        _v(groups=["A"], up_count=412, id="a"),
        _v(groups=["B"], up_count=408, id="b"),
    ]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "a"


def test_mtl_loses_to_human_even_with_far_more_upvotes(handler):
    """The user-reported symptom, verbatim."""
    versions = [
        _v(groups=["MTL Sans"], up_count=9999, id="mtl"),
        _v(groups=["Real Scans"], up_count=3, id="human"),
    ]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "human"


def test_mtl_only_chapter_is_still_downloaded_by_default(handler):
    """`--mtl avoid` demotes; it must never cost you a chapter."""
    versions = [_v(groups=["MTL Sans"], id="only")]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "only"


def test_mtl_exclude_skips_a_confirmed_only_chapter(handler):
    policy = GroupSelectionPolicy(mtl="exclude")
    versions = [_v(groups=["MTL Sans"])]
    assert handler.select_best_chapter_version(
        versions, [], False, selection_policy=policy
    ) is None


def test_mtl_exclude_never_drops_a_merely_suspect_version(handler):
    """Hard-dropping real chapters on a heuristic is not a trade we make."""
    policy = GroupSelectionPolicy(mtl="exclude")
    versions = [_v(groups=["AI Scans"], id="suspect")]
    assert handler.select_best_chapter_version(
        versions, [], False, selection_policy=policy
    )["id"] == "suspect"


def test_mtl_allow_ignores_the_signal(handler):
    policy = GroupSelectionPolicy(mtl="allow")
    versions = [_v(groups=["MTL Sans"], id="mtl"), _v(groups=["Human"], id="human")]
    # With the MTL tier inert both tie down to source order.
    assert handler.select_best_chapter_version(
        versions, [], False, selection_policy=policy
    )["id"] == "mtl"


def test_undownloadable_external_chapter_loses(handler):
    """MangaDex licensed chapters are pages:0 + externalUrl — metadata, not a
    chapter. Ranking one first trades a working download for a guaranteed
    'chapter has no pages' abort."""
    versions = [
        _v(groups=[GroupInfo(name="MangaPlus", is_official=True)],
           _undownloadable=True, id="external"),
        _v(groups=["Fan Scans"], id="fan"),
    ]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "fan"


def test_undownloadable_still_wins_when_it_is_the_only_version(handler):
    """Rank down, never filter out — One Piece's entire English run is this
    shape, and filtering would empty the series."""
    versions = [_v(groups=["MangaPlus"], _undownloadable=True, id="external")]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "external"


def test_official_beats_fan_when_both_are_downloadable(handler):
    versions = [
        _v(groups=["Fan Scans"], id="fan"),
        _v(groups=[GroupInfo(name="MangaPlus", is_official=True)], id="official"),
    ]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "official"


# --------------------------------------------------------------- page band


def test_page_band_does_not_punish_webtoon_slicing(handler):
    """Solo Leveling ch.1 on atsumaru is 22/22/19/14 pages across four groups
    and all four are complete — only the slicing differs."""
    versions = [_v(groups=[f"G{i}"], _pages=p, id=f"p{i}")
                for i, p in enumerate([22, 22, 19, 14])]
    # Nothing above discriminates, so the stable tiebreak takes the first.
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "p0"


def test_page_band_rejects_a_genuine_stub(handler):
    versions = [
        _v(groups=["Stub"], _pages=3, id="stub"),
        _v(groups=["A"], _pages=22, id="a"),
        _v(groups=["B"], _pages=19, id="b"),
    ]
    assert handler.select_best_chapter_version(versions, [], False)["id"] != "stub"


def test_page_band_is_inert_on_short_chapters(handler):
    """A 4-koma median can't tell a stub from a short chapter, so don't try."""
    versions = [_v(groups=["A"], _pages=4, id="a"), _v(groups=["B"], _pages=3, id="b")]
    assert handler.select_best_chapter_version(versions, [], False)["id"] == "a"


# ------------------------------------------------------------ track record


def _series(main_group, main_count, filler_group=None, filler_count=0):
    by_num = {}
    for i in range(1, main_count + 1):
        versions = [_v(chap=str(i), groups=[main_group])]
        if filler_group and i <= filler_count:
            versions.append(_v(chap=str(i), groups=[filler_group]))
        by_num[str(i)] = versions
    return by_num


def test_census_counts_distinct_chapter_numbers(handler):
    """A group that re-uploaded ch.5 three times must not earn 3x credit."""
    by_num = {"5": [_v(chap="5", groups=["A"]) for _ in range(3)]}
    census, total = build_group_census(handler, by_num)
    assert census == {"a": 1}
    assert total == 1


def test_long_run_group_beats_a_filler_dump(handler):
    by_num = _series("Alpha", 201, "Filler", 12)
    census, total = build_group_census(handler, by_num)
    policy = GroupSelectionPolicy(census=census, census_total=total)
    # Filler placed FIRST, so source order alone would pick it.
    versions = [_v(groups=["Filler"], id="filler"), _v(groups=["Alpha"], id="alpha")]
    winner = handler.select_best_chapter_version(
        versions, [], False, selection_policy=policy
    )
    assert winner["id"] == "alpha"
    assert winner["_group_selection"]["why"] == "track-record"


def test_two_full_run_groups_tie_and_fall_through(handler):
    """The census separates real-vs-filler; it must stay silent on two real TLs."""
    by_num = {str(i): [_v(chap=str(i), groups=["A"]), _v(chap=str(i), groups=["B"])]
              for i in range(1, 101)}
    census, total = build_group_census(handler, by_num)
    policy = GroupSelectionPolicy(census=census, census_total=total)
    versions = [
        _v(groups=["A"], uploaded=100, id="older"),
        _v(groups=["B"], uploaded=200, id="newer"),
    ]
    winner = handler.select_best_chapter_version(
        versions, [], False, selection_policy=policy
    )
    assert winner["id"] == "newer"
    assert winner["_group_selection"]["why"] == "recency"


# ------------------------------------------------------------ user filters


def test_named_group_overrides_every_automatic_signal(handler):
    """The escape hatch for an MTL false positive: name it and it's used."""
    versions = [_v(groups=["Real Scans"], id="real"), _v(groups=["MTL Sans"], id="mtl")]
    winner = handler.select_best_chapter_version(versions, ["MTL Sans"], False)
    assert winner["id"] == "mtl"
    assert winner["_group_selection"]["kind"] == "preferred_priority"


def test_priority_order_is_respected(handler):
    versions = [_v(groups=["A"], id="a"), _v(groups=["B"], id="b")]
    assert handler.select_best_chapter_version(versions, ["B", "A"], False)["id"] == "b"
    assert handler.select_best_chapter_version(versions, ["A", "B"], False)["id"] == "a"


def test_no_group_fallback_skips_when_absent(handler):
    versions = [_v(groups=["A"]), _v(groups=["B"])]
    assert handler.select_best_chapter_version(
        versions, ["Nobody"], False, allow_group_fallback=False
    ) is None


def test_multi_group_chapter_matches_on_one_member(handler):
    """dynasty comma-joined its scanlators into one opaque key, so `--group X`
    could never match a chapter released as "X, Y"."""
    versions = [_v(groups=["X", "Y"], id="xy"), _v(groups=["Z"], id="z")]
    assert handler.select_best_chapter_version(versions, ["X"], False)["id"] == "xy"
    assert handler.get_group_name(versions[0]) == "X, Y"


def test_exclude_group_demotes(handler):
    policy = GroupSelectionPolicy(
        excluded_keys=frozenset({BaseSiteHandler().get_group_match_key("Bad")})
    )
    versions = [_v(groups=["Bad"], id="bad"), _v(groups=["Good"], id="good")]
    assert handler.select_best_chapter_version(
        versions, [], False, selection_policy=policy
    )["id"] == "good"


def test_exclude_group_still_used_when_it_is_the_only_option(handler):
    """Matches the existing allow_group_fallback contract — pair it with
    --no-group-fallback to skip instead."""
    policy = GroupSelectionPolicy(
        excluded_keys=frozenset({BaseSiteHandler().get_group_match_key("Bad")})
    )
    versions = [_v(groups=["Bad"], id="bad")]
    assert handler.select_best_chapter_version(
        versions, [], False, selection_policy=policy
    )["id"] == "bad"


def test_mix_by_upvote_ranks_across_the_preferred_union(handler):
    versions = [
        _v(groups=["A"], up_count=10, id="a"),
        _v(groups=["B"], up_count=500, id="b"),
        _v(groups=["C"], up_count=9999, id="c"),
    ]
    winner = handler.select_best_chapter_version(versions, ["A", "B"], True)
    assert winner["id"] == "b"  # C is not a preferred group


# ------------------------------------------------------------ misc contracts


def test_selection_annotation_does_not_mutate_the_input(handler):
    versions = [_v(groups=["A"], id="a")]
    handler.select_best_chapter_version(versions, [], False)
    assert "_group_selection" not in versions[0]


def test_duplicate_equal_versions_do_not_corrupt_the_tiebreak(handler):
    """The stable -index tiebreak is identity-keyed; list.index() compares by
    equality and would map both duplicates to slot 0."""
    versions = [_v(groups=["A"]), _v(groups=["A"])]
    assert handler.select_best_chapter_version(versions, [], False) is not None


def test_empty_version_list(handler):
    assert handler.select_best_chapter_version([], [], False) is None


def test_no_handler_overrides_the_selector():
    """Per-site behavior belongs in `_groups`, not in a reimplemented policy.
    Ten handlers used to carry a near-identical get_group_name override."""
    import sites

    overrides = [
        type(h).__name__
        for h in sites._BASE_HANDLERS
        if type(h).select_best_chapter_version
        is not BaseSiteHandler.select_best_chapter_version
        or type(h).get_group_name is not BaseSiteHandler.get_group_name
    ]
    assert overrides == []


# ------------------------------------------------- per-handler extraction
# Offline fixtures shaped like the real API payloads (verified live
# 2026-08-02). These pin the parse, not the network.


def test_atsumaru_joins_scanlator_ids_to_names_and_carries_pagecount():
    """atsu.moe names a chapter's group by id only; the id->name map lives on
    the mangaPage payload. /api/manga/allChapters carries scanlationMangaId +
    pageCount, which is why it is the primary path — the paginated
    /api/manga/chapters endpoint has NEITHER (and returned 601 of 802 rows)."""
    from sites.atsumaru import AtsumaruSiteHandler

    h = AtsumaruSiteHandler()
    entry = {
        "id": "EEokDDLd", "scanlationMangaId": "cmgzlqc5aeuq3m191ynp3h67w",
        "title": "Chapter 1", "number": 1, "createdAt": 1784917003396,
        "index": 0, "pageCount": 22,
    }
    scanlators = {"cmgzlqc5aeuq3m191ynp3h67w": "Alpha", "cml2g1rs5000701pgm0b4uf46": "Asura"}
    ch = h._parse_chapter_entry("oZOG5", entry, scanlators=scanlators)
    assert ch["group_name"] == "Alpha"
    assert ch["_groups"][0].group_id == "cmgzlqc5aeuq3m191ynp3h67w"
    assert ch["_pages"] == 22
    assert ch["chap"] == "1"


def test_atsumaru_unknown_scanlator_id_still_separates_versions():
    """An id absent from the map must not collapse two versions into one
    census bucket — keep the id as the name."""
    from sites.atsumaru import AtsumaruSiteHandler

    h = AtsumaruSiteHandler()
    entry = {"id": "x", "scanlationMangaId": "unmapped", "number": 1, "pageCount": 5}
    ch = h._parse_chapter_entry("slug", entry, scanlators={})
    assert ch["_groups"][0].group_id == "unmapped"
    assert ch["group_name"] == "unmapped"


def test_atsumaru_paginated_entry_without_scanlator_id_has_no_group():
    from sites.atsumaru import AtsumaruSiteHandler

    h = AtsumaruSiteHandler()
    entry = {"id": "PwTgD1Gh", "title": "Chapter 200", "number": 200,
             "index": 201, "pageCount": 49, "createdAt": "2025-07-06T23:58:47.498Z"}
    ch = h._parse_chapter_entry("oZOG5", entry, scanlators={"a": "Alpha"})
    assert ch["_groups"] == []
    assert ch["group_name"] is None
    assert ch["_pages"] == 49


def test_dynasty_emits_one_groupinfo_per_scanlator():
    from sites.dynasty import DynastySiteHandler

    h = DynastySiteHandler()
    tags = [
        {"type": "Scanlator", "name": "X"},
        {"type": "Author", "name": "Someone"},
        {"type": "Scanlator", "name": "Y"},
    ]
    assert h._chapter_scanlators(tags) == ["X", "Y"]


def test_kagane_keeps_every_group_not_just_the_first():
    """Fully offline: get_chapters reads the v2 payload stashed on the context
    by fetch_comic_context, so no network is involved."""
    from sites.base import SiteComicContext
    from sites.kagane import KaganeSiteHandler

    payload = {"series_books": [{
        "book_id": "b1", "chapter_no": "5", "title": "T", "page_count": 20,
        "published_on": 1700000000,
        "groups": [{"group_id": "g1", "title": "First"},
                   {"group_id": "g2", "title": "Second"}],
    }]}
    ctx = SiteComicContext(
        comic={"_series_id": "s1", "_v2_series_payload": payload},
        title="T", identifier="s1", soup=None,
    )
    h = KaganeSiteHandler()
    chapters = h.get_chapters(ctx, None, "en", None)
    assert len(chapters) == 1
    assert [g.name for g in chapters[0]["_groups"]] == ["First", "Second"]
    assert chapters[0]["group_name"] == "First, Second"
    assert chapters[0]["_pages"] == 20
    # And the co-group is matchable, which `groups[0]` made impossible.
    assert h.group_matches_filter(
        h.get_group_infos(chapters[0]), h.get_group_match_key("Second")
    ) is True


def _code_without_comments(src: str) -> str:
    """Strip whole-line `#` comments so a source assertion can't be satisfied
    (or defeated) by prose describing the very code it checks for."""
    return "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )


def test_weebcentral_no_longer_fabricates_a_group():
    """The site credits no scanlator anywhere in its chapter markup. The old
    svg[stroke] derivation matched nothing on current markup (it's an <img>
    now), and the "Official" string it used to synthesize would now be read as
    a genuine is_official signal by group_matches_filter."""
    import inspect
    from sites.weebcentral import WeebCentralSiteHandler

    code = _code_without_comments(
        inspect.getsource(WeebCentralSiteHandler.get_chapters)
    )
    assert "svg[stroke]" not in code
    assert "#d8b4fe" not in code
    assert '"scanlator"' not in code


def test_linewebtoon_originals_and_canvas_stay_distinct(handler):
    """The old normalizer collapsed both to "official"."""
    assert (
        handler.get_group_match_key("LINE Webtoon")
        != handler.get_group_match_key("LINE Webtoon Canvas")
    )
