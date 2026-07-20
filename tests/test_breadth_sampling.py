"""Tests for v5 breadth sampling + throttle-probe tail
(sites/base.py:_probe_chapter_aggregate).

The v5 probe pipeline replaces "5 pages from 1 chapter" with
"1 page from each of 8 chapters" + a "throttle-probe tail" of 3 sequential
pages from the highest-scoring chapter (the cdn_reliability metric).

These tests use mocked handlers (the probe pipeline is heavily I/O-bound
on real handlers; mocking lets us verify the breadth logic deterministically).
Real-world validation lives in tests/test_t1_scoring.py which scores actual
manga pages end-to-end through _score_image_blob.

Cross-file: targets sites/base.py:_pick_representative_chapters,
_pick_random_middle_page_index, _probe_chapter_aggregate. Plan reference:
~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 2 section).
"""

from __future__ import annotations

import io
import os
from typing import Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import requests
from PIL import Image

from sites.base import BaseSiteHandler, SearchHit


# ---------------------------------------------------------------------------
# Helpers for building synthetic chapter lists
# ---------------------------------------------------------------------------

def _mk_chapters(count: int, decimal_every: int = 0) -> List[Dict]:
    """Build a synthetic chapter list of `count` chapters.

    decimal_every: when > 0, every Nth chapter gets a decimal suffix
    (e.g., decimal_every=3 → chapter 3 becomes 3.5, chapter 6 becomes 6.5).
    Used to test the whole-numbered preference in _pick_representative_chapters.
    """
    chapters = []
    for i in range(1, count + 1):
        if decimal_every > 0 and i % decimal_every == 0:
            chap_label = f"{i}.5"
            chap_num = float(f"{i}.5")
        else:
            chap_label = str(i)
            chap_num = float(i)
        chapters.append({"chap": chap_num, "label": chap_label})
    return chapters


def _make_jpeg_blob(w: int = 1114, h: int = 1584, quality: int = 85) -> bytes:
    """Produce a synthetic JPEG blob for mocked image fetches. Sized to
    match real MangaFire pages (1114x1584)."""
    img = Image.new("L", (w, h), 200)
    # Add some edge variation so the scorer doesn't return zero
    px = img.load()
    for x in range(0, w, 7):
        for y in range(h):
            px[x, y] = 50
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _pick_representative_chapters
# ---------------------------------------------------------------------------

def test_pick_chapters_long_series():
    """100-chapter series: 8 chapters spread across the trimmed pool (skip
    first/last)."""
    chapters = _mk_chapters(100)
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=8)
    assert len(picks) == 8
    indices = [p[0] for p in picks]
    # All indices should be in [1, 98] (first 0 and last 99 trimmed).
    assert min(indices) >= 1
    assert max(indices) <= 98
    # Strictly increasing.
    assert indices == sorted(indices)
    # No duplicates.
    assert len(set(indices)) == 8


def test_pick_chapters_oneshot():
    """1-chapter series: return that single chapter (degenerate case)."""
    chapters = _mk_chapters(1)
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=8)
    assert len(picks) == 1
    assert picks[0][0] == 0


def test_pick_chapters_short_series():
    """3-chapter series: trim first only (keeping middle + last)."""
    chapters = _mk_chapters(3)
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=8)
    # Should return chapters 1 and 2 (indices 1 and 2 in 0-based).
    assert len(picks) >= 1
    assert all(0 < idx < 3 for idx, _ in picks)


def test_pick_chapters_two_chapter_series():
    """2-chapter series: keep both (trimming would empty the pool)."""
    chapters = _mk_chapters(2)
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=8)
    assert len(picks) == 2


def test_pick_chapters_empty():
    """Empty input: empty result."""
    assert BaseSiteHandler._pick_representative_chapters([], n=8) == []


def test_pick_chapters_prefers_whole_numbered():
    """When the chapter list has decimals (e.g., 3.5, 6.5), the picker
    should prefer the whole-numbered ones (omake/extras have atypical
    page counts)."""
    chapters = _mk_chapters(50, decimal_every=2)  # half are decimals
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=8)
    # All picks should have whole-number chap values.
    for _, ch in picks:
        chap = ch["chap"]
        assert float(chap) == int(chap), (
            f"_pick_representative_chapters picked a decimal chapter: {ch}"
        )


