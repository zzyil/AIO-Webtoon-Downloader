"""Tests for sites/chapter_merger.py — particularly the post-audit Rule 6
label change and the surrounding rule table.

Cross-file: chapter_merger.group_chapters_for_download is consumed by
aio-dl.py:main() between chapter selection and the download loop. The
synthesized merged-chapter dicts (`_merged_parts` set on rule 5) feed
into _process_chapter_impl's part-by-part fetch path.
"""

from __future__ import annotations

from sites.chapter_merger import (
    AlignmentResult,
    ChapterBreakdown,
    ChapterGroup,
    _classify_chapter_breakdown,
    _classify_main_chapters,
    _format_chapter_label,
    _extract_chapter_num,
    _is_fragment_shaped_decimal,
    _is_source_only_fragment,
    align_chapter_lists,
    group_chapters_for_download,
)


def _ch(label):
    """Make a minimal chapter dict with just a `chap` field. The merger
    operates on `chap` only; other fields are passed through opaquely."""
    return {"chap": label}


# ────────────────────────────────────────────────────────────────────────
# _format_chapter_label
# ────────────────────────────────────────────────────────────────────────

def test_format_chapter_label_strips_trailing_zero_for_integer():
    assert _format_chapter_label(1.0) == "1"


def test_format_chapter_label_keeps_decimal_for_partial():
    assert _format_chapter_label(1.5) == "1.5"


def test_format_chapter_label_strips_trailing_zeros():
    # 1.50 → "1.5" (rstrip "0" then rstrip "."); 1.0 → "1"
    assert _format_chapter_label(1.50) == "1.5"
    assert _format_chapter_label(2.10) == "2.1"


# ────────────────────────────────────────────────────────────────────────
# _extract_chapter_num
# ────────────────────────────────────────────────────────────────────────

def test_extract_chapter_num_simple():
    assert _extract_chapter_num("Chapter 47") == 47.0


def test_extract_chapter_num_decimal():
    assert _extract_chapter_num("Ch 47.5") == 47.5


def test_extract_chapter_num_padded():
    assert _extract_chapter_num("047") == 47.0


def test_extract_chapter_num_takes_first_token():
    # Documented limitation: "Vol 5 Ch 47" → 5.0 (first numeric token).
    assert _extract_chapter_num("Vol 5 Ch 47") == 5.0


def test_extract_chapter_num_unparseable_returns_none():
    assert _extract_chapter_num("oneshot") is None
    assert _extract_chapter_num("") is None
    assert _extract_chapter_num(None) is None


def test_extract_chapter_num_passes_numeric_input():
    assert _extract_chapter_num(3) == 3.0
    assert _extract_chapter_num(3.5) == 3.5


# ────────────────────────────────────────────────────────────────────────
# _classify_main_chapters
# ────────────────────────────────────────────────────────────────────────

def test_classify_collapse_integers_only():
    # {1, 2, 3} all integers, no decimals → 3 main / 3 effective
    assert _classify_main_chapters([1.0, 2.0, 3.0], collapse_splits=True) == (3, 3)


def test_classify_collapse_split_cluster():
    # {1.1, 1.2, 1.3, 1.4} no integer parent → 1 main / 1 effective (collapse)
    assert _classify_main_chapters([1.1, 1.2, 1.3, 1.4], collapse_splits=True) == (1, 1)


def test_classify_collapse_main_plus_side_story():
    # {4, 4.5} → 1 main / 2 effective (side story preserved)
    assert _classify_main_chapters([4.0, 4.5], collapse_splits=True) == (1, 2)


def test_classify_no_collapse_returns_full_count():
    # collapse=False → effective = len(numbers) regardless of structure
    nums = [1.1, 1.2, 1.3, 1.4]
    assert _classify_main_chapters(nums, collapse_splits=False) == (1, 4)


def test_classify_collapse_integer_plus_sequential_decimals():
    # Rule 3a in classify: integer + sequential .1/.2/.3 → fragments collapse.
    # {1, 1.1, 1.2, 1.3} → 1 unique main, 1 effective.
    assert _classify_main_chapters([1.0, 1.1, 1.2, 1.3], collapse_splits=True) == (1, 1)


