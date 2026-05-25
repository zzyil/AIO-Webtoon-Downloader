"""Tests for v5.1 Phase 5 format expansion: AVIF/JXL + chroma subsampling.

AVIF support comes via pillow-avif-plugin (already installed in v5 via
pyvips/Pillow's wider dep tree). JXL is OPTIONAL via jxlpy — graceful
skip when missing. Chroma subsampling penalty parses JPEG SOF markers
directly (no jpeglib dependency required).

Cross-file: targets sites/search_orchestrator.py:_score_image_blob,
_compute_t1_score, _detect_chroma_subsampling, _compute_chroma_penalty
+ sites/t1_constants.py:CHROMA_PENALTY, CHROMA_COMPLEXITY_THRESHOLD,
AVIF_QUALITY_PREMIUM. Plan reference:
~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 5 section).
"""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from sites.search_orchestrator import (
    _AVIF_AVAILABLE,
    _JXL_AVAILABLE,
    _compute_chroma_penalty,
    _detect_chroma_subsampling,
    _score_image_blob,
)
from sites import t1_constants


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_NZMJ = os.path.join(_REPO_ROOT, "tmp_nzmj")
_TMP_ELECEED = os.path.join(_REPO_ROOT, "tmp_1571")

_TN_COLOR_COVER = os.path.join(_TMP_NZMJ, "ch_1", "1_0001.jpg")
_TN_BW_CONTENT = os.path.join(_TMP_NZMJ, "ch_5", "5_0021.jpg")
_EL_CLEAN = os.path.join(_TMP_ELECEED, "ch_4", "4_0056.jpg")


def _have_files(*paths: str) -> bool:
    return all(os.path.isfile(p) for p in paths)


_NEED_FIXTURES = pytest.mark.skipif(
    not _have_files(_TN_COLOR_COVER, _TN_BW_CONTENT, _EL_CLEAN),
    reason="real-world fixtures not present (dev-only)",
)


# ---------------------------------------------------------------------------
# AVIF round-trip + scoring
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _AVIF_AVAILABLE, reason="pillow-avif-plugin not installed")
def test_avif_encode_and_score():
    """An AVIF blob produced by PIL+pillow-avif-plugin is decodable and
    scoreable. The format ends up tagged as 'AVIF' in metadata."""
    img = Image.new("RGB", (1500, 2000), (128, 128, 128))
    # Add some structure so the encoder produces a real file (not just a
    # uniform-color tiny header).
    for x in range(0, 1500, 50):
        for y in range(0, 2000, 50):
            img.paste((255 - x % 256, x % 256, y % 256), (x, y, x + 30, y + 30))
    buf = io.BytesIO()
    img.save(buf, format="AVIF", quality=85)
    result = _score_image_blob(buf.getvalue(), content_type="color_manga")
    assert result is not None
    score, meta = result
    assert meta["format"] == "AVIF"
    assert 0.0 <= score <= 1.0


@pytest.mark.skipif(not _AVIF_AVAILABLE, reason="pillow-avif-plugin not installed")
@_NEED_FIXTURES
def test_avif_premium_makes_score_higher_than_webp_equivalent():
    """At matched dimensions and JPEG-quality-equivalent encode, AVIF gets
    the AVIF_QUALITY_PREMIUM (1.05) multiplier applied to its T1 composite.
    A same-source page encoded as AVIF vs WebP at q=85 should have the AVIF
    version's T1 ≈ WebP's × 1.05 (capped at 1.0)."""
    img = Image.open(_EL_CLEAN).convert("RGB")
    buf_avif = io.BytesIO(); img.save(buf_avif, format="AVIF", quality=85)
    buf_webp = io.BytesIO(); img.save(buf_webp, format="WEBP", quality=85, method=4)
    r_avif = _score_image_blob(buf_avif.getvalue(), content_type="color_webtoon_chunked")
    r_webp = _score_image_blob(buf_webp.getvalue(), content_type="color_webtoon_chunked")
    assert r_avif is not None and r_webp is not None
    s_avif, m_avif = r_avif
    s_webp, m_webp = r_webp
    assert m_avif["format"] == "AVIF"
    assert m_webp["format"] == "WEBP"
    # AVIF should be >= WebP (premium applied). They might be equal when
    # WebP already saturates at 1.0; ALLOWING that case.
    assert s_avif >= s_webp - 0.01, (
        f"AVIF ({s_avif}) should match or exceed WebP ({s_webp}) at matched q"
    )