def test_pick_chapters_fills_from_full_pool_when_whole_short():
    """If there are fewer than N whole-numbered chapters in the trimmed
    pool, fill the remainder from the full trimmed pool (decimals
    included). Picking 0 is preferable to having < N samples."""
    # Build a list where most chapters are decimals.
    chapters = [
        {"chap": float(f"{i}.5"), "label": f"{i}.5"} for i in range(1, 20)
    ]
    # Insert exactly 3 whole-numbered chapters in the middle of the list.
    chapters[5] = {"chap": 6.0, "label": "6"}
    chapters[10] = {"chap": 11.0, "label": "11"}
    chapters[15] = {"chap": 16.0, "label": "16"}
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=8)
    # Should hit 8 chapters total — 3 whole-numbered + 5 from the decimals.
    assert len(picks) == 8


def test_pick_chapters_respects_n_parameter():
    """n=2 should return at most 2 chapters; used by the EXPENSIVE_PROBE
    quick-probe clamp."""
    chapters = _mk_chapters(100)
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=2)
    assert len(picks) == 2


def test_pick_chapters_indices_are_absolute():
    """The returned (abs_idx, chapter) tuples use the absolute index in
    the chapters list. _pick_random_middle_page_index relies on this
    for deterministic seeding across runs."""
    chapters = _mk_chapters(20)
    picks = BaseSiteHandler._pick_representative_chapters(chapters, n=5)
    for abs_idx, ch in picks:
        assert chapters[abs_idx] is ch, (
            f"abs_idx {abs_idx} doesn't match chapter object"
        )


# ---------------------------------------------------------------------------
# _pick_random_middle_page_index
# ---------------------------------------------------------------------------

def test_pick_page_index_deterministic():
    """Same (series_url, chapter_index) → same page pick. Cache replays
    must be stable."""
    idx_a = BaseSiteHandler._pick_random_middle_page_index(100, "https://x.com/series", 5)
    idx_b = BaseSiteHandler._pick_random_middle_page_index(100, "https://x.com/series", 5)
    assert idx_a == idx_b


def test_pick_page_index_stratified_to_middle_half():
    """For n=100, picks should land in [25, 75) — the middle 50%."""
    for chap_idx in range(50):
        idx = BaseSiteHandler._pick_random_middle_page_index(100, "https://x.com/series", chap_idx)
        assert 25 <= idx < 75, f"chap_idx {chap_idx} picked {idx}, out of [25, 75)"


def test_pick_page_index_varies_per_chapter():
    """Different chapters of the same series should pick different pages
    (avoids always sampling page-N which could be a chapter-title splash
    on some sources)."""
    picks = set()
    for chap_idx in range(20):
        picks.add(
            BaseSiteHandler._pick_random_middle_page_index(100, "https://x.com/series", chap_idx)
        )
    # 20 chapters → expect varied picks, not all the same. Tolerate
    # occasional collisions but require >5 distinct values.
    assert len(picks) > 5


def test_pick_page_index_short_chapter_returns_middle():
    """Chapters with <=4 pages: return n//2 (safe middle)."""
    assert BaseSiteHandler._pick_random_middle_page_index(4, "url", 0) == 2
    assert BaseSiteHandler._pick_random_middle_page_index(3, "url", 0) == 1
    assert BaseSiteHandler._pick_random_middle_page_index(1, "url", 0) == 0


def test_pick_page_index_empty_chapter_returns_none():
    """n_pages=0 → None (caller skips this chapter)."""
    assert BaseSiteHandler._pick_random_middle_page_index(0, "url", 0) is None


def test_pick_page_index_different_series_different_pick():
    """Same chapter index on different series should generally pick
    different pages (the series_url is part of the seed)."""
    picks = set()
    for series_id in range(20):
        picks.add(
            BaseSiteHandler._pick_random_middle_page_index(
                100, f"https://x.com/series-{series_id}", 5,
            )
        )
    assert len(picks) > 5


