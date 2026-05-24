"""Tests for Tier-1 image-quality scoring (sites/search_orchestrator.py v5).

The T1 layer replaces the legacy `0.4*res + 0.3*format + 0.3*decode` formula
with an objective 5-component composite. These tests use REAL manga images
from tmp_nzmj/ (raw MangaFire JPEG @ 1114x1584 ~q85) wherever possible
because synthetic content with periodic line patterns aliases against the
8-pixel JPEG block detector (any hatching at periods coprime with 8 is
fine; multiples of 4 or 8 are not — period 6 hatching trips n/6=4/8=n/8).

Real-world data layout (per user note 2026-05-17):
  - tmp_nzmj/ch_1/1_0001.jpg, 1_0002.jpg, 1_0003.jpg → colored cover spreads
  - tmp_nzmj/ch_1/1_0004.jpg → near-uniform black title page with logo
  - tmp_nzmj/ch_1/1_0005.jpg onward → classic B&W manga line-art
  - mangas/Talentless Nana (hid=nzmj)/Ch.005...cbz → CBZ-packed (post-process)

Cross-file: targets sites/search_orchestrator.py:_compute_t1_score and its
helpers. Plan reference: ~/.claude/plans/how-robust-is-the-memoized-koala.md.
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image, ImageDraw, ImageFilter

from sites.search_orchestrator import (
    _compute_blockiness_wang,
    _compute_fft_hf_ratio,
    _compute_t1_score,
    _compute_tenengrad,
    _detect_lossless_blob,
    _estimate_jpeg_qf_lsm,
    _is_grayscale_pil,
    _score_image_blob,
)


# ---------------------------------------------------------------------------
# Real-world fixtures
# ---------------------------------------------------------------------------

# Project root resolved relative to this test file. Lives at
# C:\Users\legoc\OneDrive\Belgeler\AIO-Webtoon-Downloader\tests\test_t1_scoring.py
# so root is two parents up... actually one parent: tests/ is at root level.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_DIR = os.path.join(_REPO_ROOT, "tmp_nzmj")
_CBZ_DIR = os.path.join(_REPO_ROOT, "mangas", "Talentless Nana (hid=nzmj)")


def _has_test_data() -> bool:
    """Probe whether the real-world test fixtures exist on this machine.

    Tests that depend on real data skip gracefully when they don't (CI
    won't have these, only the dev's local checkout). The synthetic
    component tests below stay runnable regardless.
    """
    return (
        os.path.isdir(_TMP_DIR)
        and os.path.isfile(os.path.join(_TMP_DIR, "ch_1", "1_0001.jpg"))
        and os.path.isfile(os.path.join(_TMP_DIR, "ch_5", "5_0021.jpg"))
    )


_NEED_REAL = pytest.mark.skipif(
    not _has_test_data(),
    reason="tmp_nzmj real-world test data not present (dev-only fixtures)",
)


def _read_blob(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# Convenience accessors for common test pages.
def _bw_page_blob() -> bytes:
    """A canonical B&W manga page from ch_5 — Talentless Nana mid-chapter,
    typical line-art + screentone shading. The 'normal source quality'
    reference for ranking tests."""
    return _read_blob(os.path.join(_TMP_DIR, "ch_5", "5_0021.jpg"))


def _color_cover_blob() -> bytes:
    """Page 1 of ch_1 — color spread cover. Used for color-path tests."""
    return _read_blob(os.path.join(_TMP_DIR, "ch_1", "1_0001.jpg"))


def _title_page_blob() -> bytes:
    """Page 4 of ch_1 — near-uniform black title page with small logo.
    Used to validate low-edge-density scoring."""
    return _read_blob(os.path.join(_TMP_DIR, "ch_1", "1_0004.jpg"))


def _reencode_at_q(blob: bytes, quality: int) -> bytes:
    """Decode a JPEG and re-encode at a specific quality. Lets us test
    monotonic ranking of the same content across the quality axis."""
    img = Image.open(io.BytesIO(blob))
    img.load()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Synthetic fixtures (component-level tests where we need controlled input)
# ---------------------------------------------------------------------------

def _make_uniform(w: int, h: int, gray: int = 128) -> Image.Image:
    return Image.new("L", (w, h), gray)


def _make_random_noise(w: int, h: int, seed: int = 42) -> Image.Image:
    """Random uniform noise — flat spectrum, no aliasing with 8-px JPEG grid."""
    import numpy as np
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
    return Image.fromarray(arr, mode="L")


def _make_8x8_grid(size: int = 256) -> Image.Image:
    """High-contrast 8x8 grid pattern — known-block-aligned signal for the
    Wang detector. Should score blockiness > 0.3."""
    img = Image.new("L", (size, size), 200)
    px = img.load()
    for x in range(size):
        for y in range(size):
            if x % 8 == 0 or y % 8 == 0:
                px[x, y] = 30
    return img


# ---------------------------------------------------------------------------
# Component: Tenengrad sharpness
# ---------------------------------------------------------------------------

def test_tenengrad_high_for_edges_low_for_uniform():
    """Uniform image: ~0. Random noise: substantial. Validates the operator's
    response scales with edge density."""
    uniform = _make_uniform(800, 1000)
    raw_u, norm_u = _compute_tenengrad(uniform)
    assert raw_u < 1.0
    assert norm_u < 0.05

    noisy = _make_random_noise(800, 1000)
    raw_n, _ = _compute_tenengrad(noisy)
    assert raw_n > 50.0, f"random noise should have high gradient, got {raw_n}"


@_NEED_REAL
def test_tenengrad_blurred_lower_than_sharp_real_page():
    """A blurred copy of a real manga page scores lower than the original.
    This is the upscale/blur-detection signal — heavily-recompressed pages
    have smeared edges and read low here."""
    img = Image.open(io.BytesIO(_bw_page_blob()))
    img.load()
    blurred = img.filter(ImageFilter.GaussianBlur(radius=3))
    _, norm_sharp = _compute_tenengrad(img)
    _, norm_blur = _compute_tenengrad(blurred)
    assert norm_blur < norm_sharp, (
        f"blurred={norm_blur} should be < sharp={norm_sharp}"
    )


# ---------------------------------------------------------------------------
# Component: FFT high-frequency ratio (upscale detector)
# ---------------------------------------------------------------------------

def test_fft_hf_ratio_high_for_noise_low_for_smooth():
    """Random noise: high outer-ring energy. Uniform: near 0."""
    noisy = _make_random_noise(800, 1000)
    smooth = _make_uniform(800, 1000)
    r_noise = _compute_fft_hf_ratio(noisy)
    r_smooth = _compute_fft_hf_ratio(smooth)
    assert r_noise > 0.5
    assert r_smooth < 0.1


@_NEED_REAL
def test_fft_hf_ratio_upscaled_lower_than_native_real_page():
    """The canonical upscale-detection test on real manga: downscale a page
    to 40% then back to native — high-frequency content destroyed."""
    img = Image.open(io.BytesIO(_bw_page_blob()))
    img.load()
    w, h = img.size
    small = img.resize((w * 2 // 5, h * 2 // 5), Image.LANCZOS)
    upscaled = small.resize((w, h), Image.LANCZOS)
    r_native = _compute_fft_hf_ratio(img)
    r_upscaled = _compute_fft_hf_ratio(upscaled)
    assert r_upscaled < r_native, (
        f"upscaled={r_upscaled} should be < native={r_native}"
    )
    # The gap should be substantial — upscaled real manga loses ~20-40% of
    # its high-freq energy. If the gap is <0.05, the detector's not working.
    assert r_native - r_upscaled > 0.05


# ---------------------------------------------------------------------------
# Component: Wang blockiness
# ---------------------------------------------------------------------------

def test_blockiness_picks_up_8x8_pattern():
    """Explicit 8x8 grid pattern → high blockiness. Validates the FFT-bin
    targeting of the detector."""
    img = _make_8x8_grid(256)
    score = _compute_blockiness_wang(img)
    assert score > 0.3, f"explicit 8x8 grid scored {score}, expected >0.3"


def test_blockiness_low_for_random_noise():
    """Random noise has flat spectrum — no n/8 peaks → low blockiness.
    Uniform image too. (Synthetic content with periodic features at periods
    coprime with 8 is fine; periodic content at periods 4/8/2 would alias.)"""
    score_noise = _compute_blockiness_wang(_make_random_noise(256, 256))
    score_uniform = _compute_blockiness_wang(_make_uniform(256, 256))
    assert score_noise < 0.15, f"random noise should be ~0 blockiness, got {score_noise}"
    assert score_uniform < 0.1


@_NEED_REAL
def test_blockiness_higher_for_low_quality_jpeg_real():
    """Real manga page re-encoded at q=15 should expose visible block
    edges; q=95 should not. This is the canonical aggregator-detection
    case — pages that have been re-saved multiple times accumulate
    block artifacts."""
    blob_q95 = _reencode_at_q(_bw_page_blob(), quality=95)
    blob_q15 = _reencode_at_q(_bw_page_blob(), quality=15)
    img_q95 = Image.open(io.BytesIO(blob_q95)); img_q95.load()
    img_q15 = Image.open(io.BytesIO(blob_q15)); img_q15.load()
    b_q95 = _compute_blockiness_wang(img_q95)
    b_q15 = _compute_blockiness_wang(img_q15)
    assert b_q15 > b_q95, (
        f"low-q JPEG blockiness ({b_q15}) should exceed high-q ({b_q95})"
    )


# ---------------------------------------------------------------------------
# Component: JPEG QF estimation (Hass 2024 LSM)
# ---------------------------------------------------------------------------

@_NEED_REAL
@pytest.mark.parametrize("encode_q", [30, 50, 75, 90, 95])
def test_jpeg_qf_lsm_recovers_encode_quality_real(encode_q):
    """LSM should back-solve the encode quality within ±10. Uses a real
    manga page as the source so the QT data is realistic (not biased by
    PIL's default round-tripping)."""
    blob = _reencode_at_q(_bw_page_blob(), quality=encode_q)
    img = Image.open(io.BytesIO(blob)); img.load()
    qf, nse = _estimate_jpeg_qf_lsm(blob, img)
    assert qf is not None, f"LSM returned None for q={encode_q}"
    assert nse is not None and nse > 0.5, (
        f"NSE should be high for standard PIL JPEG (q={encode_q}), got {nse}"
    )
    assert abs(qf - encode_q) <= 10, (
        f"LSM estimated {qf}, expected {encode_q} ±10"
    )


def test_jpeg_qf_lsm_returns_none_for_non_jpeg():
    """Passing a PNG-derived PIL image yields no QT → returns (None, None)."""
    img = _make_random_noise(400, 400)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    blob = buf.getvalue()
    pil_img = Image.open(io.BytesIO(blob))
    pil_img.load()
    qf, nse = _estimate_jpeg_qf_lsm(blob, pil_img)
    assert qf is None
    assert nse is None


# ---------------------------------------------------------------------------
# Composite formula — real-world end-to-end
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_t1_score_monotonic_with_jpeg_quality_real():
    """The single most important T1 contract: re-encoding the same real
    manga page at decreasing JPEG quality must produce decreasing scores.
    Replaces the synthetic-image high/low-quality split tests."""
    original = _bw_page_blob()
    scores_by_q = {}
    for q in [95, 85, 70, 50, 30, 15]:
        result = _score_image_blob(_reencode_at_q(original, q))
        assert result is not None
        scores_by_q[q] = result[0]
    # Each step down should drop the score. Allow a tiny epsilon for the
    # crossing between q=85 and q=70 where the QT may overlap with the
    # original's encoder; the trend over the full range must hold.
    qs = sorted(scores_by_q.keys(), reverse=True)
    for higher, lower in zip(qs, qs[1:]):
        assert scores_by_q[higher] >= scores_by_q[lower] - 0.01, (
            f"score({higher}) = {scores_by_q[higher]} should >= "
            f"score({lower}) = {scores_by_q[lower]}"
        )
    # End-to-end span: q=95 should beat q=15 by a clear margin.
    assert scores_by_q[95] - scores_by_q[15] > 0.15, (
        f"q=95 ({scores_by_q[95]}) vs q=15 ({scores_by_q[15]}) gap too narrow"
    )


@_NEED_REAL
def test_t1_score_real_mangafire_page_in_expected_band():
    """A native MangaFire q~85 page (1114×1584 grayscale JPEG) should land
    in the upper band. v5.1 widened this from v5's 0.50-0.75 to 0.75-0.95
    because v5's width-based res_norm `(w-800)/1600` silently under-scored
    aggregator pages — 1114 wide → res_norm 0.20 → composite ~0.66. v5.1's
    area-based normalization with the 2M-px bw_manga target correctly
    recognizes 1114×1584 ≈ 1.76M px² → res_norm 0.88 → composite ~0.86.
    This is the intentional behavior change for v5.1, not a regression.

    The exact threshold isn't load-bearing — what matters is the band
    staying stable across formula tweaks AND being meaningfully different
    from very-low-quality scores."""
    blob = _bw_page_blob()
    result = _score_image_blob(blob)
    assert result is not None
    score, meta = result
    assert 0.75 <= score <= 0.95, (
        f"native MangaFire page scored {score}, expected v5.1 band 0.75-0.95; meta={meta}"
    )
    assert meta["format"] == "JPEG"
    assert 80 <= meta["jpeg_qf"] <= 95
    assert meta["is_grayscale"] is True  # ch_5 page is B&W
    # v5.1: content_type defaults to "unknown" when called via _score_image_blob
    # directly (no series classification context). The probe orchestrator
    # passes content_type explicitly after _classify_series_content runs.
    assert meta.get("content_type") == "unknown"
    # v5.1: res_norm is now area-based against the per-content-type target.
    assert meta.get("res_norm_target") == 2_000_000
    assert meta.get("res_norm", 0.0) > 0.80  # 1.76M / 2.0M ≈ 0.88


@_NEED_REAL
def test_t1_score_color_cover_correctly_typed():
    """A color cover (ch_1 page 1) should be flagged is_grayscale=False
    and run through the color content branch."""
    blob = _color_cover_blob()
    result = _score_image_blob(blob)
    assert result is not None
    _, meta = result
    assert meta["is_grayscale"] is False
    assert meta["format"] == "JPEG"


@_NEED_REAL
def test_t1_score_title_page_scores_lower_than_content_page():
    """The near-uniform title page (page 4 — black + logo) has very little
    edge density. It should score below a real content page from the same
    chapter, since the T1 formula weights sharpness/fft_hf at 0.30 combined."""
    title_score, title_meta = _score_image_blob(_title_page_blob())
    # Page 21 of ch_5 — middle-of-chapter typical content.
    content_score, _ = _score_image_blob(_bw_page_blob())
    assert title_score < content_score, (
        f"title page ({title_score}) should score < content page ({content_score}); "
        f"title tenengrad={title_meta['tenengrad_norm']}"
    )
    # The title page's sharpness should be visibly degraded.
    assert title_meta["tenengrad_norm"] < 0.3


@_NEED_REAL
def test_t1_score_larger_resolution_outranks_smaller_real():
    """Upscale a real page to 1800px wide and a downscale to 800px wide of
    the same content. Higher res should win (res_norm slot 0.30)."""
    img = Image.open(io.BytesIO(_bw_page_blob()))
    img.load()
    w, h = img.size
    # Downscale to 800 width; encode same q.
    img_sm = img.resize((800, int(h * 800 / w)), Image.LANCZOS)
    buf_sm = io.BytesIO()
    img_sm.save(buf_sm, format="JPEG", quality=85)
    blob_sm = buf_sm.getvalue()
    # Original (1114 wide).
    blob_orig = _reencode_at_q(_bw_page_blob(), quality=85)
    s_orig, _ = _score_image_blob(blob_orig)
    s_sm, _ = _score_image_blob(blob_sm)
    assert s_orig > s_sm, (
        f"1114-wide original ({s_orig}) should beat 800-wide downscale ({s_sm})"
    )


# ---------------------------------------------------------------------------
# Metadata schema — v5 contract
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_t1_score_metadata_shape_v5_real():
    """Every v5 metadata field must be present (may be None). The contract
    is: explicit nulls, not missing keys, so the UI tooltip and v5 cache
    don't need feature-detection."""
    result = _score_image_blob(_bw_page_blob())
    assert result is not None
    _, meta = result
    required_v5_fields = [
        # Content
        "width", "height", "format", "size_bytes", "bpp",
        "is_grayscale", "is_lossless",
        # T1 components
        "t1_score", "res_norm", "blockiness", "fft_hf_ratio",
        "tenengrad", "tenengrad_norm",
        # JPEG specifics
        "jpeg_qf", "jpeg_qf_norm", "jpeg_nse",
        # Back-compat field for UI tooltip
        "decode_quality",
        # T2 placeholders (Phase 3 populates)
        "t2_available", "t2_score", "clip_iqa_scores", "clip_iqa_mean",
        "niqe_score", "niqe_norm",
        # T3 placeholders (Phase 4 populates)
        "paired_quality_adjustment", "paired_anchor_site",
        "paired_dists_median", "paired_pairs_compared",
    ]
    for f in required_v5_fields:
        assert f in meta, f"v5 metadata missing field: {f}"


@_NEED_REAL
def test_t1_score_composite_in_range_real():
    """All real-fixture branches produce composite ∈ [0, 1]."""
    for path in [
        os.path.join(_TMP_DIR, "ch_1", "1_0001.jpg"),  # color cover
        os.path.join(_TMP_DIR, "ch_1", "1_0004.jpg"),  # title page
        os.path.join(_TMP_DIR, "ch_5", "5_0001.jpg"),  # ch start
        os.path.join(_TMP_DIR, "ch_5", "5_0021.jpg"),  # ch middle
        os.path.join(_TMP_DIR, "ch_5", "5_0039.jpg"),  # ch end
        os.path.join(_TMP_DIR, "ch_10", "10_0013.jpg"),  # different chapter
    ]:
        if not os.path.isfile(path):
            continue
        result = _score_image_blob(_read_blob(path))
        assert result is not None, f"failed to score {path}"
        score, _ = result
        assert 0.0 <= score <= 1.0, f"out-of-range score {score} for {path}"


# ---------------------------------------------------------------------------
# Lossless format branch (PNG / WebP-VP8L)
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_t1_score_lossless_png_branch_real():
    """Convert a real manga page to PNG, verify lossless branch is taken
    and decode_quality=1.0."""
    img = Image.open(io.BytesIO(_bw_page_blob()))
    img.load()
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    result = _score_image_blob(buf.getvalue())
    assert result is not None
    _, meta = result
    assert meta["format"] == "PNG"
    assert meta["is_lossless"] is True
    assert meta["decode_quality"] == 1.0
    assert meta["jpeg_qf"] is None


@_NEED_REAL
def test_t1_score_lossless_webp_branch_real():
    """A lossless WebP page should be detected via _detect_lossless_blob."""
    img = Image.open(io.BytesIO(_bw_page_blob()))
    img.load()
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=100, lossless=True, method=4)
    blob = buf.getvalue()
    assert _detect_lossless_blob(blob) is True
    result = _score_image_blob(blob)
    assert result is not None
    _, meta = result
    assert meta["is_lossless"] is True
    assert meta["decode_quality"] == 1.0


