"""Phase 4 integration tests for torchmetrics CLIP-IQA+ in T2.

Validates:
  - _get_clip_iqa_model handles missing-dep gracefully
  - _compute_t2_score accepts content_type and dispatches signal mix
    correctly (B&W skips NIQE, color uses both)
  - clip_iqa_score key appears in metadata when CLIP-IQA succeeds

The "live model inference" test runs the actual CLIP-IQA forward pass —
~0.6 s on CPU per image — guarded by a skip when torchmetrics isn't
installed (CI without torchmetrics still passes the dispatch-logic
tests).
"""
from __future__ import annotations

import io
import threading
import pytest
from PIL import Image, ImageDraw

from sites.search_orchestrator import (
    _TORCHMETRICS_AVAILABLE,
    _compute_t2_score,
    _get_clip_iqa_model,
    _T2_READY,
)


needs_torchmetrics = pytest.mark.skipif(
    not _TORCHMETRICS_AVAILABLE,
    reason="torchmetrics not installed",
)


def _make_synthetic_manga_blob():
    """Build a small JPEG that looks vaguely manga-like — sparse lines on
    light background. CLIP-IQA's "clean manga" antonym should score this
    above 0.0 (synthetic but recognizable content).
    """
    img = Image.new("L", (512, 768), 240)
    draw = ImageDraw.Draw(img)
    for i in range(8):
        col = (i + 1) * 64
        draw.line([(col, 50), (col, 700)], fill=20, width=2)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue(), img


@needs_torchmetrics
def test_get_clip_iqa_model_returns_a_model():
    """Lazy-init returns either a model instance or None (never crashes).
    First call may download weights (~150 MB) — subsequent calls are
    cached.
    """
    m = _get_clip_iqa_model()
    # On systems where the model is already cached, m is non-None.
    # On systems where the HF Hub is unreachable, m is None. Both are
    # valid outcomes — we just check no crash.
    assert m is None or hasattr(m, "forward") or callable(m)


@needs_torchmetrics
def test_compute_t2_score_populates_clip_iqa_field_for_bw():
    """For B&W content, T2 skips NIQE entirely and uses CLIP-IQA+. The
    clip_iqa_score metadata field must be populated (non-None) when the
    inference succeeds.
    """
    # Force _T2_READY for the test — warmup_t2_models would normally set
    # it in a daemon thread; we set it here so _compute_t2_score doesn't
    # short-circuit on the readiness gate.
    _T2_READY.set()

    blob, img = _make_synthetic_manga_blob()
    t2_composite, meta = _compute_t2_score(
        blob, img, is_grayscale=True, content_type="bw_manga",
    )
    # niqe should be None (skipped on B&W).
    assert meta["niqe_score"] is None
    assert meta["niqe_norm"] is None
    # clip_iqa_score should be a float in [0, 1].
    assert meta["clip_iqa_score"] is not None, f"meta={meta}"
    assert 0.0 <= meta["clip_iqa_score"] <= 1.0
    # T2 composite should equal clip_iqa_score (only signal in the mix).
    # Tolerance accounts for the round-to-4-decimals applied to the
    # metadata field; t2_composite is the raw mean.
    assert t2_composite is not None
    assert abs(t2_composite - meta["clip_iqa_score"]) < 1e-3
    assert meta["t2_available"] is True


@needs_torchmetrics
def test_compute_t2_score_color_content_uses_both_niqe_and_clip():
    """For color/unknown content, both NIQE and CLIP-IQA fire and the
    composite averages them. Both metadata fields must be populated.
    """
    _T2_READY.set()
    blob, img = _make_synthetic_manga_blob()
    t2_composite, meta = _compute_t2_score(
        blob, img, is_grayscale=False, content_type="color_manga",
    )
    # NIQE should have run (color content).
    # It may still be None if the image is too small for NIQE's 96-pixel
    # block-count gate, but at 512x768 it should succeed.
    assert meta["niqe_score"] is not None, f"NIQE should fire on 512x768 color: meta={meta}"
    # CLIP-IQA should also have run.
    assert meta["clip_iqa_score"] is not None
    # T2 composite is the mean of (1-niqe_norm) and clip_iqa_score.
    expected = ((1.0 - meta["niqe_norm"]) + meta["clip_iqa_score"]) / 2.0
    assert t2_composite is not None
    assert abs(t2_composite - expected) < 1e-3, (
        f"composite {t2_composite} != expected {expected} "
        f"(niqe_norm={meta['niqe_norm']}, clip={meta['clip_iqa_score']})"
    )


def test_compute_t2_score_signature_accepts_content_type():
    """Even without torchmetrics installed, the signature must accept
    content_type as a keyword arg (otherwise the _score_image_blob call
    site would crash).
    """
    blob, img = _make_synthetic_manga_blob()
    # When pyiqa/torchmetrics unavailable OR T2_READY not set, returns
    # (None, {"t2_available": False}). Validating the call signature
    # itself doesn't require any model.
    try:
        result = _compute_t2_score(
            blob, img, is_grayscale=True, content_type="bw_manga",
        )
        # Either (None, dict) or (float, dict) — both valid.
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[1], dict)
    except TypeError as e:
        pytest.fail(f"signature change broke call site: {e}")


def test_metadata_schema_includes_clip_iqa_score_key():
    """The v6 schema must include `clip_iqa_score` (the new scalar) in
    addition to the legacy `clip_iqa_scores` / `clip_iqa_mean` keys.
    """
    _T2_READY.set()
    blob, img = _make_synthetic_manga_blob()
    _, meta = _compute_t2_score(
        blob, img, is_grayscale=True, content_type="bw_manga",
    )
    # Even when CLIP-IQA fails to init, the key must be in the dict
    # (set to None). Other consumers shouldn't need defensive checks.
    assert "clip_iqa_score" in meta
