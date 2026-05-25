"""Unit tests for the B&W-specialized T1 weight tables and constants in
sites/t1_constants.py. Verifies:
  - All new B&W weight tables sum to 1.0 (so penalties / bonuses
    subtract / add on top of a [0, 1] composite).
  - Legacy weight tables are unchanged (no regression risk for the
    color-manga / webtoon paths).
  - Penalty and bonus constants are within sensible bounds.
  - Resolution-decay parameters are internally consistent.

The actual _compute_t1_score_bw integration test lives in
tests/test_search_orchestrator.py (Phase 3 work).
"""
from __future__ import annotations

import pytest

from sites import t1_constants as t1c


# --- Weight tables sum to 1.0 ----------------------------------------------


def test_bw_jpeg_weights_sum_to_one():
    for content_type, weights in t1c.T1_WEIGHTS_JPEG_BW.items():
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-6, (
            f"T1_WEIGHTS_JPEG_BW[{content_type!r}] sum = {total}, expected 1.0"
        )


def test_bw_webp_weights_sum_to_one():
    for content_type, weights in t1c.T1_WEIGHTS_WEBP_BW.items():
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-6, (
            f"T1_WEIGHTS_WEBP_BW[{content_type!r}] sum = {total}, expected 1.0"
        )


def test_bw_lossless_weights_sum_to_one():
    for content_type, weights in t1c.T1_WEIGHTS_LOSSLESS_BW.items():
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-6, (
            f"T1_WEIGHTS_LOSSLESS_BW[{content_type!r}] sum = {total}, expected 1.0"
        )


# --- Weight tables cover the right content types ---------------------------


def test_bw_weights_cover_both_bw_content_types():
    """Both bw_manga and bw_manga_with_color_inserts must be present in
    every B&W weight table — _compute_t1_score_bw is called for either.
    """
    for table_name in ("T1_WEIGHTS_JPEG_BW", "T1_WEIGHTS_WEBP_BW", "T1_WEIGHTS_LOSSLESS_BW"):
        table = getattr(t1c, table_name)
        assert "bw_manga" in table, f"{table_name} missing bw_manga"
        assert "bw_manga_with_color_inserts" in table, (
            f"{table_name} missing bw_manga_with_color_inserts"
        )


def test_bw_weights_use_expected_slot_names():
    """B&W weights must include the new slots: line, screen, bg. JPEG
    additionally has qf; WEBP omits it; LOSSLESS replaces qf with
    'lossless'. fft_hf is intentionally absent from B&W (screen
    subsumes it).
    """
    for content_type, w in t1c.T1_WEIGHTS_JPEG_BW.items():
        for slot in ("res", "qf", "line", "screen", "bg", "block", "tene"):
            assert slot in w, f"JPEG_BW[{content_type}] missing slot {slot!r}"
        assert "fft_hf" not in w, (
            f"JPEG_BW[{content_type}] should NOT have fft_hf (subsumed by screen)"
        )

    for content_type, w in t1c.T1_WEIGHTS_WEBP_BW.items():
        for slot in ("res", "line", "screen", "bg", "block", "tene"):
            assert slot in w, f"WEBP_BW[{content_type}] missing slot {slot!r}"
        assert "qf" not in w, f"WEBP_BW[{content_type}] should NOT have qf"

    for content_type, w in t1c.T1_WEIGHTS_LOSSLESS_BW.items():
        for slot in ("res", "lossless", "line", "screen", "bg", "block", "tene"):
            assert slot in w, f"LOSSLESS_BW[{content_type}] missing slot {slot!r}"


# --- Legacy weight tables unchanged ----------------------------------------


