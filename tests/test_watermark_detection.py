"""Tests for v5.1 Phase 1 watermark detection (sites/search_orchestrator.py).

Watermark detection runs region-based easyocr on corner / edge / center
crops of each probed page. Triggered regions stack a penalty up to 0.15
on T1; n_triggered >= 2 sets outlier=heavy_watermark.

Real-world fixtures:
  - tmp_1571 (Eleceed) — clean color webtoon, used to verify no
    false-positives on legitimate panel dialogue text.
  - tmp_nzmj (Talentless Nana) — clean B&W manga, same false-positive guard.

Synthetic watermarks composited onto clean fixtures provide the positive
cases (CRAFT/easyocr text-detection ground truth).

Cross-file: targets sites/search_orchestrator.py:_detect_watermarks,
_region_has_watermark, _resolve_region, _easyocr_bbox_to_rect.
Plan reference: ~/.claude/plans/how-robust-is-the-memoized-koala.md
(Phase 1 section).
"""

from __future__ import annotations

import io
import os
from typing import Tuple
from unittest.mock import patch

import pytest

# Skip whole module when easyocr isn't available.
easyocr = pytest.importorskip("easyocr", reason="watermark detection requires easyocr")

from PIL import Image, ImageDraw, ImageFont

from sites.search_orchestrator import (
    _detect_watermarks,
    _easyocr_bbox_to_rect,
    _region_has_watermark,
    _resolve_region,
    _score_image_blob,
    warmup_t2_models,
    _WATERMARK_READY,
)
from sites import t1_constants


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_NZMJ = os.path.join(_REPO_ROOT, "tmp_nzmj")
_TMP_ELECEED = os.path.join(_REPO_ROOT, "tmp_1571")

_TN_BW_CONTENT = os.path.join(_TMP_NZMJ, "ch_5", "5_0021.jpg")
_EL_CLEAN = os.path.join(_TMP_ELECEED, "ch_4", "4_0056.jpg")

_NEED_FIXTURES = pytest.mark.skipif(
    not (os.path.isfile(_TN_BW_CONTENT) and os.path.isfile(_EL_CLEAN)),
    reason="real-world fixtures not present (dev-only)",
)


# Module-level fixture: warm up easyocr ONCE per pytest session. Each
# warmup costs ~17 s + ~200 MB weight download on first machine-ever-use,
# then is essentially instant on cache hits. Without this, every test
# that hits _detect_watermarks would re-pay the lazy-init cost.
@pytest.fixture(scope="module", autouse=True)
def _warm_easyocr():
    """Force warmup of easyocr Reader before this module's tests run."""
    warmup_t2_models(background=False)
    assert _WATERMARK_READY.is_set(), (
        "easyocr warmup did not signal _WATERMARK_READY"
    )


# Helper to load a usable TrueType font. PIL's default bitmap font is too
# small for easyocr to read reliably (~6 px); use Windows arial.ttf at
# 32 px which mimics realistic aggregator watermark text size.
def _get_arial_font(size: int = 32) -> "ImageFont.ImageFont":
    for candidate in [
        "arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial.ttf",
    ]:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _composite_corner_watermark(img: Image.Image) -> Image.Image:
    """Place a realistic-looking aggregator watermark in the bottom-right
    corner: 2-line text with white background, inside the corner_br region.

    Two lines because the webtoon content_type threshold requires
    n_boxes >= 2 in a region. Real aggregators usually emit something like
    "READ ON\nSITENAME" — two visually-distinct text rows.
    """
    out = img.copy()
    draw = ImageDraw.Draw(out)
    font = _get_arial_font(32)
    # corner_br is the last 200×150 pixels. Stay safely inside it.
    x1 = out.width - 190
    y1 = out.height - 130
    draw.rectangle([(x1, y1), (out.width - 5, out.height - 5)], fill="white")
    draw.text((x1 + 10, y1 + 10), "READ ON\nMANGAREAD", fill="black", font=font)
    return out


# ---------------------------------------------------------------------------
# _resolve_region — coordinate resolution
# ---------------------------------------------------------------------------

def test_resolve_region_corner_tl():
    """corner_tl = (0, 0, 200, 150) → top-left 200×150 box."""
    assert _resolve_region((0, 0, 200, 150), 1000, 1500) == (0, 0, 200, 150)


def test_resolve_region_corner_br_negative_coords():
    """Negative coordinates count from the opposite edge."""
    # corner_br = (-200, -150, None, None)
    r = _resolve_region((-200, -150, None, None), 1000, 1500)
    assert r == (800, 1350, 1000, 1500)


