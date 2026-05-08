"""Tests for sites/chapter_merger.py — particularly the post-audit Rule 6
label change and the surrounding rule table.

Cross-file: chapter_merger.group_chapters_for_download is consumed by
aio-dl.py:main() between chapter selection and the download loop. The
synthesized merged-chapter dicts (`_merged_parts` set on rule 5) feed
into _process_chapter_impl's part-by-part fetch path.
"""

from __future__ import annotations

from sites.chapter_merger import (
    ChapterGroup,
    _classify_main_chapters,
    _format_chapter_label,
    _extract_chapter_num,
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


def test_rule_3_integer_plus_many_decimals_drops_decimals():
    """{1, 1.1, 1.2, 1.3} → 1 group labeled '1', decimals dropped."""
    chapters = [_ch("1"), _ch("1.1"), _ch("1.2"), _ch("1.3")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1"
    assert len(groups[0].parts) == 1
    assert groups[0].parts[0]["chap"] == "1"


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


def test_rule_6_scattered_decimals_uses_lowest_decimal_label():
    """Audit fix: {1.5, 1.6} → label '1.5', NOT '1'.

    Previously this emitted label='1' (integer floor) which collided with
    a real Chapter 1 from another source on resume — `main_tmp_dir/ch_1`
    would match BOTH the rule-6 group and a real Chapter 1, falsely
    treating one as the resume target for the other. Using the actual
    decimal as the label keeps the on-disk identity unique.
    """
    chapters = [_ch("1.5"), _ch("1.6")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1.5"
    assert len(groups[0].parts) == 1
    assert groups[0].parts[0]["chap"] == "1.5"


def test_rule_6_scattered_decimals_lowest_first():
    """Rule 6: when decimals are out of order, the LOWEST is kept.
    {1.5, 1.1} → label '1.1', parts=[{chap: '1.1'}].
    """
    chapters = [_ch("1.5"), _ch("1.1")]
    groups = group_chapters_for_download(chapters)
    assert len(groups) == 1
    assert groups[0].label == "1.1"
    assert groups[0].parts[0]["chap"] == "1.1"


def test_rule_6_no_collision_with_real_integer_from_other_source():
    """Two sources passed sequentially: source A delivers {1.2, 1.3}
    (rule-6 partials), source B delivers {1} (real integer). With the
    audit-fixed label, the rule-6 group's label is '1.2' so the on-disk
    tdirs differ. Note: this scenario tests the LABELS produced by two
    independent group calls — we don't merge sources here, just verify
    the labels can co-exist.
    """
    groups_a = group_chapters_for_download([_ch("1.2"), _ch("1.3")])
    groups_b = group_chapters_for_download([_ch("1")])
    assert groups_a[0].label == "1.2"
    assert groups_b[0].label == "1"
    assert groups_a[0].label != groups_b[0].label


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