# ---------------------------------------------------------------------------
# _probe_chapter_aggregate — breadth sampling end-to-end (mocked)
# ---------------------------------------------------------------------------

class _MockHandler(BaseSiteHandler):
    """A scriptable handler for testing the probe pipeline. Overrides the
    methods that _probe_chapter_aggregate needs and exposes counters so
    tests can assert call patterns."""

    name = "mock"

    def __init__(self, chapters: List[Dict], pages_per_chapter: int = 20,
                 page_blob: Optional[bytes] = None,
                 fail_chapters: Optional[set] = None,
                 fail_get_images: bool = False,
                 fail_throttle_tail: bool = False,
                 timeout_chapters: Optional[set] = None,
                 timeout_all_get_images: bool = False,
                 timeout_fetch_chapters: Optional[set] = None):
        super().__init__()
        self._chapters = chapters
        self._pages_per_chapter = pages_per_chapter
        self._page_blob = page_blob or _make_jpeg_blob()
        self._fail_chapters = fail_chapters or set()
        self._fail_get_images = fail_get_images
        self._fail_throttle_tail = fail_throttle_tail
        # v5.2: chapters whose get_chapter_images raises a TIMEOUT (slowness) —
        # distinct from _fail_chapters, which raise a RuntimeError (a genuine
        # content failure). The probe EXCLUDES timeouts from the score aggregate
        # but keeps genuine failures as a scored 0.0.
        self._timeout_chapters = timeout_chapters or set()
        self._timeout_all_get_images = timeout_all_get_images
        # chapters whose single image FETCH times out (the _ex (None, True)
        # path), distinct from a get_chapter_images-level timeout.
        self._timeout_fetch_chapters = timeout_fetch_chapters or set()
        self.fetch_image_calls: List[str] = []

    def fetch_comic_context(self, url, scraper, make_request):
        # Returns a non-None object — the probe just needs it to not be None.
        return MagicMock(comic={}, title="mock", identifier="mock")

    def get_chapters(self, context, scraper, language, make_request):
        return self._chapters

    def get_chapter_images(self, chapter, scraper, make_request):
        chap_label = chapter.get("label") or chapter.get("chap")
        if self._timeout_all_get_images or chap_label in self._timeout_chapters:
            # A network timeout (slowness), NOT a RuntimeError — the probe marks
            # this is_timeout=True and drops it from the aggregate.
            raise requests.exceptions.ReadTimeout(
                f"mock: get_chapter_images timed out for {chap_label}"
            )
        if self._fail_get_images or chap_label in self._fail_chapters:
            raise RuntimeError(f"mock: get_chapter_images failed for {chap_label}")
        return [
            f"https://cdn.mock/chap-{chap_label}/page-{i}.jpg"
            for i in range(self._pages_per_chapter)
        ]

    def _fetch_probe_item_bytes_ex(self, item, scraper):
        # v5.2: the probe calls _fetch_probe_item_bytes_ex (returns
        # (bytes_or_None, timed_out)); the base _fetch_probe_item_bytes wrapper
        # (used by the throttle tail) delegates HERE, so BOTH the breadth pass
        # and the tail route through this override and fetch_image_calls still
        # captures every fetch.
        self.fetch_image_calls.append(item)
        # Fetch-level timeout for chapters flagged via timeout_fetch_chapters
        # (item URLs embed the chapter label as `chap-<label>`).
        for label in self._timeout_fetch_chapters:
            if f"/chap-{label}/" in item:
                return None, True
        # _fail_throttle_tail: succeed for the first _breadth_fetch_budget
        # fetches (one per breadth chapter), then fail every subsequent (tail)
        # fetch as a GENUINE (non-timeout) failure — a CDN that throttles after
        # N first-request hits.
        if self._fail_throttle_tail:
            if len(self.fetch_image_calls) > self._breadth_fetch_budget:
                return None, False
        return self._page_blob, False

    # How many "breadth phase" fetches succeed before the simulated CDN
    # starts throttling (used by _fail_throttle_tail). Default = 8 (one
    # per chapter in the default breadth pass). Tests using max_samples=2
    # should override.
    _breadth_fetch_budget = 8