def test_legacy_jpeg_weights_byte_for_byte():
    """The non-B&W (legacy) tables must NOT have been altered by Phase 2."""
    expected = {
        "bw_manga":                    {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
        "bw_manga_with_color_inserts": {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
        "color_manga":                 {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
        "color_webtoon_chunked":       {"res": 0.35, "qf": 0.25, "block": 0.15, "fft_hf": 0.20, "tene": 0.05},
        "color_webtoon_single_image":  {"res": 0.35, "qf": 0.25, "block": 0.15, "fft_hf": 0.20, "tene": 0.05},
        "unknown":                     {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
    }
    assert t1c.T1_WEIGHTS_JPEG == expected


def test_legacy_webp_weights_byte_for_byte():
    expected = {
        "bw_manga":                    {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
        "bw_manga_with_color_inserts": {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
        "color_manga":                 {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
        "color_webtoon_chunked":       {"res": 0.40, "block": 0.20, "fft_hf": 0.25, "tene": 0.15},
        "color_webtoon_single_image":  {"res": 0.40, "block": 0.20, "fft_hf": 0.25, "tene": 0.15},
        "unknown":                     {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
    }
    assert t1c.T1_WEIGHTS_WEBP == expected


# --- Penalty / bonus magnitudes within sensible bounds ---------------------


def test_bw_penalty_constants_within_bounds():
    """Penalties must be in (0, 0.20] so individual signals can't push
    the composite below 0 even on multiple-misfire pages.
    """
    assert 0 < t1c.UPSCALER_PENALTY_MAX <= 0.20
    assert 0 < t1c.BILEVEL_PENALTY_BW <= 0.20
    assert 0 < t1c.CHROMA_PENALTY_BW <= 0.20
    assert 0 < t1c.LOSSLESS_BONUS_BW <= 0.10  # bonus smaller than penalty


def test_total_bw_penalty_bounded():
    """If every penalty fires simultaneously, the total subtracted from
    T1_BW must stay below 0.40 — leaves room for the weighted-sum
    component (typically 0.5-0.8) to remain positive.
    """
    max_subtract = (
        t1c.UPSCALER_PENALTY_MAX
        + t1c.BILEVEL_PENALTY_BW
        + t1c.CHROMA_PENALTY_BW
    )
    assert max_subtract < 0.40, (
        f"max combined penalty {max_subtract} too aggressive — could "
        f"zero out the composite on multiple-misfire pages"
    )


# --- Resolution-decay shape -------------------------------------------------


def _decay_res_norm(area_ratio: float) -> float:
    """Reference implementation of the v6 res_norm decay shape.
    _compute_t1_score_bw will inline this; the test asserts the shape
    matches expectations at key points.

    Below DECAY_START → 1.0
    Linear decay between DECAY_START and DECAY_END → 1.0 → FLOOR
    Above DECAY_END → FLOOR
    """
    if area_ratio <= 1.0:
        return float(min(1.0, max(0.0, area_ratio)))  # legacy area/target clamp
    if area_ratio <= t1c.RES_NORM_DECAY_START:
        return 1.0
    if area_ratio >= t1c.RES_NORM_DECAY_END:
        return t1c.RES_NORM_FLOOR
    # Linear decay between (DECAY_START, 1.0) and (DECAY_END, FLOOR).
    span = t1c.RES_NORM_DECAY_END - t1c.RES_NORM_DECAY_START
    progress = (area_ratio - t1c.RES_NORM_DECAY_START) / span
    return 1.0 - progress * (1.0 - t1c.RES_NORM_FLOOR)


def test_res_norm_decay_below_target_unchanged():
    """Area at or below target → standard area/target ratio."""
    assert _decay_res_norm(0.5) == 0.5
    assert _decay_res_norm(1.0) == 1.0


def test_res_norm_decay_in_safe_zone():
    """Area between target and DECAY_START × target → still 1.0 (no penalty
    for genuinely-high-quality scans)."""
    assert _decay_res_norm(1.2) == 1.0
    assert _decay_res_norm(t1c.RES_NORM_DECAY_START) == 1.0


def test_res_norm_decay_partial():
    """Halfway through the decay band → halfway between 1.0 and FLOOR."""
    mid = (t1c.RES_NORM_DECAY_START + t1c.RES_NORM_DECAY_END) / 2
    expected = 1.0 - 0.5 * (1.0 - t1c.RES_NORM_FLOOR)
    assert abs(_decay_res_norm(mid) - expected) < 1e-6


def test_res_norm_decay_floor_clamped():
    """Above DECAY_END → exactly FLOOR, no further decay."""
    assert _decay_res_norm(t1c.RES_NORM_DECAY_END) == t1c.RES_NORM_FLOOR
    assert _decay_res_norm(t1c.RES_NORM_DECAY_END * 2) == t1c.RES_NORM_FLOOR


def test_res_norm_decay_parameters_internally_consistent():
    assert t1c.RES_NORM_DECAY_END > t1c.RES_NORM_DECAY_START > 1.0
    assert 0.5 <= t1c.RES_NORM_FLOOR < 1.0


# --- Defensive: weight names cover what _compute_t1_score_bw expects ------


# --- Phase 3 integration: dispatch from _score_image_blob ----------------


def _make_bw_test_blob(size=(1500, 1350)):
    """Build a JPEG blob that mimics a B&W manga page: midtone-gray
    screentone region plus dark lines. Approximate the 2M-px res_norm
    target so res_norm = 1.0 cleanly.
    """
    import io
    import numpy as np
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("L", (w, h), 240)
    draw = ImageDraw.Draw(img)
    # Midtone screentone area
    for y in range(h // 3, 2 * h // 3, 5):
        for x in range(w // 4, 3 * w // 4, 5):
            draw.ellipse([x - 1, y - 1, x + 1, y + 1], fill=110)
    # Sparse line art
    for i in range(5):
        col = (i + 1) * w // 6
        draw.line([(col, 50), (col, h - 50)], fill=20, width=2)
    rng = np.random.RandomState(7)
    arr = np.asarray(img, dtype=np.float32)
    arr += rng.normal(0, 4, arr.shape)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    out_img = Image.fromarray(arr, mode="L")
    buf = io.BytesIO()
    out_img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_score_image_blob_bw_dispatch_populates_new_fields():
    """When content_type='bw_manga', _score_image_blob routes through
    _compute_t1_score_bw — verify the bw_* metadata keys appear.
    """
    from sites.search_orchestrator import _score_image_blob
    blob = _make_bw_test_blob()
    result = _score_image_blob(blob, content_type="bw_manga")
    assert result is not None
    score, meta = result
    assert 0.0 <= score <= 1.0
    # New B&W keys must be present.
    for key in (
        "bw_screentone_score", "bw_line_quality", "bw_upscaler_score",
        "bw_bilevel", "bw_bg_uniformity",
        "bw_upscaler_penalty", "bw_bilevel_penalty", "bw_chroma_penalty",
        "bw_lossless_bonus",
    ):
        assert key in meta, f"missing bw key: {key}"
    # Should report the dispatched content type.
    assert meta["content_type"] == "bw_manga"
    # fft_hf_ratio is replaced by screentone in B&W path → reported as None.
    assert meta["fft_hf_ratio"] is None
    # Standard v5 keys must STILL be present for back-compat.
    for key in (
        "width", "height", "format", "size_bytes", "is_grayscale",
        "is_lossless", "bpp", "res_norm", "res_norm_target", "blockiness",
        "tenengrad", "tenengrad_norm", "tenengrad_clean",
        "usm_overshoot_score", "content_type",
        "jpeg_qf", "jpeg_qf_norm", "decode_quality", "t1_score",
    ):
        assert key in meta, f"missing v5-compat key: {key}"


def test_score_image_blob_color_manga_keeps_legacy_path():
    """When content_type='color_manga', the legacy _compute_t1_score
    formula must still fire — bw_* fields should NOT appear.
    """
    from sites.search_orchestrator import _score_image_blob
    blob = _make_bw_test_blob()  # any blob — we're testing the dispatch
    result = _score_image_blob(blob, content_type="color_manga")
    assert result is not None
    _, meta = result
    # Legacy v5 keys present.
    assert meta["content_type"] == "color_manga"
    assert meta.get("fft_hf_ratio") is not None  # legacy formula uses fft_hf
    # B&W-specific keys must NOT be present (no spurious bw_screentone on
    # the color path).
    assert "bw_screentone_score" not in meta
    assert "bw_line_quality" not in meta


def test_score_image_blob_unknown_content_type_stays_legacy():
    """Default content_type='unknown' must keep the legacy formula
    (back-compat with callers that don't pass content_type).
    """
    from sites.search_orchestrator import _score_image_blob
    blob = _make_bw_test_blob()
    result = _score_image_blob(blob)  # no content_type → defaults to "unknown"
    assert result is not None
    _, meta = result
    assert meta["content_type"] == "unknown"
    assert "bw_screentone_score" not in meta


def test_no_unexpected_slots_in_bw_weights():
    """Guard against typos. Every B&W weight slot must be one of the
    documented names.
    """
    allowed_jpeg = {"res", "qf", "line", "screen", "bg", "block", "tene"}
    allowed_webp = {"res", "line", "screen", "bg", "block", "tene"}
    allowed_lossless = {"res", "lossless", "line", "screen", "bg", "block", "tene"}
    for content_type, w in t1c.T1_WEIGHTS_JPEG_BW.items():
        assert set(w.keys()) == allowed_jpeg, (
            f"JPEG_BW[{content_type}] keys = {set(w.keys())} != {allowed_jpeg}"
        )
    for content_type, w in t1c.T1_WEIGHTS_WEBP_BW.items():
        assert set(w.keys()) == allowed_webp, (
            f"WEBP_BW[{content_type}] keys = {set(w.keys())} != {allowed_webp}"
        )
    for content_type, w in t1c.T1_WEIGHTS_LOSSLESS_BW.items():
        assert set(w.keys()) == allowed_lossless, (
            f"LOSSLESS_BW[{content_type}] keys = {set(w.keys())} != {allowed_lossless}"
        )
