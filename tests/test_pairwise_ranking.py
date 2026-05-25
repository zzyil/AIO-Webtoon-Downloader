"""Tests for v6 anchor-free pairwise T3 ranking
(sites/search_orchestrator.py:_run_pairwise_ranking and its helper
_compute_pairwise_components).

The full _run_pairwise_ranking function does HTTP fetches via handler
methods, so end-to-end tests heavily mock those. The unit tests target:
  - _compute_pairwise_components returns the expected schema
  - Empty / degenerate inputs are no-ops
  - Win-rate aggregation produces correct ranking sign
  - JPEG-ghost penalty still applies

Real-world validation happens in the integration test in Phase 3
(test_t1_bw_scoring's _make_bw_test_blob) and in live searches.
"""
from __future__ import annotations

import io
from typing import List
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw

from sites.search_orchestrator import (
    PAIRWISE_MAX_ADJ,
    SeriesCandidate,
    SourceEntry,
    _compute_pairwise_components,
    _run_pairwise_ranking,
    _PAIRWISE_COMPONENT_KEYS,
)


# --- Synthetic test blobs ---------------------------------------------------


def _make_test_jpeg(size=(1500, 1350), quality=85, screentone=True):
    """Build a JPEG blob mimicking a B&W manga page."""
    img = Image.new("L", size, 240)
    draw = ImageDraw.Draw(img)
    w, h = size
    if screentone:
        for y in range(h // 3, 2 * h // 3, 5):
            for x in range(w // 4, 3 * w // 4, 5):
                draw.ellipse([x - 1, y - 1, x + 1, y + 1], fill=110)
    for i in range(5):
        col = (i + 1) * w // 6
        draw.line([(col, 50), (col, h - 50)], fill=20, width=2)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# --- _compute_pairwise_components ------------------------------------------


def test_compute_pairwise_components_returns_full_schema():
    """All COMPONENT_KEYS must be present in the returned dict (None when
    unavailable for the content type).
    """
    blob = _make_test_jpeg()
    components = _compute_pairwise_components(blob, content_type="bw_manga")
    assert components is not None
    for key in _PAIRWISE_COMPONENT_KEYS:
        assert key in components


def test_compute_pairwise_components_bw_includes_screentone_and_line():
    """For B&W content, the screentone / line / bg_uniformity slots are
    populated."""
    blob = _make_test_jpeg()
    components = _compute_pairwise_components(blob, content_type="bw_manga")
    assert components["screentone"] is not None
    assert components["line"] is not None
    assert components["bg_uniformity"] is not None
    # B&W skips the fft_hf slot (subsumed by screentone).
    assert components["fft_hf"] is None


def test_compute_pairwise_components_color_uses_fft_hf():
    """For color_manga content, the legacy formula fires → fft_hf
    populated, B&W-specific slots None."""
    blob = _make_test_jpeg()
    components = _compute_pairwise_components(blob, content_type="color_manga")
    assert components["fft_hf"] is not None
    assert components["screentone"] is None
    assert components["line"] is None


def test_compute_pairwise_components_returns_none_on_corrupt_blob():
    components = _compute_pairwise_components(b"not an image at all" * 50, "bw_manga")
    assert components is None


def test_compute_pairwise_components_returns_none_on_tiny():
    """PIL-makeable but too small to score (<100 px either axis)."""
    img = Image.new("L", (50, 50), 128)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    components = _compute_pairwise_components(buf.getvalue(), "bw_manga")
    assert components is None


def test_compute_pairwise_components_jpeg_quality_in_qf_slot():
    """Higher JPEG quality → higher qf_or_lossless value."""
    blob_q90 = _make_test_jpeg(quality=90)
    blob_q40 = _make_test_jpeg(quality=40)
    c_q90 = _compute_pairwise_components(blob_q90, "bw_manga")
    c_q40 = _compute_pairwise_components(blob_q40, "bw_manga")
    assert c_q90 is not None and c_q40 is not None
    assert c_q90["qf_or_lossless"] > c_q40["qf_or_lossless"]


# --- _run_pairwise_ranking — degenerate / no-op cases ----------------------


def test_run_pairwise_ranking_empty_candidates_noop():
    _run_pairwise_ranking([], MagicMock(), MagicMock())
    # No exception, no return value. Implicit success.


def test_run_pairwise_ranking_single_source_skipped():
    """A candidate with only one measured source → no adjustment can
    happen, function returns without crashing."""
    src = SourceEntry(
        site="alpha",
        url="http://a/x",
        title="X",
        title_match=1.0,
        cover=None,
        seed_quality=0.8,
        img_quality_score=0.6,
        img_quality_metadata={"samples_succeeded": 5, "content_type": "bw_manga"},
    )
    cand = SeriesCandidate(
        canonical_title="X", canonical_year=None, sources=[src],
    )
    _run_pairwise_ranking([cand], MagicMock(), MagicMock())
    # img_quality_score unchanged.
    assert src.img_quality_score == 0.6
    # No pairwise metadata added (function exited before bucket loop).
    assert "pairwise_adjustment" not in (src.img_quality_metadata or {})


def test_run_pairwise_ranking_skips_sources_below_min_score():
    """Two sources both below PAIRWISE_MIN_SOURCE_SCORE → no pairing."""
    s1 = SourceEntry(
        site="alpha", url="http://a/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.20,
        img_quality_metadata={"samples_succeeded": 3, "content_type": "bw_manga"},
    )
    s2 = SourceEntry(
        site="beta", url="http://b/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.25,
        img_quality_metadata={"samples_succeeded": 3, "content_type": "bw_manga"},
    )
    cand = SeriesCandidate(
        canonical_title="X", canonical_year=None, sources=[s1, s2],
    )
    _run_pairwise_ranking([cand], MagicMock(), MagicMock())
    # Both scores unchanged.
    assert s1.img_quality_score == 0.20
    assert s2.img_quality_score == 0.25


def test_run_pairwise_ranking_skips_sources_without_real_probe():
    """Two sources both eligible by score but neither has
    samples_succeeded > 0 (cover-probe fallback only) → no pairing."""
    s1 = SourceEntry(
        site="alpha", url="http://a/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.85,
        img_quality_metadata={"samples_succeeded": 0, "content_type": "bw_manga"},
    )
    s2 = SourceEntry(
        site="beta", url="http://b/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.83,
        img_quality_metadata={"content_type": "bw_manga"},  # missing samples_succeeded
    )
    cand = SeriesCandidate(
        canonical_title="X", canonical_year=None, sources=[s1, s2],
    )
    _run_pairwise_ranking([cand], MagicMock(), MagicMock())
    assert s1.img_quality_score == 0.85
    assert s2.img_quality_score == 0.83


def test_run_pairwise_ranking_skips_unknown_bucket():
    """Sources with content_type='unknown' map to the unknown_bucket,
    which is excluded from pairing."""
    s1 = SourceEntry(
        site="alpha", url="http://a/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.7,
        img_quality_metadata={"samples_succeeded": 3, "content_type": "unknown"},
    )
    s2 = SourceEntry(
        site="beta", url="http://b/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.6,
        img_quality_metadata={"samples_succeeded": 3, "content_type": "unknown"},
    )
    cand = SeriesCandidate(
        canonical_title="X", canonical_year=None, sources=[s1, s2],
    )
    _run_pairwise_ranking([cand], MagicMock(), MagicMock())
    assert s1.img_quality_score == 0.7
    assert s2.img_quality_score == 0.6


# --- _run_pairwise_ranking — pairwise win counting (mocked I/O) -----------


def _make_mock_handler(site, url, chapter_nums, page_blob):
    """Build a MagicMock handler with the methods _run_pairwise_ranking
    calls: fetch_comic_context, get_chapters, get_chapter_images,
    _pick_random_middle_page_index, _fetch_probe_item_bytes.

    chapter_nums: list of ints to make available as chapters.
    page_blob: bytes returned by _fetch_probe_item_bytes for any page.
    """
    handler = MagicMock()
    handler.fetch_comic_context.return_value = {"site": site, "url": url}
    handler.get_chapters.return_value = [
        {"chap": str(n), "title": f"Chapter {n}", "url": f"{url}/ch/{n}"}
        for n in chapter_nums
    ]
    handler.get_chapter_images.return_value = [f"{url}/img/0.jpg", f"{url}/img/1.jpg"]
    handler._pick_random_middle_page_index.return_value = 0
    handler._fetch_probe_item_bytes.return_value = page_blob
    return handler


def test_run_pairwise_ranking_a_beats_b_on_higher_quality(monkeypatch):
    """Source A has a high-quality blob, source B has a low-quality blob.
    After pairwise comparison, A's adjustment should be positive,
    B's should be negative.
    """
    blob_hq = _make_test_jpeg(quality=95)
    blob_lq = _make_test_jpeg(quality=20)
    handler_a = _make_mock_handler("alpha", "http://a/x", [1, 2, 3], blob_hq)
    handler_b = _make_mock_handler("beta", "http://b/x", [1, 2, 3], blob_lq)

    def _get_handler(name):
        return {"alpha": handler_a, "beta": handler_b}.get(name)

    import sites
    monkeypatch.setattr(sites, "get_handler_by_name", _get_handler)

    s1 = SourceEntry(
        site="alpha", url="http://a/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.60,
        img_quality_metadata={"samples_succeeded": 5, "content_type": "bw_manga"},
    )
    s2 = SourceEntry(
        site="beta", url="http://b/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.55,
        img_quality_metadata={"samples_succeeded": 5, "content_type": "bw_manga"},
    )
    cand = SeriesCandidate(
        canonical_title="X", canonical_year=None, sources=[s1, s2],
    )

    def _scraper_factory(h):
        return MagicMock()

    _run_pairwise_ranking([cand], _scraper_factory, MagicMock())

    # A should have gained, B should have lost.
    assert s1.img_quality_score > 0.60, (
        f"A should improve: now {s1.img_quality_score}, meta={s1.img_quality_metadata}"
    )
    assert s2.img_quality_score < 0.55, (
        f"B should drop: now {s2.img_quality_score}, meta={s2.img_quality_metadata}"
    )
    # Both have pairwise metadata populated.
    assert s1.img_quality_metadata["pairwise_winrate"] > 0.5
    assert s2.img_quality_metadata["pairwise_winrate"] < 0.5


def test_run_pairwise_ranking_identical_sources_get_zero_adjustment(monkeypatch):
    """Two sources serving the same blob → winrate ≈ 0.5 → adj ≈ 0
    (modulo JPEG-ghost which might be small)."""
    blob = _make_test_jpeg(quality=85)
    handler_a = _make_mock_handler("alpha", "http://a/x", [1, 2, 3], blob)
    handler_b = _make_mock_handler("beta", "http://b/x", [1, 2, 3], blob)

    def _get_handler(name):
        return {"alpha": handler_a, "beta": handler_b}.get(name)

    import sites
    monkeypatch.setattr(sites, "get_handler_by_name", _get_handler)

    s1 = SourceEntry(
        site="alpha", url="http://a/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.6,
        img_quality_metadata={"samples_succeeded": 5, "content_type": "bw_manga"},
    )
    s2 = SourceEntry(
        site="beta", url="http://b/x", title="X", title_match=1.0,
        cover=None, seed_quality=0.5, img_quality_score=0.6,
        img_quality_metadata={"samples_succeeded": 5, "content_type": "bw_manga"},
    )
    cand = SeriesCandidate(
        canonical_title="X", canonical_year=None, sources=[s1, s2],
    )

    def _scraper_factory(h):
        return MagicMock()

    _run_pairwise_ranking([cand], _scraper_factory, MagicMock())

    # Both got the same blob — all components tie → 0 wins, 0 losses → winrate=None
    # or both winrate=0.5 if non-tied. Adjustment from win-rate should be ~0.
    # The ghost penalty is the same for both (same blob) so it's identical.
    pairwise_adj_a = s1.img_quality_metadata.get("pairwise_adjustment", 0.0) or 0.0
    pairwise_adj_b = s2.img_quality_metadata.get("pairwise_adjustment", 0.0) or 0.0
    # Allow up to 0.05 deviation due to ghost penalty differences (same
    # blob → same ghost penalty so this should actually be zero).
    assert abs(pairwise_adj_a - pairwise_adj_b) < 0.001, (
        f"identical sources should get identical adjustments: "
        f"A={pairwise_adj_a}, B={pairwise_adj_b}"
    )


# --- Constants sanity check ------------------------------------------------


def test_pairwise_max_adj_within_bounds():
    """PAIRWISE_MAX_ADJ must be small enough to not flip rankings on a
    single misfire. v5.1 used 0.15; v6 preserves that."""
    assert 0.05 <= PAIRWISE_MAX_ADJ <= 0.20


def test_pairwise_component_keys_includes_all_expected():
    expected = {"res_norm", "qf_or_lossless", "screentone", "line",
                "bg_uniformity", "block_inv", "fft_hf", "tenengrad"}
    assert set(_PAIRWISE_COMPONENT_KEYS) == expected