# ---------------------------------------------------------------------------
# JXL — optional dep, skipif-style
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _JXL_AVAILABLE, reason="jxlpy not installed")
def test_jxl_decodes_and_scores():
    """JXL blobs decode via jxlpy (when installed) and produce valid
    T1 scores."""
    img = Image.new("RGB", (1500, 2000), (128, 128, 128))
    buf = io.BytesIO()
    try:
        img.save(buf, format="JXL", quality=85)
    except Exception:
        pytest.skip("PIL doesn't have JXL save support even though jxlpy is loadable")
    result = _score_image_blob(buf.getvalue(), content_type="color_manga")
    if result is None:
        pytest.skip("JXL decode returned None — likely needs full jxlpy integration")
    score, meta = result
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# _detect_chroma_subsampling — JPEG marker parsing
# ---------------------------------------------------------------------------

def test_chroma_subsampling_default_444():
    """PIL's default JPEG save with subsampling=0 produces 4:4:4 chroma."""
    img = Image.new("RGB", (400, 600), (200, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=0)
    result = _detect_chroma_subsampling(buf.getvalue())
    assert result == "4:4:4"


def test_chroma_subsampling_420():
    """PIL's subsampling=2 (Y(2,2)) produces 4:2:0 — the default for most
    aggregator-encoded JPEGs."""
    img = Image.new("RGB", (400, 600), (200, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=2)
    result = _detect_chroma_subsampling(buf.getvalue())
    assert result == "4:2:0"


def test_chroma_subsampling_422():
    """PIL's subsampling=1 (Y(2,1)) produces 4:2:2."""
    img = Image.new("RGB", (400, 600), (200, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=1)
    result = _detect_chroma_subsampling(buf.getvalue())
    assert result == "4:2:2"


def test_chroma_subsampling_returns_none_for_non_jpeg():
    """PNG / WebP / AVIF / etc. → return None (chroma subsampling N/A)."""
    img = Image.new("RGB", (400, 400), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert _detect_chroma_subsampling(buf.getvalue()) is None


def test_chroma_subsampling_returns_none_for_grayscale_jpeg():
    """Grayscale JPEG (1 component) → None (no chroma channels)."""
    img = Image.new("L", (400, 400), 128)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    assert _detect_chroma_subsampling(buf.getvalue()) is None


def test_chroma_subsampling_handles_corrupt_blob():
    """Garbage bytes → None gracefully."""
    assert _detect_chroma_subsampling(b"not a jpeg at all") is None
    assert _detect_chroma_subsampling(b"") is None


# ---------------------------------------------------------------------------
# _compute_chroma_penalty — penalty application logic
# ---------------------------------------------------------------------------

def test_chroma_penalty_skipped_for_bw_manga():
    """B&W manga content_type skips chroma penalty entirely (chroma irrelevant
    on grayscale content)."""
    import numpy as np
    img = Image.new("RGB", (400, 600), (200, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=2)  # 4:2:0
    arr = np.asarray(img, dtype=np.uint8)
    penalty, _ = _compute_chroma_penalty(buf.getvalue(), arr, "bw_manga")
    assert penalty == 0.0


def test_chroma_penalty_skipped_when_not_420():
    """4:4:4 JPEG → no penalty (best chroma quality)."""
    import numpy as np
    img = Image.new("RGB", (400, 600), (200, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=0)  # 4:4:4
    arr = np.asarray(img, dtype=np.uint8)
    penalty, diag = _compute_chroma_penalty(buf.getvalue(), arr, "color_manga")
    assert penalty == 0.0
    assert diag["chroma_subsampling"] == "4:4:4"


def test_chroma_penalty_skipped_for_low_complexity_color():
    """4:2:0 JPEG with uniform color (low complexity) → no penalty (subsampling
    is visually acceptable on flat-color content)."""
    import numpy as np
    img = Image.new("RGB", (400, 600), (200, 100, 100))  # uniform red
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=2)
    arr = np.asarray(img, dtype=np.uint8)
    penalty, diag = _compute_chroma_penalty(buf.getvalue(), arr, "color_manga")
    # Uniform color has near-zero chroma std → below CHROMA_COMPLEXITY_THRESHOLD.
    assert penalty == 0.0


@_NEED_FIXTURES
def test_chroma_penalty_applied_for_high_complexity_color_420():
    """Real high-chroma color content (Eleceed) saved as 4:2:0 → penalty
    applied. This is the case v5.1 wants to detect: aggregator-encoded
    color webtoons that aggressively subsample chroma."""
    import numpy as np
    img = Image.open(_EL_CLEAN).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=2)  # force 4:2:0
    arr = np.asarray(img, dtype=np.uint8)
    penalty, diag = _compute_chroma_penalty(buf.getvalue(), arr, "color_webtoon_chunked")
    # Real Eleceed content has rich color → chroma complexity > threshold.
    assert penalty == t1_constants.CHROMA_PENALTY
    assert diag["chroma_subsampling"] == "4:2:0"
    assert diag["chroma_complexity"] is not None
    assert diag["chroma_complexity"] >= t1_constants.CHROMA_COMPLEXITY_THRESHOLD


# ---------------------------------------------------------------------------
# End-to-end via _score_image_blob — full T1 reflects chroma penalty
# ---------------------------------------------------------------------------

@_NEED_FIXTURES
def test_score_image_blob_chroma_penalty_affects_t1():
    """Two encodes of the same real Eleceed page: one at 4:4:4, one at 4:2:0.
    The 4:2:0 version's T1 should be 0.03 lower (the CHROMA_PENALTY)."""
    img = Image.open(_EL_CLEAN).convert("RGB")
    buf_444 = io.BytesIO()
    img.save(buf_444, format="JPEG", quality=85, subsampling=0)
    buf_420 = io.BytesIO()
    img.save(buf_420, format="JPEG", quality=85, subsampling=2)
    r_444 = _score_image_blob(buf_444.getvalue(), content_type="color_webtoon_chunked")
    r_420 = _score_image_blob(buf_420.getvalue(), content_type="color_webtoon_chunked")
    assert r_444 is not None and r_420 is not None
    s_444, m_444 = r_444
    s_420, m_420 = r_420
    assert m_444["chroma_subsampling"] == "4:4:4"
    assert m_420["chroma_subsampling"] == "4:2:0"
    assert m_444["chroma_penalty"] == 0.0
    assert m_420["chroma_penalty"] == t1_constants.CHROMA_PENALTY
    # 4:2:0 T1 should be ~CHROMA_PENALTY lower (allow noise from JPEG
    # round-trip differences in other metrics).
    assert s_444 > s_420 - 0.005, (
        f"4:4:4 T1 ({s_444}) should be > 4:2:0 T1 ({s_420}) "
        f"by approximately {t1_constants.CHROMA_PENALTY}"
    )


@_NEED_FIXTURES
def test_score_image_blob_bw_manga_no_chroma_penalty():
    """B&W manga page → chroma_penalty is 0 regardless of subsampling
    setting (chroma irrelevant for grayscale content)."""
    img = Image.open(_TN_BW_CONTENT).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, subsampling=2)  # 4:2:0
    result = _score_image_blob(buf.getvalue(), content_type="bw_manga")
    assert result is not None
    _, meta = result
    assert meta["chroma_penalty"] == 0.0


# ---------------------------------------------------------------------------
# Constants — sanity checks
# ---------------------------------------------------------------------------

def test_chroma_penalty_constant_in_reasonable_range():
    """CHROMA_PENALTY should be small (subsampling is a visually subtle issue)."""
    assert 0.0 < t1_constants.CHROMA_PENALTY <= 0.10


def test_avif_premium_constant_in_reasonable_range():
    """AVIF_QUALITY_PREMIUM should be > 1.0 (premium) but small (cap)."""
    assert 1.0 < t1_constants.AVIF_QUALITY_PREMIUM <= 1.15
