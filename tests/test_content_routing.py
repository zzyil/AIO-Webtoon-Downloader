"""Tests for v5.1 content-type classification + adaptive res_norm targets.

Uses REAL manga + webtoon fixtures wherever possible (synthetic content
hits known pitfalls: pixel-noise averages to gray under Lanczos downscale,
so chroma_var false-low on what should be color; the _is_grayscale_pil
90th-percentile chroma probe similarly fails on uncorrelated noise).

Real-world fixtures (dev-only — tests skip when absent):
  - tmp_nzmj/   Talentless Nana — 1114×1584 manga, B&W with color cover
                inserts in ch_1 pages 1-3. ch_1/1_0004 is the near-pitch-
                black title page; ch_5+ are typical B&W manga pages.
  - tmp_1571/   Eleceed — 700×1000 / 700×1160 / 690×1280 chunked color
                webtoon. The exact "700→690 width drift across chapters,
                consistent within chapters" case the v5.1 plan was built
                around.

Cross-file: targets sites/search_orchestrator.py:_compute_chroma_var,
_compute_page_content_features, _classify_series_content, _compute_t1_score
+ sites/t1_constants.py. Plan reference:
~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 4 section).
"""

from __future__ import annotations

import io
import os
from typing import Dict, List

import pytest
from PIL import Image

from sites.search_orchestrator import (
    _classify_series_content,
    _compute_chroma_var,
    _compute_page_content_features,
    _compute_t1_score,
    _score_image_blob,
)
from sites import t1_constants


# ---------------------------------------------------------------------------
# Real-world fixture paths
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_NZMJ = os.path.join(_REPO_ROOT, "tmp_nzmj")
_TMP_ELECEED = os.path.join(_REPO_ROOT, "tmp_1571")

# Talentless Nana pages: ch_1 pages 1-3 are color covers; ch_1 page 4 is
# near-pitch-black title (16KB file, mostly black with logo); ch_5+ are
# typical B&W manga content.
_TN_COLOR_COVER = os.path.join(_TMP_NZMJ, "ch_1", "1_0001.jpg")
_TN_COLOR_COVER_2 = os.path.join(_TMP_NZMJ, "ch_1", "1_0002.jpg")
_TN_COLOR_COVER_3 = os.path.join(_TMP_NZMJ, "ch_1", "1_0003.jpg")
_TN_BLACK_TITLE = os.path.join(_TMP_NZMJ, "ch_1", "1_0004.jpg")
_TN_BW_CONTENT = os.path.join(_TMP_NZMJ, "ch_5", "5_0021.jpg")
_TN_BW_CONTENT_2 = os.path.join(_TMP_NZMJ, "ch_10", "10_0013.jpg")

# Eleceed (chunked color webtoon) pages. Width drifts 700 → 700 → 690
# across these three chapters — the canonical v5.1 case.
_EL_CH4_P1 = os.path.join(_TMP_ELECEED, "ch_4", "4_0001.jpg")          # 700×1000
_EL_CH4_MID = os.path.join(_TMP_ELECEED, "ch_4", "4_0056.jpg")        # 700×1000
_EL_CH63_P1 = os.path.join(_TMP_ELECEED, "ch_63", "63_0001.jpg")      # 700×1160
_EL_CH63_MID = os.path.join(_TMP_ELECEED, "ch_63", "63_0042.jpg")     # 700×1190
_EL_CH320_P1 = os.path.join(_TMP_ELECEED, "ch_320", "320_0001.jpg")   # 690×1280
_EL_CH320_MID = os.path.join(_TMP_ELECEED, "ch_320", "320_0055.jpg")  # 690×1280


def _have_files(*paths: str) -> bool:
    return all(os.path.isfile(p) for p in paths)


_NEED_NZMJ = pytest.mark.skipif(
    not _have_files(_TN_COLOR_COVER, _TN_BW_CONTENT),
    reason="tmp_nzmj fixtures not present (dev-only)",
)

_NEED_ELECEED = pytest.mark.skipif(
    not _have_files(_EL_CH4_P1, _EL_CH63_P1, _EL_CH320_P1),
    reason="tmp_1571 Eleceed fixtures not present (dev-only)",
)


def _open(path: str) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# _compute_chroma_var on real images
# ---------------------------------------------------------------------------

@_NEED_ELECEED
def test_chroma_var_high_for_real_color_webtoon():
    """Real Eleceed color page has substantial chroma_var (>10).

    Empirically the Eleceed pages range chroma_var 8-26 (varies by content
    — heavy action panels with colored effects score higher than calmer
    dialog panels). The 10.0 floor is calibrated against the lowest-chroma
    Eleceed sample we saw (~8.6 on the ch_320 mid-page).
    """
    img = _open(_EL_CH63_P1)
    cv = _compute_chroma_var(img)
    assert cv > 10.0, f"real color webtoon should have chroma_var > 10, got {cv}"