def test_probe_chapter_aggregate_breadth_full_success():
    """8 chapters × 1 page each, all succeed → samples=8/8 + throttle tail."""
    handler = _MockHandler(_mk_chapters(20))
    hit = SearchHit(site="mock", title="Mock Series", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    assert metadata["samples_attempted"] == 8
    assert metadata["samples_succeeded"] == 8
    # Throttle tail should have run with full success.
    assert metadata["cdn_reliability"] is not None
    assert metadata["cdn_reliability"] == 1.0
    # Score should reflect the synthetic JPEG quality.
    assert 0.3 < score < 1.0


def test_probe_chapter_aggregate_breadth_max_samples_clamps_chapter_count():
    """max_samples=2 limits to 2 chapters (the EXPENSIVE_PROBE quick-probe path)."""
    handler = _MockHandler(_mk_chapters(20))
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock(), max_samples=2)
    assert result is not None
    _, metadata = result
    assert metadata["samples_attempted"] == 2
    assert metadata["samples_succeeded"] == 2


def test_probe_chapter_aggregate_partial_chapter_failures_use_mean():
    """When some chapters fail get_chapter_images, falls back to mean
    aggregation (preserves throttle/failure signal)."""
    chapters = _mk_chapters(20)
    handler = _MockHandler(chapters, fail_chapters={"5", "11"})
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    # samples_succeeded should be less than 8 (some failed).
    assert metadata["samples_succeeded"] < 8
    assert metadata["samples_attempted"] == 8


def test_probe_chapter_aggregate_all_chapters_fail_returns_zero():
    """Every chapter fails → composite 0.0 with samples=0/8 (the v5 broken-
    CDN equivalent of v4's samples=0/5 case). Required for rizzcomic
    override to detect the bottom-out."""
    chapters = _mk_chapters(20)
    handler = _MockHandler(chapters, fail_get_images=True)
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    assert score == 0.0
    assert metadata["samples_succeeded"] == 0
    assert metadata["samples_attempted"] == 8
    assert metadata.get("cdn_reliability") == 0.0


def test_probe_chapter_aggregate_returns_none_on_empty_chapters():
    """No chapters → orchestrator falls back to cover probe (return None)."""
    handler = _MockHandler([])
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is None


def test_probe_chapter_aggregate_throttle_tail_detects_cdn_failure():
    """The throttle-probe tail re-fetches THROTTLE_TAIL_PAGES more pages
    from the highest-scoring chapter. When those fail, cdn_reliability < 1.0
    (the rizzchoros.cloud detection signal)."""
    handler = _MockHandler(_mk_chapters(20), fail_throttle_tail=True)
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    # Breadth phase should succeed (8 chapters all got their first page).
    assert metadata["samples_succeeded"] == 8
    # But the throttle tail failed → cdn_reliability < 1.0.
    assert metadata.get("cdn_reliability") is not None
    assert metadata["cdn_reliability"] == 0.0


def test_probe_chapter_aggregate_chapter_indices_recorded():
    """The chapter_indices_sampled metadata field records which absolute
    chapter indices were probed — used for cache audit / debugging."""
    handler = _MockHandler(_mk_chapters(20))
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    _, metadata = result
    assert "chapter_indices_sampled" in metadata
    indices = metadata["chapter_indices_sampled"]
    assert len(indices) == 8
    # Indices should be in [1, 18] (first/last trimmed from 20).
    assert all(1 <= i <= 18 for i in indices)


def test_probe_chapter_aggregate_metadata_aggregates_t1_components():
    """The v5 aggregate metadata should include mean values for each T1
    component (res_norm, jpeg_qf, blockiness, fft_hf_ratio, tenengrad)
    across successful samples."""
    handler = _MockHandler(_mk_chapters(20))
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    _, metadata = result
    for field in ("res_norm", "blockiness", "fft_hf_ratio", "tenengrad",
                  "tenengrad_norm", "jpeg_qf", "jpeg_qf_norm", "t1_score"):
        assert field in metadata, f"v5 aggregate metadata missing field: {field}"


