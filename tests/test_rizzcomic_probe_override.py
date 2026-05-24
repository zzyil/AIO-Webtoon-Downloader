"""Tests for sites/rizzcomic.py probe overrides (v5).

rizzcomic.com's CDN (rizzchoros.cloud) historically poisons requests —
serving first-page-per-chapter fine but throttling everything after.
This handler overrides _probe_chapter_aggregate to:
  1. Convert parent's None return to (0.0, samples=0/0) so cover-fallback
     can't camouflage the broken CDN via the 3023px cover JPEG (~0.85).
  2. v5 addition: when breadth scoring looks fine (e.g. 0.7) but the
     throttle-probe tail's cdn_reliability is 0.0, cap composite to 0.1
     and flag outlier="throttle_detected". This handles the case where
     breadth happens to hit cache shards but real chapter downloads
     would still fail.

Cross-file: targets sites/rizzcomic.py:RizzComicSiteHandler.
Plan reference: ~/.claude/plans/how-robust-is-the-memoized-koala.md
(Phase 5 section).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sites.rizzcomic import RizzComicSiteHandler


def _mk_handler() -> RizzComicSiteHandler:
    return RizzComicSiteHandler()


def test_rizzcomic_force_zero_on_parent_none():
    """When parent's _probe_chapter_aggregate returns None (hard failure
    pre-fetch-loop), the override emits (0.0, samples=0/0, cdn_reliability=0)."""
    h = _mk_handler()
    hit = MagicMock()
    with patch(
        "sites.mangathemesia.MangaThemesiaSiteHandler._probe_chapter_aggregate",
        return_value=None,
    ):
        result = h._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    assert result is not None
    score, metadata = result
    assert score == 0.0
    assert metadata["samples_attempted"] == 0
    assert metadata["samples_succeeded"] == 0
    assert metadata["format"] == "FAILED"
    assert metadata["cdn_reliability"] == 0.0


def test_rizzcomic_passes_through_zero_samples_succeeded():
    """When parent returns (0.0, samples=0/8) from the v5 breadth-failure
    path, the override should pass it through unchanged (the v5 base
    already returns the right shape on total failure)."""
    h = _mk_handler()
    hit = MagicMock()
    parent_result = (0.0, {
        "width": 0, "height": 0, "format": "FAILED", "size_bytes": 0,
        "samples_attempted": 8, "samples_succeeded": 0,
        "cdn_reliability": 0.0,
    })
    with patch(
        "sites.mangathemesia.MangaThemesiaSiteHandler._probe_chapter_aggregate",
        return_value=parent_result,
    ):
        result = h._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    score, metadata = result
    # 0.0 score is already at the floor — override shouldn't modify it.
    assert score == 0.0
    assert metadata["samples_succeeded"] == 0


def test_rizzcomic_short_circuits_when_cdn_reliability_zero_but_breadth_fine():
    """v5 throttle-detection case: breadth phase scored well (0.7) because
    the CDN happened to serve each first-page request fine, but the
    throttle-probe tail (3 sequential fetches from one chapter) shows
    cdn_reliability=0.0. The override caps composite at 0.1 and flags
    outlier='throttle_detected'."""
    h = _mk_handler()
    hit = MagicMock()
    parent_result = (0.7, {
        "width": 1500, "height": 2000, "format": "JPEG", "size_bytes": 300000,
        "samples_attempted": 8, "samples_succeeded": 8,
        "cdn_reliability": 0.0,  # throttle tail failed
        "t1_score": 0.7,
    })
    with patch(
        "sites.mangathemesia.MangaThemesiaSiteHandler._probe_chapter_aggregate",
        return_value=parent_result,
    ):
        result = h._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    score, metadata = result
    assert score == 0.1, f"expected throttle bottom-out to 0.1, got {score}"
    assert metadata["outlier"] == "throttle_detected"
    # Original parent metadata should otherwise be preserved.
    assert metadata["samples_succeeded"] == 8


def test_rizzcomic_does_not_cap_when_cdn_reliability_partial():
    """cdn_reliability=0.33 (1 of 3 tail pages succeeded) is degraded but
    not totally broken — the override should NOT cap the score. Only the
    total-failure case (cdn_reliability == 0) triggers the bottom-out."""
    h = _mk_handler()
    hit = MagicMock()
    parent_result = (0.7, {
        "width": 1500, "height": 2000, "format": "JPEG", "size_bytes": 300000,
        "samples_attempted": 8, "samples_succeeded": 8,
        "cdn_reliability": 0.33,
    })
    with patch(
        "sites.mangathemesia.MangaThemesiaSiteHandler._probe_chapter_aggregate",
        return_value=parent_result,
    ):
        result = h._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    score, _ = result
    assert score == 0.7, f"partial throttle should NOT bottom out, got {score}"


def test_rizzcomic_does_not_cap_when_cdn_reliability_full():
    """cdn_reliability=1.0 (healthy CDN) means no throttling. Composite
    passes through unchanged."""
    h = _mk_handler()
    hit = MagicMock()
    parent_result = (0.65, {
        "width": 1500, "height": 2000, "format": "JPEG", "size_bytes": 300000,
        "samples_attempted": 8, "samples_succeeded": 8,
        "cdn_reliability": 1.0,
    })
    with patch(
        "sites.mangathemesia.MangaThemesiaSiteHandler._probe_chapter_aggregate",
        return_value=parent_result,
    ):
        result = h._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    score, metadata = result
    assert score == 0.65
    # No throttle_detected outlier on healthy paths.
    assert metadata.get("outlier") != "throttle_detected"


def test_rizzcomic_does_not_cap_when_cdn_reliability_missing():
    """Older parents (or breadth probes that couldn't run a tail) emit
    cdn_reliability=None. The override must not trigger on missing/null
    values — only on the explicit 0.0 throttle signal."""
    h = _mk_handler()
    hit = MagicMock()
    parent_result = (0.7, {
        "width": 1500, "height": 2000, "format": "JPEG", "size_bytes": 300000,
        "samples_attempted": 8, "samples_succeeded": 8,
        "cdn_reliability": None,
    })
    with patch(
        "sites.mangathemesia.MangaThemesiaSiteHandler._probe_chapter_aggregate",
        return_value=parent_result,
    ):
        result = h._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    score, _ = result
    assert score == 0.7


def test_rizzcomic_cover_probe_always_returns_none():
    """The cover-probe override is belt-and-suspenders: even if the
    orchestrator's cover-fallback path somehow fires, no cover bytes
    are produced for rizzcomic."""
    h = _mk_handler()
    assert h._probe_cover_image(MagicMock(), MagicMock(), MagicMock()) is None


def test_rizzcomic_low_breadth_score_not_artificially_raised():
    """A breadth score of 0.05 (real measured-low) should NOT get raised
    by the override. The cap is min(score, 0.1) only when cdn_reliability=0,
    not max(score, 0.1)."""
    h = _mk_handler()
    hit = MagicMock()
    parent_result = (0.05, {
        "width": 800, "height": 1200, "format": "JPEG", "size_bytes": 50000,
        "samples_attempted": 8, "samples_succeeded": 8,
        "cdn_reliability": 0.0,
    })
    with patch(
        "sites.mangathemesia.MangaThemesiaSiteHandler._probe_chapter_aggregate",
        return_value=parent_result,
    ):
        result = h._probe_chapter_aggregate(hit, MagicMock(), MagicMock())
    score, _ = result
    # 0.05 should be left alone — the cap is only applied when score > 0.1.
    assert score == 0.05