@_NEED_NZMJ
def test_chroma_var_high_for_real_color_manga_cover():
    """Talentless Nana color cover has high chroma_var (>20).

    The cover spreads are saturated full-color art — empirically chroma_var
    lands 20-50 on these. The B&W content pages have chroma_var=0 because
    they're stored in L mode (no chroma channel after RGB convert).
    """
    img = _open(_TN_COLOR_COVER)
    cv = _compute_chroma_var(img)
    assert cv > 20.0, f"color manga cover should have chroma_var > 20, got {cv}"


@_NEED_NZMJ
def test_chroma_var_zero_for_bw_manga_page():
    """Talentless Nana B&W manga page has chroma_var=0 (L-mode → R=G=B
    after RGB convert)."""
    img = _open(_TN_BW_CONTENT)
    cv = _compute_chroma_var(img)
    assert cv < 0.5, f"B&W manga page should have chroma_var ~0, got {cv}"


def test_chroma_var_low_for_uniform_color():
    """A uniform-color (same RGB across the canvas) image has chroma=0
    even if it's RGB-mode (synthetic edge case for the function itself,
    not testing image quality semantics)."""
    img = Image.new("RGB", (400, 600), (128, 128, 128))
    cv = _compute_chroma_var(img)
    assert cv < 0.1


def test_chroma_var_handles_palette_mode():
    """Palette-mode (P) images don't crash chroma_var (PIL converts internally)."""
    img = Image.new("P", (100, 100))
    cv = _compute_chroma_var(img)
    assert cv >= 0.0  # well-defined finite value


# ---------------------------------------------------------------------------
# _compute_page_content_features — shape contract + real-image flags
# ---------------------------------------------------------------------------

@_NEED_ELECEED
def test_page_content_features_shape_real_webtoon():
    """Returned dict has all expected keys with sensible types for real
    Eleceed page (700×1000)."""
    img = _open(_EL_CH4_P1)
    f = _compute_page_content_features(img, b"dummy")
    assert f["width"] == 700
    assert f["height"] == 1000
    assert abs(f["aspect"] - 700 / 1000) < 1e-6
    assert isinstance(f["is_grayscale_page"], bool)
    assert isinstance(f["chroma_var"], float)


@_NEED_NZMJ
def test_page_content_features_bw_manga_flagged_grayscale():
    """Talentless Nana B&W content page (L-mode JPEG) reports
    is_grayscale_page=True."""
    img = _open(_TN_BW_CONTENT)
    f = _compute_page_content_features(img, b"dummy")
    assert f["is_grayscale_page"] is True


@_NEED_ELECEED
def test_page_content_features_color_webtoon_not_flagged_grayscale():
    """Real Eleceed color webtoon page reports is_grayscale_page=False."""
    img = _open(_EL_CH63_P1)
    f = _compute_page_content_features(img, b"dummy")
    assert f["is_grayscale_page"] is False


@_NEED_NZMJ
def test_page_content_features_black_title_page_flagged_grayscale():
    """Talentless Nana ch_1 page 4 is near-pitch-black with manga title
    logo. It's L-mode after JPEG decode (zero chroma) → flagged grayscale.
    Real-data sanity check that the near-uniform black case classifies
    correctly without crashing."""
    img = _open(_TN_BLACK_TITLE)
    f = _compute_page_content_features(img, b"dummy")
    assert f["is_grayscale_page"] is True
    # Tiny chroma_var since the page is monochromatic.
    assert f["chroma_var"] < 1.0


# ---------------------------------------------------------------------------
# _classify_series_content — real-data canonical cases (the v5.1 motivation)
# ---------------------------------------------------------------------------

@_NEED_ELECEED
def test_classify_eleceed_as_chunked_webtoon():
    """The v5.1 canonical case: Eleceed across 3 chapters → color_webtoon_chunked.

    Width consistency holds within chapters (700 in ch_4/63, 690 in ch_320)
    and the across-chapter drift is small enough (CV ≈ 0.007) to pass the
    WEBTOON_WIDTH_CONSISTENCY_CV=0.05 threshold. All pages are color
    (chroma_var > 8 on every sample we checked).
    """
    paths = [
        _EL_CH4_P1, _EL_CH4_MID,
        _EL_CH63_P1, _EL_CH63_MID,
        _EL_CH320_P1, _EL_CH320_MID,
    ]
    features = [_compute_page_content_features(_open(p), b"dummy") for p in paths]
    assert _classify_series_content(features) == "color_webtoon_chunked"