# ---------------------------------------------------------------------------
# Lossy WebP branch — uses reweighted formula (no QF available)
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_t1_score_lossy_webp_reweighted_formula_real():
    """A lossy WebP of a real page should run the 0.35+0.20+0.20+0.25
    reweighted formula and have None QF fields."""
    img = Image.open(io.BytesIO(_bw_page_blob()))
    img.load()
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85, lossless=False, method=4)
    result = _score_image_blob(buf.getvalue())
    assert result is not None
    score, meta = result
    assert meta["format"] == "WEBP"
    assert meta["is_lossless"] is False
    assert meta["jpeg_qf"] is None
    assert meta["jpeg_qf_norm"] is None
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Edge cases — degenerate inputs
# ---------------------------------------------------------------------------

def test_t1_score_returns_none_on_tiny_image():
    """Images <100×100 are placeholder/icon, never real content."""
    img = _make_uniform(50, 50)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    assert _score_image_blob(buf.getvalue()) is None


def test_t1_score_returns_none_on_corrupt_blob():
    """Garbage bytes → PIL decode fails → None (orchestrator falls back to seed)."""
    assert _score_image_blob(b"not an image at all" * 50) is None
    assert _score_image_blob(b"") is None
    assert _score_image_blob(b"x" * 100) is None  # too short for header