def test_probe_chapter_aggregate_v5_throttle_tail_skipped_when_no_success():
    """When EVERY breadth sample fails the chapter list is unreachable from
    the start; the throttle tail can't run because there's no highest-
    scoring chapter to probe. cdn_reliability is 0.0 from the failure path,
    not None."""
    handler = _MockHandler(_mk_chapters(20), fail_get_images=True)
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    _, metadata = result
    assert metadata["cdn_reliability"] == 0.0


# ---------------------------------------------------------------------------
# v5.2 — timeouts (slowness) are EXCLUDED from the score; genuine failures kept
# ---------------------------------------------------------------------------
# The bug: a slow-but-healthy site (atsumaru) whose breadth-probe budget expired
# had its un-reached chapters scored 0.0, dragging an ~0.8 source down to ~0.10.
# The fix distinguishes "couldn't measure in time" (timeout / budget-miss →
# EXCLUDED) from "measured and broken" (empty list / 4xx / non-image → 0.0).
# _mk_chapters(10) samples chapters 2..9 exactly (see the picker output), so
# timing out a known label subset is deterministic.

def test_probe_aggregate_timeouts_excluded_from_score():
    """Chapters whose get_chapter_images TIMES OUT are dropped from the
    aggregate, not scored 0.0 — the score reflects only the measured pages."""
    handler = _MockHandler(_mk_chapters(10), timeout_chapters={"2", "4", "6"})
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    # 3 of the 8 sampled chapters (2, 4, 6) timed out; 5 succeeded.
    assert metadata["samples_attempted"] == 8
    assert metadata["samples_timed_out"] == 3
    assert metadata["samples_succeeded"] == 5
    assert metadata["samples_measured"] == 5
    # Score is the healthy median of the 5 successes — NOT dragged toward 0.
    # (The pre-fix mean over 8 with three 0.0s would sit in the red band.)
    assert 0.3 < score < 1.0


def test_probe_aggregate_fetch_level_timeout_excluded():
    """A timeout in the single-image FETCH (not get_chapter_images) is also
    treated as not-measured via the _ex (None, True) path."""
    handler = _MockHandler(_mk_chapters(10), timeout_fetch_chapters={"3", "5"})
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    assert metadata["samples_timed_out"] == 2
    assert metadata["samples_succeeded"] == 6
    assert 0.3 < score < 1.0


def test_probe_aggregate_all_timeouts_returns_none():
    """When EVERY sampled chapter times out we measured nothing — return None
    (→ orchestrator falls back to cover/seed), NOT a fake 0.0 broken verdict."""
    handler = _MockHandler(_mk_chapters(10), timeout_all_get_images=True)
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is None


def test_probe_aggregate_mixed_genuine_failure_and_timeout():
    """Genuine failures (RuntimeError) stay a scored 0.0 (pulling the mean down —
    the broken-CDN signal), while timeouts are excluded. Verifies the two are
    NOT conflated."""
    handler = _MockHandler(
        _mk_chapters(10),
        fail_chapters={"3"},          # genuine failure -> 0.0, KEPT in the mean
        timeout_chapters={"5", "7"},  # timeouts -> EXCLUDED
    )
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    assert metadata["samples_timed_out"] == 2      # chapters 5, 7
    assert metadata["samples_succeeded"] == 5      # 2, 4, 6, 8, 9
    # measured = 5 successes + 1 genuine failure (chapter 3) = 6; timeouts excluded.
    assert metadata["samples_measured"] == 6
    # The genuine failure forces the MEAN branch, so the single 0.0 drags the
    # score below the all-success median but keeps it > 0.
    assert 0.0 < score < 1.0


def test_probe_aggregate_genuine_failures_still_return_zero_not_none():
    """All-GENUINE-failure (no timeouts) still returns a measured 0.0 (the
    rizzchoros / rizzcomic contract), NOT None — only all-TIMEOUT returns None."""
    handler = _MockHandler(_mk_chapters(10), fail_get_images=True)
    hit = SearchHit(site="mock", title="Mock", url="https://mock/series")
    result = handler._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    assert score == 0.0
    assert metadata["samples_succeeded"] == 0
    assert metadata["samples_timed_out"] == 0
    assert metadata["cdn_reliability"] == 0.0
