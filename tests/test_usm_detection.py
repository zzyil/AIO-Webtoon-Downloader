"""Tests for v5.1 Phase 2 USM (Unsharp Mask) fake-sharpness detection.

The algorithm: compute Laplacian variance / image variance ratio. Clean
images have raw_ratio ~ 10-12 (lap_var is small relative to img_var);
USM-amplified images push it to 16-35. Maps (raw_ratio - 12) / 20 → 0..1+.

The score is used to DAMP Tenengrad in the T1 composite via:
  tenengrad_clean = tenengrad_norm * (1 - clamp(0, 1, usm / USM_NORMALIZATION[content_type]))
so fake-sharpened pages don't get artificial Tenengrad credit from their
overshoot rings.

Per-content-type USM_NORMALIZATION (in t1_constants.py):
  bw_manga / color_manga / unknown:  0.30 / 0.40 / 0.40 — sensitive
  webtoon_chunked / single_image:    0.50          — lenient (anti-aliased)

Real-world fixtures + PIL UnsharpMask provide the positive cases.

Cross-file: targets sites/search_orchestrator.py:_compute_usm_overshoot,
_compute_t1_score, t1_constants.USM_NORMALIZATION. Plan reference:
~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 2 section).
"""

from __future__ import annotations

import io
import os

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="USM detection requires cv2")
from PIL import Image, ImageFilter

from sites.search_orchestrator import (
    _compute_usm_overshoot,
    _score_image_blob,
)
from sites import t1_constants


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_NZMJ = os.path.join(_REPO_ROOT, "tmp_nzmj")
_TMP_ELECEED = os.path.join(_REPO_ROOT, "tmp_1571")

_TN_BW_CONTENT = os.path.join(_TMP_NZMJ, "ch_5", "5_0021.jpg")
_TN_BLACK_TITLE = os.path.join(_TMP_NZMJ, "ch_1", "1_0004.jpg")
_EL_CLEAN = os.path.join(_TMP_ELECEED, "ch_4", "4_0056.jpg")


def _have_files(*paths: str) -> bool:
    return all(os.path.isfile(p) for p in paths)


_NEED_NZMJ = pytest.mark.skipif(
    not _have_files(_TN_BW_CONTENT, _TN_BLACK_TITLE),
    reason="tmp_nzmj fixtures not present (dev-only)",
)
_NEED_ELECEED = pytest.mark.skipif(
    not _have_files(_EL_CLEAN),
    reason="tmp_1571 Eleceed fixtures not present (dev-only)",
)


def _open_rgb(path: str) -> Image.Image:
    img = Image.open(path).convert("RGB")
    img.load()
    return img


# ---------------------------------------------------------------------------
# Real-world: clean fixtures should score 0
# ---------------------------------------------------------------------------

@_NEED_NZMJ
def test_usm_clean_bw_manga_low_score():
    """Clean Talentless Nana B&W content page → usm_score == 0 (no
    sharpening artifacts detected). The variance ratio sits at ~11-12,
    just below the (raw_ratio - 12) / 20 offset, so score clamps to 0."""
    img = _open_rgb(_TN_BW_CONTENT)
    score, diag = _compute_usm_overshoot(np.asarray(img), content_type="bw_manga")
    assert score < 0.1, (
        f"clean B&W manga should have usm_score near 0, got {score} "
        f"(ratio {diag.get('lap_var_ratio')})"
    )


@_NEED_ELECEED
def test_usm_clean_webtoon_low_score():
    """Clean Eleceed page → usm_score == 0. Anti-aliased edges have
    lower lap_var than B&W manga, so this is the "easy" clean case."""
    img = _open_rgb(_EL_CLEAN)
    score, _ = _compute_usm_overshoot(np.asarray(img), content_type="color_webtoon_chunked")
    assert score < 0.05


# ---------------------------------------------------------------------------
# Real-world + synthetic USM application: detection
# ---------------------------------------------------------------------------