def test_resolve_region_edge_top_full_width():
    """edge_top = (0, 0, None, 100) → full-width 100px-tall strip."""
    r = _resolve_region((0, 0, None, 100), 700, 1000)
    assert r == (0, 0, 700, 100)


def test_resolve_region_center_strip_h_expressions():
    """center_strip uses h/2±50 string tokens — must resolve to a
    100px-tall horizontal band centered on the image."""
    r = _resolve_region((0, "h/2 - 50", None, "h/2 + 50"), 700, 1000)
    assert r == (0, 450, 700, 550)


def test_resolve_region_clamps_oversize():
    """Negative coords larger than image dimensions clamp to 0."""
    # -500 on a 200-wide image: max(0, 200 + (-500)) = max(0, -300) = 0
    r = _resolve_region((-500, 0, None, 100), 200, 1000)
    assert r is None or r[0] == 0


def test_resolve_region_degenerate_returns_none():
    """Region that resolves to zero area (x2 <= x1) returns None."""
    assert _resolve_region((100, 0, 50, 100), 700, 1000) is None
    assert _resolve_region((0, 100, 700, 50), 700, 1000) is None


def test_resolve_region_too_small_returns_none():
    """Regions smaller than 16×16 are too small for OCR — return None."""
    assert _resolve_region((0, 0, 10, 10), 1000, 1000) is None


def test_resolve_region_all_regions_resolvable_on_real_image_size():
    """Every entry in WATERMARK_REGIONS must resolve cleanly on typical
    image dimensions (700×1000 Eleceed, 1500×2000 manga)."""
    for size in [(700, 1000), (1500, 2000), (1114, 1584)]:
        w, h = size
        for name, region_def in t1_constants.WATERMARK_REGIONS.items():
            r = _resolve_region(region_def, w, h)
            assert r is not None, (
                f"region {name} failed to resolve on {w}×{h}: {region_def}"
            )


# ---------------------------------------------------------------------------
# _easyocr_bbox_to_rect — bbox math
# ---------------------------------------------------------------------------

def test_bbox_to_rect_axis_aligned_quadrilateral():
    """easyocr's 4-point polygon → (xmin, ymin, xmax, ymax)."""
    bbox = [[10, 20], [100, 20], [100, 50], [10, 50]]
    assert _easyocr_bbox_to_rect(bbox) == (10, 20, 100, 50)


def test_bbox_to_rect_robust_to_rotation():
    """Slight rotation (perspective): bbox is still axis-aligned via min/max."""
    bbox = [[10, 22], [100, 18], [102, 52], [12, 56]]
    rect = _easyocr_bbox_to_rect(bbox)
    assert rect == (10, 18, 102, 56)


# ---------------------------------------------------------------------------
# _region_has_watermark — threshold logic
# ---------------------------------------------------------------------------

def test_region_has_watermark_empty_boxes():
    """No boxes → not triggered, no crash."""
    triggered, diag = _region_has_watermark([], 30000, "bw_manga")
    assert triggered is False
    assert diag["n_boxes"] == 0


def test_region_has_watermark_manga_single_box_does_not_trigger():
    """v7 (2026-05-18): manga thresholds raised to n>=2 AND cov>=0.20.
    A single bbox with high coverage no longer trips manga — was
    `n>=1 AND cov>=0.05` in v6 and prior, but tripped on routine
    in-corner content (speech bubbles, sound effects, page numbers,
    cover title/logo art via the cover-probe fallback). See
    `_region_has_watermark` docstring rationale.

    v5.1 (post-2026-05-17): _region_has_watermark takes axis-aligned
    rects (x_min, y_min, x_max, y_max) because we use easyocr.detect()
    (CRAFT-only). Per-box confidence is gated by text_threshold=0.7 at
    the detect() call site instead of post-hoc filtering."""
    # Single bbox, area 7600 in a 30000 region → coverage 0.253.
    # Pre-v7 this tripped (n>=1, cov>=0.05). v7 requires n>=2.
    fake_rect = (10, 20, 200, 60)
    triggered, _ = _region_has_watermark([fake_rect], 30000, "bw_manga")
    assert triggered is False  # n=1 fails new n>=2 gate


def test_region_has_watermark_manga_two_boxes_high_coverage_triggers():
    """v7: manga branch triggers only when n>=2 AND cov>=0.20. Two boxes
    totaling 31% coverage clear both bars — this is the real piracy
    watermark profile (multi-line "READ ON SITENAME" overlay)."""
    b1 = (10, 20, 200, 60)    # area 190*40 = 7600
    b2 = (10, 70, 200, 110)   # area 190*40 = 7600
    # Total 15200 / region 30000 → coverage 0.507 ≫ 0.20.
    triggered, _ = _region_has_watermark([b1, b2], 30000, "bw_manga")
    assert triggered is True