def test_t1_score_webp_below_floor_outlier_preserved():
    """v4's webp_below_floor outlier flag survives v5 rewrite. Force a
    sub-0.05 bpp WebP by encoding a near-uniform image at q=1."""
    smooth = Image.new("RGB", (1500, 2000), (128, 128, 128))
    buf = io.BytesIO()
    smooth.save(buf, format="WEBP", quality=1, lossless=False, method=4)
    blob = buf.getvalue()
    result = _score_image_blob(blob)
    if result is None:
        pytest.skip("test image too uniform for libwebp to round-trip cleanly")
    score, meta = result
    if meta["bpp"] >= 0.05:
        pytest.skip(f"libwebp on this platform produced bpp={meta['bpp']}; can't test floor")
    assert meta.get("outlier") == "webp_below_floor"
    assert score <= 0.1


# ---------------------------------------------------------------------------
# Format-prejudice removal — sanity check
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_format_no_longer_dominates_score_real():
    """The v4 formula gave JPEG a flat -0.09 penalty vs WebP regardless of
    actual quality (format_bonus weights JPEG=0.55, WebP=0.85). v5 lets
    per-format formulas compete fairly. JPEG at q=95 should not be wildly
    below WebP at q=95 for the same source content (within ~0.15)."""
    img = Image.open(io.BytesIO(_bw_page_blob()))
    img.load()
    buf_j = io.BytesIO()
    img.save(buf_j, format="JPEG", quality=95)
    buf_w = io.BytesIO()
    img.save(buf_w, format="WEBP", quality=95, lossless=False, method=4)
    s_jpeg, _ = _score_image_blob(buf_j.getvalue())
    s_webp, _ = _score_image_blob(buf_w.getvalue())
    # Both should be substantial (above weak tier 0.4).
    assert s_jpeg > 0.45
    assert s_webp > 0.45
    # Gap < 0.20: no large format-prejudice. (v4 penalized JPEG by 0.09 flat;
    # v5 should be within ~0.10-0.15 either way depending on resolution.)
    assert abs(s_jpeg - s_webp) < 0.20, (
        f"JPEG ({s_jpeg}) vs WebP ({s_webp}) gap too wide"
    )