@_NEED_NZMJ
def test_usm_detects_pil_unsharpmask_on_bw_manga():
    """PIL UnsharpMask r=2 %=200 applied to a clean B&W page produces a
    high usm_score that triggers the fake_sharpened outlier."""
    img = _open_rgb(_TN_BW_CONTENT)
    img_usm = img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=0))
    score, _ = _compute_usm_overshoot(np.asarray(img_usm), content_type="bw_manga")
    # bw_manga USM_NORMALIZATION is 0.30. Heavy USM should push score
    # well above that → full Tenengrad damping. Empirical: ~0.88.
    assert score > 0.30, (
        f"PIL UnsharpMask r=2 %=200 should produce usm_score > 0.30, got {score}"
    )


@_NEED_NZMJ
def test_usm_monotonic_with_increasing_strength():
    """Heavier USM produces a higher usm_score. Verifies the score is
    a monotonic indicator of sharpening intensity."""
    img = _open_rgb(_TN_BW_CONTENT)
    scores = []
    for radius, percent in [(0, 0), (1, 100), (2, 200), (3, 400)]:
        if radius == 0:
            arr = np.asarray(img)
        else:
            arr = np.asarray(img.filter(
                ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=0)
            ))
        score, _ = _compute_usm_overshoot(arr, content_type="bw_manga")
        scores.append(score)
    # Allow small wobble at heaviest USM (signal saturates) — what we
    # require is the score INCREASES monotonically through medium USM.
    for prev, curr in zip(scores[:3], scores[1:4]):
        assert curr >= prev - 0.05, (
            f"USM score should monotonically increase; "
            f"got {scores} ({prev} → {curr} regressed)"
        )
    # End-to-end: heavy USM >> clean.
    assert scores[-1] - scores[0] > 0.5


@_NEED_ELECEED
def test_usm_detects_unsharpmask_on_webtoon():
    """Webtoon detection — USM_NORMALIZATION is 0.50 (more lenient) so
    the raw score doesn't need to be as high to trigger damping. Same
    UnsharpMask applied to an Eleceed page should still produce a
    meaningful (non-zero) score."""
    img = _open_rgb(_EL_CLEAN)
    img_usm = img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=0))
    score, _ = _compute_usm_overshoot(np.asarray(img_usm), content_type="color_webtoon_chunked")
    # Empirical on Eleceed: ~0.21 raw score; > 0.10 is the clear-USM band.
    assert score > 0.10


# ---------------------------------------------------------------------------
# Edge cases — degenerate inputs
# ---------------------------------------------------------------------------

def test_usm_uniform_image_returns_zero():
    """A uniform-color image has zero variance — degenerate input, returns
    0 gracefully (no division by zero)."""
    img = np.full((600, 800, 3), 128, dtype=np.uint8)
    score, diag = _compute_usm_overshoot(img, content_type="bw_manga")
    assert score == 0.0
    assert diag.get("lap_var_ratio") == 0.0