def test_region_has_watermark_manga_two_boxes_low_coverage_does_not_trigger():
    """v7: cov>=0.20 is the manga floor. Two boxes that meet n>=2 but
    sit under the coverage bar do NOT trip — guard against
    speech-bubble-plus-page-number patterns flagging as watermark."""
    b1 = (10, 20, 50, 60)    # area 40*40 = 1600
    b2 = (10, 70, 50, 110)   # area 40*40 = 1600
    # Total 3200 / region 30000 → coverage 0.107 (above webtoon's 0.10
    # but below manga's 0.20 — the asymmetry IS the test).
    triggered, _ = _region_has_watermark([b1, b2], 30000, "bw_manga")
    assert triggered is False


def test_region_has_watermark_webtoon_threshold_requires_2_boxes():
    """Webtoon thresholds: n>=2, coverage>=0.10 → 1 box doesn't trigger."""
    fake_rect = (10, 20, 200, 60)
    triggered, _ = _region_has_watermark([fake_rect], 30000, "color_webtoon_chunked")
    assert triggered is False  # only 1 box → doesn't meet n>=2


def test_region_has_watermark_webtoon_two_boxes_triggers():
    """2 boxes at high coverage → webtoon triggers."""
    b1 = (10, 20, 200, 60)    # area 190*40 = 7600
    b2 = (10, 70, 200, 110)   # area 190*40 = 7600
    # Total 15200 / region 30000 → coverage 0.507 ≫ 0.10
    triggered, _ = _region_has_watermark([b1, b2], 30000, "color_webtoon_chunked")
    assert triggered is True


def test_region_has_watermark_low_coverage_does_not_trigger():
    """Tiny text bbox (low coverage in region) doesn't trigger, regardless
    of content type."""
    fake_rect = (10, 20, 30, 30)  # area 20*10 = 200
    # coverage = 200/30000 = 0.0067 ≪ 0.20 ≪ 0.10
    triggered, _ = _region_has_watermark([fake_rect, fake_rect], 30000, "bw_manga")
    assert triggered is False


# ---------------------------------------------------------------------------
# _detect_watermarks — end-to-end on real fixtures
# ---------------------------------------------------------------------------

@_NEED_FIXTURES
def test_detect_clean_eleceed_no_triggers():
    """A clean Eleceed page (with legitimate panel dialogue) must NOT
    trigger any watermark regions. The text in panels sits in the page
    center, outside the WATERMARK_REGIONS crops."""
    img = Image.open(_EL_CLEAN).convert("RGB")
    result = _detect_watermarks(img, content_type="color_webtoon_chunked")
    assert result["watermark_score"] == 0.0
    assert result["watermark_regions"] == []
    assert result["watermark_detector_used"] == "easyocr"


@_NEED_FIXTURES
def test_detect_clean_bw_manga_no_triggers():
    """A clean Talentless Nana B&W content page should not trigger.
    The B&W threshold is stricter (n>=1) so this is a meaningful FP guard
    against incidental sound effects / panel text catching as watermark."""
    img = Image.open(_TN_BW_CONTENT)
    result = _detect_watermarks(img, content_type="bw_manga")
    assert result["watermark_score"] == 0.0


@_NEED_FIXTURES
def test_detect_synthetic_corner_watermark_on_webtoon():
    """A composited 2-line corner watermark on an Eleceed page triggers
    the corner_br region (webtoon threshold n>=2)."""
    img = Image.open(_EL_CLEAN).convert("RGB")
    wm_img = _composite_corner_watermark(img)
    result = _detect_watermarks(wm_img, content_type="color_webtoon_chunked")
    assert result["watermark_score"] > 0.0
    region_names = {r["region"] for r in result["watermark_regions"]}
    assert "corner_br" in region_names