def test_classify_collapse_integer_plus_scattered_decimals():
    # Rule 3b in classify: integer + scattered decimals → the highest is a
    # canonical side story. {8, 8.1, 8.5} → 1 unique main, 2 effective
    # (integer + .5 side story; .1 is a dropped duplicate partial).
    assert _classify_main_chapters([8.0, 8.1, 8.5], collapse_splits=True) == (1, 2)


# ────────────────────────────────────────────────────────────────────────
# group_chapters_for_download — full rule table
# ────────────────────────────────────────────────────────────────────────

def test_rule_1_integer_only():
    """Single integer chapter: one group, label = the integer."""
    chapters = [_ch("1")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1"
    assert len(groups[0].parts) == 1


def test_rule_2_integer_plus_one_decimal_preserves_partial():
    """{1, 1.5} → two groups: '1' and '1.5'."""
    chapters = [_ch("1"), _ch("1.5")]
    groups = group_chapters_for_download(chapters)
    assert [g.label for g in groups] == ["1", "1.5"]
    assert all(len(g.parts) == 1 for g in groups)


def test_rule_3_integer_plus_sequential_decimals_drops_decimals():
    """Rule 3a (sequential decimals): {1, 1.1, 1.2, 1.3} → 1 group labeled
    '1', decimals dropped. Decimals form a contiguous .1/.2/.3 sequence,
    which is MangaDex's split-fragment signature — they're parts of
    chapter 1, not separate sub-chapters."""
    chapters = [_ch("1"), _ch("1.1"), _ch("1.2"), _ch("1.3")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1"
    assert len(groups[0].parts) == 1
    assert groups[0].parts[0]["chap"] == "1"


def test_rule_3_integer_plus_scattered_decimals_keeps_highest():
    """Rule 3b (scattered decimals): {8, 8.1, 8.5} → 2 groups, '8' and '8.5'.

    The .1 is a duplicate partial upload, the .5 is the canonical sub-chapter
    (MangaFire's "Chapter 8.5: Side Story" convention). The decimals don't
    form a sequential split (gap at .2/.3/.4), so we keep the integer AND
    the highest decimal; the .1 is dropped as a redundant partial.

    Real-world: this matches Kagurabachi on MangaFire, which returns
    {8, 8.1, 8.5} for chapters where the publisher named the side story
    'X.5' but the aggregator also exposed an early partial upload as 'X.1'.
    """
    chapters = [_ch("8"), _ch("8.1"), _ch("8.5")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 2
    labels = [g.label for g in groups]
    assert labels == ["8", "8.5"]
    assert groups[0].parts[0]["chap"] == "8"
    assert groups[1].parts[0]["chap"] == "8.5"


def test_rule_3_scattered_with_three_decimals_keeps_highest():
    """Rule 3b with 3 scattered decimals: {1, 1.1, 1.3, 1.7} →
    2 groups, '1' and '1.7'. Intermediate decimals dropped."""
    chapters = [_ch("1"), _ch("1.1"), _ch("1.3"), _ch("1.7")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 2
    assert [g.label for g in groups] == ["1", "1.7"]


def test_rule_3_scattered_two_decimals_starting_at_dot_two():
    """Rule 3b: {1, 1.2, 1.3} — decimals don't START at .1, so they're
    scattered (sequential split signature requires .1/.2/...). Keep
    integer + highest decimal."""
    chapters = [_ch("1"), _ch("1.2"), _ch("1.3")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 2
    assert [g.label for g in groups] == ["1", "1.3"]


def test_rule_4_no_integer_one_decimal_singleton():
    """{2.5} → one group labeled '2.5'."""
    chapters = [_ch("2.5")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "2.5"


def test_rule_5_split_cluster_combines_parts():
    """{1.1, 1.2, 1.3, 1.4} → 1 group labeled '1' with all 4 parts."""
    chapters = [_ch("1.1"), _ch("1.2"), _ch("1.3"), _ch("1.4")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1"
    assert len(groups[0].parts) == 4
    assert [p["chap"] for p in groups[0].parts] == ["1.1", "1.2", "1.3", "1.4"]


def test_rule_6_scattered_decimals_uses_highest_decimal_label():
    """Rule 6: {1.5, 1.6} → label '1.6' (the highest decimal).

    Two design constraints captured in one test:
      1. Use the actual decimal as the label, NOT the integer floor:
         labeling as "1" would collide with a real Chapter 1 from
         another source on resume (main_tmp_dir/ch_1 would match both),
         falsely treating one as the resume target for the other.
      2. Pick the HIGHEST decimal, not the lowest: when a source emits
         duplicate partial uploads of the same canonical sub-chapter,
         the higher decimal is by convention the publisher's canonical
         numbering (MangaFire's "X.5: Side Story"-style chapters).
    """
    chapters = [_ch("1.5"), _ch("1.6")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1.6"
    assert len(groups[0].parts) == 1
    assert groups[0].parts[0]["chap"] == "1.6"


def test_rule_6_scattered_decimals_highest_kept_regardless_of_input_order():
    """Rule 6: when decimals arrive out of order, the HIGHEST is still kept.
    {1.5, 1.1} → label '1.5', parts=[{chap: '1.5'}].
    """
    chapters = [_ch("1.5"), _ch("1.1")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1.5"
    assert groups[0].parts[0]["chap"] == "1.5"


def test_rule_6_no_collision_with_real_integer_from_other_source():
    """Two sources passed sequentially: source A delivers {1.2, 1.3}
    (rule-6 partials), source B delivers {1} (real integer). With
    label = highest decimal, the rule-6 group's label is '1.3' so the
    on-disk tdirs differ from source B's '1'. Note: this scenario tests
    the LABELS produced by two independent group calls — we don't merge
    sources here, just verify the labels can co-exist.
    """
    groups_a = group_chapters_for_download([_ch("1.2"), _ch("1.3")])
    groups_b = group_chapters_for_download([_ch("1")])
    assert groups_a[0].label == "1.3"
    assert groups_b[0].label == "1"
    assert groups_a[0].label != groups_b[0].label


def test_rule_6_dot_one_and_dot_five_keeps_dot_five():
    """Rule 6: {8.1, 8.5} with no integer 8 → label '8.5'.

    The .1 is a duplicate partial upload; .5 is the canonical sub-chapter.
    No integer parent here (in the MangaFire {8, 8.1, 8.5} case the
    integer 8 is preserved separately via Rule 3b — this is the
    no-integer variant where MangaFire only emits the duplicates).
    """
    chapters = [_ch("8.1"), _ch("8.5")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "8.5"
    assert groups[0].parts[0]["chap"] == "8.5"


def test_collapse_disabled_passes_through():
    """collapse_splits=False yields one group per chapter, no merging."""
    chapters = [_ch("1.1"), _ch("1.2"), _ch("1.3")]
    groups = group_chapters_for_download(chapters, collapse_splits=False)
    assert len(groups) == 3
    assert [g.label for g in groups] == ["1.1", "1.2", "1.3"]
    assert all(len(g.parts) == 1 for g in groups)


def test_unparseable_chapters_pass_through():
    """Chapters with non-numeric labels (oneshot, omake) are emitted as
    singleton groups — they aren't bucketable but shouldn't be silently
    dropped."""
    chapters = [_ch("Oneshot"), _ch("1"), _ch("Omake")]
    groups = group_chapters_for_download(chapters)
    labels = [g.label for g in groups]
    assert "1" in labels
    # Non-numeric labels appear at the end (after numeric buckets)
    assert "Oneshot" in labels or "1" in labels  # both present
    assert any(g.label == "Oneshot" for g in groups)
    assert any(g.label == "Omake" for g in groups)


def test_empty_chapter_list():
    """Empty input → empty output, no exceptions."""
    assert group_chapters_for_download([]) == []


def test_rule_5_synthesized_merged_dict_carries_metadata():
    """Rule 5 synthesizes a merged chapter dict; the parts list retains
    the original part dicts so _process_chapter_impl can iterate them.
    Verifies the parts list is preserved verbatim (not copied/wrapped).
    """
    parts = [
        {"chap": "1.1", "url": "https://a/ch/1.1", "hid": "p1"},
        {"chap": "1.2", "url": "https://a/ch/1.2", "hid": "p2"},
    ]
    groups = group_chapters_for_download(parts)
    assert len(groups) == 1
    assert groups[0].label == "1"
    assert groups[0].parts == parts  # same objects, same order


# ────────────────────────────────────────────────────────────────────────
# Cross-source duplicate detection (2026-05-27 refinement)
#
# These tests cover the Rule 2 / 3b / 6 refinement that uses a peer-source
# consensus_set to drop source-only .1/.2/.3/.4 fragments without touching
# real side stories. See ~/.claude/plans/ultrathink-mangafire-and-some-
# flickering-sparkle.md for the design and sites/chapter_merger.py for
# the implementation (_is_source_only_fragment, _classify_chapter_breakdown).
# ────────────────────────────────────────────────────────────────────────

def test_is_fragment_shaped_decimal_dot_one_through_four():
    """Fragment-shaped decimals are exactly .1/.2/.3/.4 (the conventional
    upload-fragment suffixes). .5 and higher are real side-story numbers."""
    assert _is_fragment_shaped_decimal(52.1, 52) is True
    assert _is_fragment_shaped_decimal(52.2, 52) is True
    assert _is_fragment_shaped_decimal(52.3, 52) is True
    assert _is_fragment_shaped_decimal(52.4, 52) is True
    assert _is_fragment_shaped_decimal(52.5, 52) is False
    assert _is_fragment_shaped_decimal(52.6, 52) is False
    assert _is_fragment_shaped_decimal(52.9, 52) is False
    # Integer chapter is not "fragment-shaped" (rel == 0).
    assert _is_fragment_shaped_decimal(52.0, 52) is False


def test_is_source_only_fragment_requires_non_empty_consensus():
    """Without consensus (None or empty set), the predicate must return
    False so the rest of the rule table behaves identically to today."""
    assert _is_source_only_fragment(52.1, 52, None) is False
    assert _is_source_only_fragment(52.1, 52, set()) is False


def test_is_source_only_fragment_returns_false_when_peer_confirms():
    """Peer source has 52.1 → it's a real chapter, NOT a fragment to drop."""
    assert _is_source_only_fragment(52.1, 52, {52.0, 52.1, 53.0}) is False


def test_is_source_only_fragment_returns_true_for_lone_dot_one():
    """The Shangri-La Frontier case: mangafire has 52.1, peers don't."""
    assert _is_source_only_fragment(52.1, 52, {52.0, 53.0}) is True


def test_is_source_only_fragment_keeps_dot_five():
    """.5 is never fragment-shaped, regardless of consensus state.
    Matches the user's 'we're not trying to remove extras, just duplicates'
    constraint — .5 is the canonical side-story suffix."""
    assert _is_source_only_fragment(52.5, 52, {52.0}) is False
    assert _is_source_only_fragment(52.5, 52, set()) is False


# ────────────────────────────────────────────────────────────────────────
# group_chapters_for_download with consensus_set — the critical fixes
# ────────────────────────────────────────────────────────────────────────

def test_rule_2_refined_drops_source_only_dot_one():
    """THE critical case from the user's Shangri-La Frontier data: mangafire
    has {52, 52.1}, peer sources have {52} only. With consensus_set={52},
    52.1 is identified as a source-only fragment and dropped — exactly
    what the user asked for ('make rule 2 harsher for .1 chapters if no
    other providers have it')."""
    groups = group_chapters_for_download(
        [_ch("52"), _ch("52.1")],
        consensus_set={52.0},
    )
    assert [g.label for g in groups] == ["52"]


def test_rule_2_refined_keeps_dot_five_even_when_source_only():
    """Per the user's 'don't remove extras' constraint, .5 is treated
    conservatively: even if no peer has the .5, it's kept as a side story
    because .5 is overwhelmingly used for canonical sub-chapters, not
    fragments."""
    groups = group_chapters_for_download(
        [_ch("52"), _ch("52.5")],
        consensus_set={52.0},
    )
    assert [g.label for g in groups] == ["52", "52.5"]


def test_rule_2_refined_keeps_dot_one_when_peer_confirms():
    """If a peer source also lists 52.1, it's NOT a duplicate — keep both."""
    groups = group_chapters_for_download(
        [_ch("52"), _ch("52.1")],
        consensus_set={52.0, 52.1},
    )
    assert [g.label for g in groups] == ["52", "52.1"]


def test_rule_2_unchanged_without_consensus_set():
    """consensus_set=None → fall back to pre-refinement Rule 2 behavior
    (always keep the decimal). Critical for direct-URL / single-source
    runs where there's no peer data."""
    groups = group_chapters_for_download(
        [_ch("52"), _ch("52.1")],
        consensus_set=None,
    )
    assert [g.label for g in groups] == ["52", "52.1"]


def test_rule_2_unchanged_when_collapse_off_even_with_consensus():
    """collapse_splits=False → no drops regardless of consensus signal.
    The opt-out lets the user keep everything if they want to."""
    groups = group_chapters_for_download(
        [_ch("52"), _ch("52.1")],
        collapse_splits=False,
        consensus_set={52.0},
    )
    assert [g.label for g in groups] == ["52", "52.1"]


def test_rule_3b_refined_drops_source_only_fragment_highest():
    """Rule 3b case where the kept 'highest' is itself a source-only
    fragment shape: {52, 52.1, 52.3}, peer has {52} only. Intermediate
    .1 is dropped by the unchanged Rule 3b; the refinement additionally
    drops .3 (fragment-shaped + source-only). Rare in practice but the
    refinement should fire."""
    groups = group_chapters_for_download(
        [_ch("52"), _ch("52.1"), _ch("52.3")],
        consensus_set={52.0},
    )
    assert [g.label for g in groups] == ["52"]


def test_rule_3b_keeps_dot_five_highest():
    """Rule 3b standard case: {52, 52.1, 52.5}, peer has {52}. Existing
    Rule 3b drops 52.1 (intermediate); 52.5 is the highest decimal and
    NOT fragment-shaped, so it's kept regardless of consensus."""
    groups = group_chapters_for_download(
        [_ch("52"), _ch("52.1"), _ch("52.5")],
        consensus_set={52.0},
    )
    assert [g.label for g in groups] == ["52", "52.5"]


def test_rule_6_refined_drops_when_parent_in_consensus():
    """Rule 6 (scattered, not sequential): {1.2, 1.3} — no integer, doesn't
    start at .1 so it's Rule 6 territory, NOT Rule 5 (sequential split).
    Peer has the integer parent {1}. Refinement drops the cluster as
    fragment noise: highest 1.3 is fragment-shaped AND parent in consensus."""
    groups = group_chapters_for_download(
        [_ch("1.2"), _ch("1.3")],
        consensus_set={1.0},
    )
    assert [g.label for g in groups] == []


def test_rule_6_keeps_when_parent_not_in_consensus():
    """Rule 6: {1.2, 1.3}, no peer has the integer parent 1 either (peer
    consensus is on different chapter numbers). No peer signal that 1 is
    the canonical chapter → keep current behavior (highest decimal, label
    '1.3'). Conservative — we only drop when peers explicitly confirm
    the parent as canonical."""
    groups = group_chapters_for_download(
        [_ch("1.2"), _ch("1.3")],
        consensus_set={5.0, 10.0},  # peer consensus exists, but not for floor=1
    )
    assert [g.label for g in groups] == ["1.3"]


def test_rule_6_keeps_dot_five_regardless_of_consensus():
    """Rule 6: {1.5, 1.6}, parent in consensus. .6 isn't fragment-shaped,
    so it's kept even though the parent IS in consensus. The refinement
    only fires on fragment-shaped (.1-.4) decimals."""
    groups = group_chapters_for_download(
        [_ch("1.5"), _ch("1.6")],
        consensus_set={1.0},
    )
    assert [g.label for g in groups] == ["1.6"]


# ────────────────────────────────────────────────────────────────────────
# _classify_main_chapters with consensus_set
# (Mirrors the same scenarios above but checks the diagnostic-count side)
# ────────────────────────────────────────────────────────────────────────

def test_classify_rule_2_refined_drops_dot_one():
    """{52, 52.1} with consensus={52} → (1 unique main, 1 effective).
    Mirrors test_rule_2_refined_drops_source_only_dot_one for the
    display side."""
    assert _classify_main_chapters(
        [52.0, 52.1],
        collapse_splits=True,
        consensus_set={52.0},
    ) == (1, 1)


def test_classify_rule_2_keeps_dot_five():
    """{52, 52.5} with consensus={52} → (1 unique main, 2 effective)
    — .5 preserved as side story, matches the download path."""
    assert _classify_main_chapters(
        [52.0, 52.5],
        collapse_splits=True,
        consensus_set={52.0},
    ) == (1, 2)


def test_classify_rule_2_peer_confirmed_dot_one_kept():
    """{52, 52.1} with consensus={52, 52.1} → (1 unique main, 2 effective).
    Peer-confirmed decimals count as effective entries."""
    assert _classify_main_chapters(
        [52.0, 52.1],
        collapse_splits=True,
        consensus_set={52.0, 52.1},
    ) == (1, 2)


def test_classify_no_consensus_matches_pre_refinement_behavior():
    """When consensus_set is None, the count must match the pre-2026-05-27
    behavior exactly so single-source / direct-URL runs don't see any
    surprise changes."""
    # Same {8, 8.1, 8.5} Kagurabachi case as the existing
    # test_classify_collapse_integer_plus_scattered_decimals test.
    assert _classify_main_chapters(
        [8.0, 8.1, 8.5],
        collapse_splits=True,
        consensus_set=None,
    ) == (1, 2)


# ────────────────────────────────────────────────────────────────────────
# _classify_chapter_breakdown
# ────────────────────────────────────────────────────────────────────────

def test_breakdown_empty_input():
    bd = _classify_chapter_breakdown([], consensus_set=set(), consensus_max=None)
    assert isinstance(bd, ChapterBreakdown)
    assert all(getattr(bd, f) == 0 for f in [
        "consensus_main", "source_only_latest", "consensus_side_stories",
        "safe_decimals", "prologue_count", "source_only_orphans",
        "fragments_dropped", "unparseable_passthrough",
    ])


def test_breakdown_routes_each_bucket_correctly():
    """Synthetic dataset hitting every bucket exactly once. Catches any
    routing bug where a chapter falls into the wrong bucket."""
    chapters = [
        _ch("0"),         # prologue_count
        _ch("1"),         # consensus_main
        _ch("2"),         # consensus_main
        _ch("3"),         # source_only_orphans (in range but not in consensus)
        _ch("3.1"),       # fragments_dropped
        _ch("3.5"),       # safe_decimals (source-only, .5+)
        _ch("4.5"),       # consensus_side_stories (peer-confirmed)
        _ch("10"),        # source_only_latest (> consensus_max)
        _ch("Oneshot"),   # unparseable_passthrough
    ]
    bd = _classify_chapter_breakdown(
        chapters,
        consensus_set={1.0, 2.0, 4.5},  # peers have 1, 2, 4.5
        consensus_max=4.5,
    )
    assert bd.prologue_count == 1
    assert bd.consensus_main == 2  # 1, 2
    assert bd.source_only_orphans == 1  # 3 (in range, not in consensus)
    assert bd.fragments_dropped == 1  # 3.1
    assert bd.safe_decimals == 1  # 3.5
    assert bd.consensus_side_stories == 1  # 4.5
    assert bd.source_only_latest == 1  # 10
    assert bd.unparseable_passthrough == 1  # "Oneshot"


def test_breakdown_no_consensus_treats_everything_as_main_or_safe():
    """Without peer signal, integers all go to consensus_main and decimals
    all go to safe_decimals — nothing is dropped or flagged orphan because
    we have no basis to judge."""
    bd = _classify_chapter_breakdown(
        [_ch("1"), _ch("2"), _ch("2.1"), _ch("2.5")],
        consensus_set=set(),
        consensus_max=None,
    )
    assert bd.consensus_main == 2  # 1, 2
    assert bd.safe_decimals == 2  # both 2.1 AND 2.5 → safe (no peer signal)
    assert bd.fragments_dropped == 0  # NEVER fires without consensus
    assert bd.source_only_orphans == 0


def test_breakdown_shangri_la_frontier_dataset():
    """End-to-end test on the exact mangafire chapter list from the user's
    Shangri-La Frontier example (305 entries). Expected per the design
    plan:
      consensus_main:    266  (integers 1..266, peer-confirmed)
      safe_decimals:      16  (the .5 entries — mangafire-only, .5+ kept)
      prologue_count:      1  (chapter 0)
      fragments_dropped:  22  (all .1 entries dropped)
      others:              0
      sum:               305
    """
    # The actual mangafire chapter list per user-supplied data.
    int_chapters = [_ch(str(n)) for n in range(0, 267)]
    dot_one = [
        _ch("5.1"), _ch("15.1"), _ch("35.1"), _ch("45.1"), _ch("52.1"),
        _ch("55.1"), _ch("65.1"), _ch("75.1"), _ch("85.1"), _ch("95.1"),
        _ch("105.1"), _ch("115.1"), _ch("125.1"), _ch("135.1"), _ch("145.1"),
        _ch("155.1"), _ch("165.1"), _ch("175.1"), _ch("185.1"), _ch("195.1"),
        _ch("205.1"), _ch("215.1"),
    ]
    dot_five = [
        _ch("5.5"), _ch("15.5"), _ch("25.5"), _ch("35.5"), _ch("45.5"),
        _ch("55.5"), _ch("65.5"), _ch("95.5"), _ch("105.5"), _ch("115.5"),
        _ch("125.5"), _ch("135.5"), _ch("145.5"), _ch("155.5"), _ch("195.5"),
        _ch("205.5"),
    ]
    chapters = int_chapters + dot_one + dot_five
    assert len(chapters) == 305  # sanity

    # Peer consensus: all 5 peer sources agree on integers 1..266
    # (chapter 0 isn't on peers; the .5s and .1s aren't on peers either).
    peer_consensus = {float(n) for n in range(1, 267)}

    bd = _classify_chapter_breakdown(
        chapters,
        consensus_set=peer_consensus,
        consensus_max=266.0,
    )
    assert bd.consensus_main == 266
    assert bd.safe_decimals == 16
    assert bd.prologue_count == 1
    assert bd.fragments_dropped == 22
    assert bd.consensus_side_stories == 0  # peers don't carry the .5s
    assert bd.source_only_latest == 0
    assert bd.source_only_orphans == 0
    assert bd.unparseable_passthrough == 0
    # Sanity: bucket sum must equal input length.
    total = (
        bd.consensus_main + bd.source_only_latest + bd.consensus_side_stories
        + bd.safe_decimals + bd.prologue_count + bd.source_only_orphans
        + bd.fragments_dropped + bd.unparseable_passthrough
    )
    assert total == 305


def test_breakdown_shangri_la_frontier_group_count():
    """Same Shangri-La Frontier dataset, but exercising the download path:
    group_chapters_for_download should emit 283 groups (266 + 16 + 1)
    when collapse is on with the peer consensus. The 22 .1 fragments
    are dropped (15 by existing Rule 3b — chapters with both .1 and .5
    — plus 7 by the new Rule 2 refinement — lone .1 with no peer)."""
    int_chapters = [_ch(str(n)) for n in range(0, 267)]
    dot_one = [
        _ch("5.1"), _ch("15.1"), _ch("35.1"), _ch("45.1"), _ch("52.1"),
        _ch("55.1"), _ch("65.1"), _ch("75.1"), _ch("85.1"), _ch("95.1"),
        _ch("105.1"), _ch("115.1"), _ch("125.1"), _ch("135.1"), _ch("145.1"),
        _ch("155.1"), _ch("165.1"), _ch("175.1"), _ch("185.1"), _ch("195.1"),
        _ch("205.1"), _ch("215.1"),
    ]
    dot_five = [
        _ch("5.5"), _ch("15.5"), _ch("25.5"), _ch("35.5"), _ch("45.5"),
        _ch("55.5"), _ch("65.5"), _ch("95.5"), _ch("105.5"), _ch("115.5"),
        _ch("125.5"), _ch("135.5"), _ch("145.5"), _ch("155.5"), _ch("195.5"),
        _ch("205.5"),
    ]
    chapters = int_chapters + dot_one + dot_five
    peer_consensus = {float(n) for n in range(1, 267)}

    groups = group_chapters_for_download(
        chapters,
        collapse_splits=True,
        consensus_set=peer_consensus,
    )
    # 266 main + 1 prologue (chapter 0) + 16 .5 side stories = 283
    assert len(groups) == 283
    # No .1 labels survive
    assert not any(".1" in g.label for g in groups)
    # All .5 labels survive
    survived_fives = sum(1 for g in groups if g.label.endswith(".5"))
    assert survived_fives == 16


# ────────────────────────────────────────────────────────────────────────
# align_chapter_lists exposes consensus_set + breakdown
# ────────────────────────────────────────────────────────────────────────

def test_align_exposes_consensus_set():
    """align_chapter_lists must populate consensus_set and consensus_max
    on the AlignmentResult. Two sources with overlapping chapter numbers
    yields a consensus = the overlap."""
    a = [_ch("1"), _ch("2"), _ch("3")]
    b = [_ch("1"), _ch("2"), _ch("4")]
    result = align_chapter_lists([("a", a), ("b", b)])
    assert isinstance(result, AlignmentResult)
    # 1.0 and 2.0 are in both → consensus. 3 and 4 are source-only → not.
    assert result.consensus_set == {1.0, 2.0}
    assert result.consensus_max == 2.0


def test_align_consensus_empty_for_single_source():
    """Single source → no peer → empty consensus, None max. Critical for
    direct-URL flows to fall through to the pre-refinement behavior."""
    result = align_chapter_lists([("a", [_ch("1"), _ch("2"), _ch("2.1")])])
    assert result.consensus_set == set()
    assert result.consensus_max is None


def test_align_includes_breakdown_in_diagnostics():
    """Each merge_diagnostics entry must carry a 'breakdown' dict that
    sums to the source's total_chapters. SearchChapterMap.jsx reads
    these fields to render the bucket chips."""
    a = [_ch("1"), _ch("2"), _ch("3")]
    b = [_ch("1"), _ch("2"), _ch("4"), _ch("4.1")]
    result = align_chapter_lists([("a", a), ("b", b)])
    for site in ("a", "b"):
        diag = result.merge_diagnostics[site]
        assert "breakdown" in diag
        bd = diag["breakdown"]
        # Sum of buckets equals total_chapters (excluding unparseable
        # which is also a bucket).
        total = sum(bd.values())
        assert total == diag["total_chapters"]


def test_align_classifies_source_only_dot_one_as_fragment():
    """Verifies the cross-source-consensus signal: source A has a lone
    {3, 3.1}, source B has only {3}. 3.1 lands in fragments_dropped
    on source A's breakdown."""
    a = [_ch("3"), _ch("3.1")]
    b = [_ch("3")]
    result = align_chapter_lists([("a", a), ("b", b)])
    # source a is the anchor (most chapters)
    bd_a = result.merge_diagnostics["a"]["breakdown"]
    assert bd_a["fragments_dropped"] == 1
    assert bd_a["consensus_main"] == 1  # just chapter 3
    assert bd_a["safe_decimals"] == 0
    # source b has only chapter 3 (consensus_main)
    bd_b = result.merge_diagnostics["b"]["breakdown"]
    assert bd_b["consensus_main"] == 1
    assert bd_b["fragments_dropped"] == 0