def test_usm_no_edges_returns_zero():
    """An image with too few edges (n < 100) bails early and returns 0."""
    # Gradient image — has gradients but Canny won't fire much.
    img = np.zeros((600, 800, 3), dtype=np.uint8)
    for x in range(800):
        img[:, x] = (x // 4, x // 4, x // 4)
    score, diag = _compute_usm_overshoot(img, content_type="bw_manga")
    # Either no edges (clean 0) or very low ratio. Either way score == 0.
    assert score == 0.0


@_NEED_NZMJ
def test_usm_near_black_image_returns_zero():
    """The Talentless Nana title page (ch_1/page_4) is near-pitch-black
    with a small white logo. Almost no variance → degenerate case, 0."""
    img = _open_rgb(_TN_BLACK_TITLE)
    score, _ = _compute_usm_overshoot(np.asarray(img), content_type="bw_manga")
    # Either truly 0 (uniform) or a tiny number reflecting just the logo edges.
    assert score < 0.10


def test_usm_no_op_when_cv2_unavailable(monkeypatch):
    """When _CV2_AVAILABLE is False, _compute_usm_overshoot returns 0.
    Graceful degrade — T1 composite would just skip damping."""
    from sites import search_orchestrator
    monkeypatch.setattr(search_orchestrator, "_CV2_AVAILABLE", False)
    arr = np.full((400, 400, 3), 128, dtype=np.uint8)
    score, diag = _compute_usm_overshoot(arr, content_type="bw_manga")
    assert score == 0.0
    assert diag.get("usm_unavailable") is True


# ---------------------------------------------------------------------------
# T1 integration — Tenengrad damping via tenengrad_clean
# ---------------------------------------------------------------------------

@_NEED_NZMJ
def test_t1_composite_drops_when_usm_applied():
    """Full _score_image_blob: a USM-applied page scores LOWER than the
    same page clean because tenengrad_clean drops to 0 (full damping)."""
    img_clean = _open_rgb(_TN_BW_CONTENT)
    img_usm = img_clean.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=0))
    buf_c = io.BytesIO(); img_clean.convert("L").save(buf_c, format="JPEG", quality=85)
    buf_u = io.BytesIO(); img_usm.convert("L").save(buf_u, format="JPEG", quality=85)
    r_c = _score_image_blob(buf_c.getvalue(), content_type="bw_manga")
    r_u = _score_image_blob(buf_u.getvalue(), content_type="bw_manga")
    assert r_c is not None and r_u is not None
    s_c, m_c = r_c
    s_u, m_u = r_u
    # Clean: tene_clean preserves tene_norm.
    assert m_c["tenengrad_clean"] >= m_c["tenengrad_norm"] - 0.05
    # USM: tene_clean is damped well below tene_norm (full kill at usm >= USM_NORM).
    assert m_u["tenengrad_clean"] < m_u["tenengrad_norm"] - 0.5
    # Overall composite drops (typically ~0.10-0.15 for the canonical case).
    assert s_c - s_u > 0.05, (
        f"USM should drop composite; clean={s_c:.4f} usm={s_u:.4f}"
    )
    # The fake_sharpened outlier flag triggers when usm > 0.8 * USM_NORM.
    assert m_u.get("outlier") == "fake_sharpened"


@_NEED_NZMJ
def test_t1_clean_image_has_no_usm_penalty():
    """Clean B&W page has usm=0 → tene_clean == tene_norm exactly,
    no outlier flag set."""
    img = _open_rgb(_TN_BW_CONTENT)
    buf = io.BytesIO()
    img.convert("L").save(buf, format="JPEG", quality=85)
    r = _score_image_blob(buf.getvalue(), content_type="bw_manga")
    assert r is not None
    _, m = r
    assert m["usm_overshoot_score"] == 0.0
    assert m["tenengrad_clean"] == m["tenengrad_norm"]
    # No fake_sharpened outlier on clean content (other outliers like
    # webp_below_floor or heavy_watermark could be set; check specifically).
    assert m.get("outlier") != "fake_sharpened"


@_NEED_ELECEED
def test_t1_webtoon_lenient_threshold():
    """Webtoon content has USM_NORMALIZATION=0.50 (lenient because of
    natural anti-aliasing). The same UnsharpMask r=2 %=200 produces
    only PARTIAL damping (not full kill) because raw score ~0.21 < 0.50."""
    img = _open_rgb(_EL_CLEAN)
    img_usm = img.filter(ImageFilter.UnsharpMask(radius=2, percent=200, threshold=0))
    buf = io.BytesIO()
    img_usm.save(buf, format="JPEG", quality=85)
    r = _score_image_blob(buf.getvalue(), content_type="color_webtoon_chunked")
    assert r is not None
    _, m = r
    # Partial damping: tene_clean somewhere between 0 and tene_norm.
    assert 0 < m["tenengrad_clean"] < m["tenengrad_norm"]


# ---------------------------------------------------------------------------
# Per-content-type normalization constants — sanity
# ---------------------------------------------------------------------------

def test_usm_normalization_all_content_types_present():
    """Every classifier output has a USM_NORMALIZATION band."""
    for ct in ("bw_manga", "bw_manga_with_color_inserts", "color_manga",
                "color_webtoon_chunked", "color_webtoon_single_image",
                "unknown"):
        assert ct in t1_constants.USM_NORMALIZATION, f"missing band for {ct}"


def test_usm_normalization_webtoon_more_lenient_than_manga():
    """Webtoon bands should be >= manga bands (anti-aliased content tolerates
    more raw overshoot before triggering damping)."""
    assert (
        t1_constants.USM_NORMALIZATION["color_webtoon_chunked"]
        >= t1_constants.USM_NORMALIZATION["bw_manga"]
    )