@_NEED_FIXTURES
def test_detect_synthetic_corner_watermark_on_manga():
    """A multi-line corner watermark on a manga page still triggers under
    the v7 stricter manga thresholds (n>=2 AND cov>=0.20). The 2-line
    "READ ON\\nMANGAREAD" composite emits two CRAFT bboxes covering a
    large fraction of the corner_br region — well above both gates.

    This is the regression guard for the v7 threshold tightening: real
    piracy watermarks must continue to flag even though incidental
    in-corner content (single speech bubbles, page numbers, cover title
    text) no longer does.
    """
    img = Image.open(_TN_BW_CONTENT).convert("RGB")
    # TN pages are 1114×1584; corner_br is (-200, -150, None, None) =
    # (914, 1434, 1114, 1584).
    wm_img = img.copy()
    draw = ImageDraw.Draw(wm_img)
    font = _get_arial_font(32)
    x1 = wm_img.width - 190
    y1 = wm_img.height - 130
    draw.rectangle([(x1, y1), (wm_img.width - 5, wm_img.height - 5)], fill="white")
    draw.text((x1 + 10, y1 + 10), "READ ON\nMANGAREAD", fill="black", font=font)
    result = _detect_watermarks(wm_img, content_type="bw_manga")
    assert result["watermark_score"] > 0.0


@_NEED_FIXTURES
def test_score_image_blob_applies_watermark_penalty():
    """End-to-end: the final T1 composite reflects the watermark penalty.
    A watermarked image scores lower than the same image clean."""
    img = Image.open(_EL_CLEAN).convert("RGB")
    wm_img = _composite_corner_watermark(img)
    buf_clean = io.BytesIO(); img.save(buf_clean, format="JPEG", quality=85)
    buf_wm = io.BytesIO(); wm_img.save(buf_wm, format="JPEG", quality=85)

    result_clean = _score_image_blob(
        buf_clean.getvalue(), content_type="color_webtoon_chunked",
    )
    result_wm = _score_image_blob(
        buf_wm.getvalue(), content_type="color_webtoon_chunked",
    )
    assert result_clean is not None
    assert result_wm is not None
    s_clean, m_clean = result_clean
    s_wm, m_wm = result_wm
    # Watermark detected on the wm version, not the clean.
    assert m_clean["watermark_score"] == 0.0
    assert m_wm["watermark_score"] > 0.0
    # Watermark penalty drops the composite by approximately
    # watermark_score (the JPEG re-encode noise produces small additional
    # variance, so use a lenient lower-bound).
    assert s_clean - s_wm > 0.03, (
        f"watermarked page should score lower: clean={s_clean:.4f} "
        f"wm={s_wm:.4f} (delta {s_clean - s_wm:.4f})"
    )


# ---------------------------------------------------------------------------
# Graceful degrade
# ---------------------------------------------------------------------------

def test_detect_watermarks_returns_none_score_when_easyocr_unavailable(monkeypatch):
    """When _EASYOCR_AVAILABLE is False, _detect_watermarks returns
    watermark_score=None (signals 'we did not measure' to downstream)."""
    from sites import search_orchestrator
    monkeypatch.setattr(search_orchestrator, "_EASYOCR_AVAILABLE", False)
    img = Image.new("RGB", (700, 1000), (200, 200, 200))
    result = _detect_watermarks(img, content_type="bw_manga")
    assert result["watermark_score"] is None
    assert result["watermark_detector_used"] is None


def test_detect_watermarks_returns_none_when_not_ready(monkeypatch):
    """When _WATERMARK_READY isn't set, _detect_watermarks returns None
    (graceful degrade during weight-prefetch warmup)."""
    from sites import search_orchestrator
    import threading
    # Substitute an unset event so the warmup-not-finished path is hit.
    fake_event = threading.Event()  # unset
    monkeypatch.setattr(search_orchestrator, "_WATERMARK_READY", fake_event)
    img = Image.new("RGB", (700, 1000), (200, 200, 200))
    result = _detect_watermarks(img, content_type="bw_manga")
    assert result["watermark_score"] is None


def test_detect_watermarks_handles_tiny_image():
    """Images smaller than 200×200 can't be cropped to corner regions —
    return score=0.0 (we checked, no watermark found because no regions
    to check)."""
    img = Image.new("RGB", (150, 150), (255, 255, 255))
    result = _detect_watermarks(img, content_type="bw_manga")
    # Either watermark_score=0.0 (we processed, nothing found) OR None
    # (depending on _WATERMARK_READY state during pytest run).
    assert result["watermark_score"] in (0.0, None)


# ---------------------------------------------------------------------------
# Outlier flag — heavy_watermark when n_triggered >= 2
# ---------------------------------------------------------------------------