@_NEED_NZMJ
def test_classify_talentless_nana_as_bw_with_color_inserts():
    """Talentless Nana sampled across ch_1 covers + ch_5/10 B&W content
    classifies as bw_manga_with_color_inserts.

    Mix matches the real series shape (color covers + B&W body) which the
    v5.1 classifier specifically distinguishes from pure B&W manga.
    """
    paths = [
        _TN_COLOR_COVER, _TN_COLOR_COVER_2, _TN_COLOR_COVER_3,  # 3 color
        _TN_BLACK_TITLE,                                          # title (B&W)
        _TN_BW_CONTENT, _TN_BW_CONTENT_2,                        # 2 B&W
    ]
    features = [_compute_page_content_features(_open(p), b"dummy") for p in paths]
    # 3 color + 3 grayscale → grayscale_ratio = 0.5 → with_color_inserts.
    result = _classify_series_content(features)
    assert result == "bw_manga_with_color_inserts"


@_NEED_NZMJ
def test_classify_pure_bw_subset_as_bw_manga():
    """Sampling only the B&W pages of Talentless Nana (no covers) classifies
    as pure bw_manga. Verifies the classifier's grayscale-ratio threshold."""
    paths = [_TN_BLACK_TITLE, _TN_BW_CONTENT, _TN_BW_CONTENT_2] * 3  # 9 grayscale
    features = [_compute_page_content_features(_open(p), b"dummy") for p in paths]
    assert _classify_series_content(features) == "bw_manga"


def test_classify_empty_returns_unknown():
    """Empty input → 'unknown' (no crash, no false positives)."""
    assert _classify_series_content([]) == "unknown"


def test_classify_landscape_color_unclassifiable():
    """Wide-aspect color image set (aspect > 0.85) doesn't match manga /
    webtoon patterns → 'unknown'. Synthetic features acceptable here —
    we're testing the classifier logic, not the chroma-detection path."""
    features = [
        {"width": 2000, "height": 1500, "aspect": 2000/1500,
         "is_grayscale_page": False, "chroma_var": 30.0}
        for _ in range(8)
    ]
    assert _classify_series_content(features) == "unknown"


def test_classify_color_webtoon_single_image():
    """Single 1200×8000 page → color_webtoon_single_image. Synthetic
    features because we don't have a real single-image-webtoon fixture
    — the classification rule is independent of pixel content."""
    features = [
        {"width": 1200, "height": 8000, "aspect": 1200/8000,
         "is_grayscale_page": False, "chroma_var": 30.0}
    ]
    assert _classify_series_content(features) == "color_webtoon_single_image"


# ---------------------------------------------------------------------------
# Adaptive res_norm — the core v5.1 fix
# ---------------------------------------------------------------------------

def test_res_norm_targets_complete():
    """Every content_type the classifier returns has a res_norm target."""
    classifier_outputs = {
        "color_webtoon_single_image", "color_webtoon_chunked",
        "bw_manga", "bw_manga_with_color_inserts", "color_manga",
        "unknown",
    }
    for ct in classifier_outputs:
        assert ct in t1_constants.RES_NORM_TARGETS, (
            f"missing res_norm target for content_type: {ct}"
        )


def test_res_norm_webtoon_target_smaller_than_manga():
    """Sanity: webtoons should have a SMALLER reference area than manga
    because chunked-webtoon pages are physically smaller (700×1100 vs
    1500×2000). A 700×1100 page should score high res_norm against
    webtoon's target."""
    assert (
        t1_constants.RES_NORM_TARGETS["color_webtoon_chunked"]
        < t1_constants.RES_NORM_TARGETS["bw_manga"]
    )


def test_t1_weights_jpeg_complete():
    """Every content_type has JPEG weights that sum to 1.0."""
    for ct, w in t1_constants.T1_WEIGHTS_JPEG.items():
        total = w["res"] + w["qf"] + w["block"] + w["fft_hf"] + w["tene"]
        assert abs(total - 1.0) < 1e-6, (
            f"T1_WEIGHTS_JPEG[{ct}] sums to {total}, not 1.0"
        )


def test_t1_weights_webp_complete():
    """Every content_type has WebP weights that sum to 1.0 (no qf slot)."""
    for ct, w in t1_constants.T1_WEIGHTS_WEBP.items():
        total = w["res"] + w["block"] + w["fft_hf"] + w["tene"]
        assert abs(total - 1.0) < 1e-6, (
            f"T1_WEIGHTS_WEBP[{ct}] sums to {total}, not 1.0"
        )