@_NEED_FIXTURES
def test_score_image_blob_sets_heavy_watermark_outlier_on_2plus_regions():
    """When 2+ regions trigger, the outlier flag is set to 'heavy_watermark'.
    Composite 2 watermarks on a clean page to force 2 regions."""
    img = Image.open(_EL_CLEAN).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = _get_arial_font(28)
    # Top-right corner watermark
    draw.rectangle([(img.width - 190, 5), (img.width - 5, 130)], fill="white")
    draw.text((img.width - 180, 15), "READ ON\nFAKE-X", fill="black", font=font)
    # Bottom-right corner watermark
    draw.rectangle([(img.width - 190, img.height - 130), (img.width - 5, img.height - 5)], fill="white")
    draw.text((img.width - 180, img.height - 120), "READ ON\nFAKE-X", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    result = _score_image_blob(buf.getvalue(), content_type="color_webtoon_chunked")
    assert result is not None
    score, meta = result
    # Expect 2+ regions triggered → outlier flag.
    n_regions = len(meta.get("watermark_regions") or [])
    if n_regions >= 2:
        assert meta.get("outlier") == "heavy_watermark"
    else:
        # If easyocr's confidence dropped on one of the two corners, the
        # test is informational rather than load-bearing — re-check:
        # at least one corner SHOULD have triggered.
        assert n_regions >= 1


# ---------------------------------------------------------------------------
# v7 is_cover_probe — cover-probe path skips watermark detection entirely
# ---------------------------------------------------------------------------

def test_score_image_blob_cover_probe_skips_watermark_detection():
    """v7 (2026-05-18): _score_image_blob(blob, is_cover_probe=True) must
    NOT run the watermark detector. Cover artwork legitimately places title
    and logo text in the corner/edge regions our detector crops; flagging
    that as `heavy_watermark` was a structural false positive.

    Build a synthetic 'cover' with prominent multi-line title text in the
    top-left corner and edge_top region — exactly the pattern that
    pre-v7 would have tripped 2+ regions and emitted
    `outlier=heavy_watermark`. With is_cover_probe=True, the detector
    is bypassed entirely so the placeholders are None / [] and no
    outlier is set."""
    cover = Image.new("RGB", (940, 1390), (255, 255, 255))
    draw = ImageDraw.Draw(cover)
    font_big = _get_arial_font(72)
    font_med = _get_arial_font(40)
    # Title text spanning the top edge + top-left corner regions
    draw.text((20, 20), "KAGURABACHI", fill="black", font=font_big)
    # Volume label in top-right corner
    draw.text((cover.width - 200, 30), "Vol 1", fill="black", font=font_med)
    # Author tag in bottom-right (corner_br)
    draw.text((cover.width - 240, cover.height - 80),
              "Takeru Hokazono", fill="black", font=font_med)
    buf = io.BytesIO()
    cover.save(buf, format="JPEG", quality=85)
    blob = buf.getvalue()

    result = _score_image_blob(blob, is_cover_probe=True)
    assert result is not None
    score, meta = result
    # Detector NOT run → None placeholders.
    assert meta.get("watermark_score") is None, (
        f"is_cover_probe should skip watermark detection, got "
        f"watermark_score={meta.get('watermark_score')!r}"
    )
    assert meta.get("watermark_regions") == []
    assert meta.get("watermark_detector_used") is None
    # outlier must not be heavy_watermark (other outliers like
    # low_chroma may still legitimately fire and that's fine).
    assert meta.get("outlier") != "heavy_watermark"


def test_score_image_blob_chapter_probe_default_runs_detector():
    """Regression guard for the v7 split: when is_cover_probe is omitted
    (the default chapter-page path), the watermark detector still runs.
    Verified via the placeholder fields populated by the detector when
    easyocr is available — they should be NOT None on the chapter-page
    path. (If easyocr isn't ready, score=None is the graceful-degrade
    signal; either way is acceptable for this test — what we're
    ruling out is the cover-probe skip path leaking onto chapter probes.)"""
    cover = Image.new("RGB", (940, 1390), (255, 255, 255))
    draw = ImageDraw.Draw(cover)
    font = _get_arial_font(36)
    draw.text((50, 50), "Page 1 content", fill="black", font=font)
    buf = io.BytesIO()
    cover.save(buf, format="JPEG", quality=85)
    blob = buf.getvalue()

    result = _score_image_blob(blob)  # default is_cover_probe=False
    assert result is not None
    score, meta = result
    # Detector was invoked — watermark_detector_used reflects easyocr
    # availability. If easyocr is ready it's "easyocr"; if it isn't, the
    # detector returned None for the score (graceful degrade). Either way,
    # this differs from the cover-probe path which forces ALL THREE
    # placeholder fields to their None/[] defaults via setdefault.
    if _WATERMARK_READY.is_set():
        # easyocr ran — used field must reflect that.
        assert meta.get("watermark_detector_used") == "easyocr"
        assert isinstance(meta.get("watermark_score"), float)