def test_t1_weights_lossless_complete():
    """Every content_type has lossless weights that sum to 1.0."""
    for ct, w in t1_constants.T1_WEIGHTS_LOSSLESS.items():
        total = w["res"] + w["lossless"] + w["block"] + w["fft_hf"] + w["tene"]
        assert abs(total - 1.0) < 1e-6


@_NEED_ELECEED
def test_eleceed_real_page_rescued_by_webtoon_classification():
    """The core v5.1 fix on real data: a 690×1280 Eleceed page (320_0055
    chunk-size) scores much higher when classified as color_webtoon_chunked
    than when treated as unknown/bw_manga.

    Numbers:
      area = 690 * 1280 = 883,200 px²
      "unknown" target = 2,000,000 → res_norm = 0.442
      "color_webtoon_chunked" target = 900,000 → res_norm = 0.981

    That's a 0.54-point res_norm rescue. After T1 weighting (0.30-0.35),
    the composite gains ~0.16-0.19 from correct classification.
    """
    blob = _read(_EL_CH320_MID)  # 690×1280
    result_unknown = _score_image_blob(blob, content_type="unknown")
    result_webtoon = _score_image_blob(blob, content_type="color_webtoon_chunked")
    assert result_unknown is not None
    assert result_webtoon is not None
    score_unknown, meta_unknown = result_unknown
    score_webtoon, meta_webtoon = result_webtoon

    # The webtoon classification dramatically improves res_norm.
    assert meta_webtoon["res_norm"] > meta_unknown["res_norm"] + 0.30, (
        f"webtoon classification should rescue real Eleceed page res_norm; "
        f"unknown={meta_unknown['res_norm']:.3f}, "
        f"webtoon={meta_webtoon['res_norm']:.3f}"
    )
    # Composite score gains too (after webtoon weight reshuffling).
    assert score_webtoon > score_unknown + 0.05, (
        f"Eleceed page score: unknown={score_unknown:.3f} vs "
        f"webtoon={score_webtoon:.3f} — gap should exceed 0.05"
    )


@_NEED_ELECEED
def test_eleceed_real_page_t1_weights_change_with_content_type():
    """Same real Eleceed page scored under different content_types
    produces different composites because the per-content-type weight
    tables differ.

    color_webtoon_chunked drops tene 0.15→0.05 and bumps res 0.30→0.35 +
    fft_hf 0.15→0.20. For Eleceed (which has anti-aliased gradients with
    LOW Tenengrad signal — these pages have softer edges than B&W manga),
    the lower tene weight in webtoon context REDUCES under-penalization
    of legitimate anti-aliased content."""
    blob = _read(_EL_CH63_MID)
    result_bw = _score_image_blob(blob, content_type="bw_manga")
    result_webtoon = _score_image_blob(blob, content_type="color_webtoon_chunked")
    assert result_bw is not None
    assert result_webtoon is not None
    # Different content_types → different composites (the test passes if
    # they differ by ANY measurable amount).
    assert result_bw[0] != result_webtoon[0]
    assert result_bw[1]["content_type"] == "bw_manga"
    assert result_webtoon[1]["content_type"] == "color_webtoon_chunked"
    # Both content_types tag res_norm_target correctly in metadata.
    assert result_bw[1]["res_norm_target"] == t1_constants.RES_NORM_TARGETS["bw_manga"]
    assert result_webtoon[1]["res_norm_target"] == t1_constants.RES_NORM_TARGETS["color_webtoon_chunked"]


@_NEED_NZMJ
def test_talentless_nana_bw_content_classified_correctly_by_t1():
    """A Talentless Nana B&W content page (1114×1584 JPEG) when probed
    with content_type=bw_manga should score in the same band as the v5
    test established (0.75-0.95).

    This guards against accidental regression: when v5.1's classifier
    chain decides "bw_manga" for a series, the per-content-type T1 weights
    should give the SAME numeric band as the v5 hardcoded weights (because
    the bw_manga entry in T1_WEIGHTS_JPEG matches v5's defaults exactly)."""
    blob = _read(_TN_BW_CONTENT)
    result = _score_image_blob(blob, content_type="bw_manga")
    assert result is not None
    score, meta = result
    assert 0.75 <= score <= 0.95, (
        f"Talentless Nana B&W page in bw_manga context scored {score}, "
        f"expected 0.75-0.95 (matches v5 band)"
    )
    assert meta["content_type"] == "bw_manga"
    assert meta["res_norm_target"] == 2_000_000
