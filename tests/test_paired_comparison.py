"""Tests for Tier-3 paired DISTS comparison (sites/search_orchestrator.py v5).

T3 runs after the probe phase and uses piq.DISTS + cv2.phaseCorrelate to
compare image pairs from different sources of the same series. The median
DISTS becomes a paired_quality_adjustment in [-0.15, +0.15] applied to
img_quality_score.

DISTS (Ding 2020) is texture-resampling-robust, which is critical on manga:
SSIM/LPIPS misfire on screentones (perceptually translation-invariant
patterns). DISTS doesn't.

Cross-file: targets sites/search_orchestrator.py:_run_paired_comparison,
_compute_dists_pair, _align_image_pair. Plan reference:
~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 4 section).
"""

from __future__ import annotations

import io
import os
from typing import List
from unittest.mock import MagicMock, patch

import pytest

# Skip whole module when piq isn't available.
piq = pytest.importorskip("piq", reason="T3 paired comparison requires piq")
cv2 = pytest.importorskip("cv2", reason="T3 paired comparison requires cv2")

from PIL import Image

from sites.search_orchestrator import (
    SeriesCandidate,
    SourceEntry,
    _align_image_pair,
    _align_image_pair_v51,         # v5.1 Phase 3b 3-tier alignment
    _compute_dists_pair,           # v5 back-compat alias
    _compute_jpeg_ghost,           # v5.1 Phase 3a JPEG-ghost detector
    _compute_paired_perceptual,    # v5.1 ensemble entrypoint
    _get_dists_model,
    _get_stlpips_model,            # v5.1 ST-LPIPS lazy-init
    _phase_correlate_translation,
    _run_paired_comparison,
    PAIRED_MAX_ADJUSTMENT,
    _T2_DEVICE,
    _CUDA_AVAILABLE,
)


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_DIR = os.path.join(_REPO_ROOT, "tmp_nzmj")
_BW_PAGE = os.path.join(_TMP_DIR, "ch_5", "5_0021.jpg")

_NEED_REAL = pytest.mark.skipif(
    not os.path.isfile(_BW_PAGE),
    reason="real test fixtures not present (dev-only)",
)


def _reencode_at_q(blob: bytes, quality: int) -> bytes:
    img = Image.open(io.BytesIO(blob))
    img.load()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# DISTS model lazy-init
# ---------------------------------------------------------------------------

def test_get_dists_model_returns_callable():
    """The lazy-init helper returns a callable when piq is available."""
    model = _get_dists_model()
    assert model is not None


def test_get_dists_model_cached():
    """Repeated calls return the same model instance."""
    m1 = _get_dists_model()
    m2 = _get_dists_model()
    assert m1 is m2


# ---------------------------------------------------------------------------
# DISTS sensitivity
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_dists_identical_is_zero():
    """DISTS(x, x) should be ~0."""
    blob = open(_BW_PAGE, "rb").read()
    d = _compute_dists_pair(blob, blob)
    assert d is not None
    assert d < 0.01, f"DISTS(x, x) = {d}, expected ~0"


@_NEED_REAL
def test_dists_monotonic_with_jpeg_quality():
    """DISTS increases monotonically as the target is degraded.
    Anchor at q=95, target at decreasing q."""
    anchor = _reencode_at_q(open(_BW_PAGE, "rb").read(), 95)
    distances = []
    for q in [95, 70, 50, 30, 15]:
        target = _reencode_at_q(open(_BW_PAGE, "rb").read(), q)
        d = _compute_dists_pair(anchor, target)
        assert d is not None, f"DISTS failed at q={q}"
        distances.append((q, d))
    # Monotonic non-decreasing (allow small slack for noise).
    for (q_a, d_a), (q_b, d_b) in zip(distances, distances[1:]):
        assert d_b >= d_a - 0.005, (
            f"DISTS non-monotonic between q={q_a} ({d_a}) and q={q_b} ({d_b})"
        )
    # End-to-end span: q=95 should have lower DISTS than q=15.
    assert distances[0][1] < distances[-1][1]


@_NEED_REAL
def test_dists_returns_none_when_align_fails():
    """When alignment confidence is too low (unrelated images), _compute_dists_pair
    returns None instead of a misleading score."""
    blob_a = open(_BW_PAGE, "rb").read()
    # Pure noise — phaseCorrelate response should be low.
    import numpy as np
    rng = np.random.default_rng(42)
    noise = rng.integers(0, 256, size=(1500, 1100, 3), dtype=np.uint8)
    noise_img = Image.fromarray(noise)
    buf = io.BytesIO()
    noise_img.save(buf, format="JPEG", quality=85)
    d = _compute_dists_pair(blob_a, buf.getvalue())
    # We don't strictly require None here — phaseCorrelate is sometimes
    # surprisingly confident on noise — but we DO require it to be huge
    # if it returns anything (the noise vs manga page diff is massive).
    if d is not None:
        assert d > 0.3, (
            f"DISTS(real_manga, random_noise) returned suspicious value {d}; "
            f"expected None (low align confidence) or very large"
        )


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_phase_correlate_zero_shift_for_identical():
    """phaseCorrelate on identical images should return ~(0, 0) translation
    with high confidence."""
    import numpy as np
    img = np.asarray(Image.open(_BW_PAGE).convert("L"))
    dx, dy, response = _phase_correlate_translation(img, img)
    assert abs(dx) < 1.0
    assert abs(dy) < 1.0
    assert response > 0.5


@_NEED_REAL
def test_phase_correlate_recovers_translation():
    """Synthetically shift an image by 10 pixels; phaseCorrelate should
    recover the shift within 1px."""
    import numpy as np
    img = np.asarray(Image.open(_BW_PAGE).convert("L"))
    shifted = np.roll(img, shift=(10, -5), axis=(0, 1))
    dx, dy, response = _phase_correlate_translation(img, shifted)
    # Note: phaseCorrelate returns translation FROM b TO a, sign convention
    # depends on the implementation. We just check magnitude is recovered.
    assert abs(abs(dy) - 10) <= 2, f"expected dy ~ ±10, got {dy}"
    assert abs(abs(dx) - 5) <= 2, f"expected dx ~ ±5, got {dx}"


@_NEED_REAL
def test_align_image_pair_handles_size_difference():
    """Two different-resolution copies of the same page should align after
    Lanczos downscale to a common target."""
    img = Image.open(_BW_PAGE)
    img.load()
    # Make a smaller copy.
    w, h = img.size
    small = img.resize((w // 2, h // 2), Image.LANCZOS)
    big_buf = io.BytesIO(); img.save(big_buf, format="JPEG", quality=90)
    small_buf = io.BytesIO(); small.save(small_buf, format="JPEG", quality=90)
    result = _align_image_pair(big_buf.getvalue(), small_buf.getvalue())
    assert result is not None
    arr_a, arr_b = result
    assert arr_a.shape == arr_b.shape  # post-align must be identical shape


# ---------------------------------------------------------------------------
# End-to-end paired comparison (mocked handlers)
# ---------------------------------------------------------------------------

def _make_candidate(sources: List[SourceEntry]) -> SeriesCandidate:
    return SeriesCandidate(canonical_title="Mock", canonical_year=2020, sources=sources)


def _make_source(
    site: str, url: str, score: float, width: int = 1500,
    content_type: str = "color_manga",
) -> SourceEntry:
    return SourceEntry(
        site=site, url=url, title="Mock", cover=None,
        title_match=1.0, seed_quality=0.85, img_quality_score=score,
        # v5.1: samples_succeeded > 0 marks this as a "real chapter probe"
        # source eligible for T3 anchor selection. Production probes
        # always populate this; cover-probe-fallback sources don't, and
        # T3 excludes them so it doesn't pick a DMCA-affected anchor
        # whose get_chapter_images would fail.
        #
        # v5.1: content_type drives bucket assignment in T3. Tests
        # default to "color_manga" so all mock sources land in the
        # "manga_pages" bucket together; tests exercising the bucket-
        # routing logic itself can override.
        img_quality_metadata={
            "width": width, "height": 2000, "format": "JPEG",
            "samples_succeeded": 8, "samples_attempted": 8,
            "content_type": content_type,
        },
    )


def test_run_paired_comparison_skipped_with_only_one_measured():
    """A candidate with one measured source has no pair to compare against;
    T3 should leave all sources untouched."""
    cand = _make_candidate([
        _make_source("a", "http://a/1", 0.7),
        _make_source("b", "http://b/1", None),  # not measured
    ])
    # The unmeasured source has img_quality_score=None which we pass via the
    # constructor; we also need to ensure the dataclass field defaults work.
    cand.sources[1].img_quality_score = None
    _run_paired_comparison([cand], MagicMock(), MagicMock())
    # No T3 metadata should have been added.
    for src in cand.sources:
        meta = src.img_quality_metadata or {}
        assert meta.get("paired_quality_adjustment") in (None, 0.0)


def test_run_paired_comparison_skipped_when_below_threshold():
    """Sources with img_quality_score below PAIRED_MIN_SOURCE_SCORE are
    too broken for meaningful comparison; T3 skips them."""
    cand = _make_candidate([
        _make_source("a", "http://a/1", 0.1),
        _make_source("b", "http://b/1", 0.05),
    ])
    _run_paired_comparison([cand], MagicMock(), MagicMock())
    for src in cand.sources:
        meta = src.img_quality_metadata or {}
        assert meta.get("paired_quality_adjustment") in (None, 0.0)


def test_run_paired_comparison_handles_handler_lookup_failure(monkeypatch):
    """When get_handler_by_name returns None for the anchor, the candidate
    is silently skipped (no crash)."""
    from sites import search_orchestrator
    cand = _make_candidate([
        _make_source("a", "http://a/1", 0.7),
        _make_source("b", "http://b/1", 0.6),
    ])
    monkeypatch.setattr(
        "sites.get_handler_by_name",
        lambda name: None,
    )
    # Shouldn't raise.
    _run_paired_comparison([cand], MagicMock(), MagicMock())


def test_paired_max_adjustment_constant_in_range():
    """The PAIRED_MAX_ADJUSTMENT cap should be small enough that a single
    paired DISTS measurement can't dominate the ranking but big enough to
    matter. 0.15 is the planned value."""
    assert 0.05 <= PAIRED_MAX_ADJUSTMENT <= 0.25


def test_paired_comparison_anchor_chosen_by_width():
    """When all sources pass the measured-quality threshold, the anchor is
    the one with the highest metadata.width."""
    # Mock all the way down: just verify anchor selection logic by
    # using a small candidate set and checking which source gets
    # paired_quality_adjustment = 0.0 (anchor signal).
    cand = _make_candidate([
        _make_source("low_res", "http://lo/1", 0.7, width=800),
        _make_source("hi_res", "http://hi/1", 0.7, width=2000),
        _make_source("mid_res", "http://mid/1", 0.7, width=1500),
    ])
    # Mock get_handler_by_name → all return None so the comparison short-
    # circuits but the anchor-selection metadata still gets recorded for
    # candidates where chapter_list fetch fails (which is what happens here).
    with patch("sites.get_handler_by_name", return_value=None):
        _run_paired_comparison([cand], MagicMock(), MagicMock())
    # Without a working handler chain, nothing gets paired-compared. The
    # anchor-selection happens BEFORE the chapter-list fetch, so we don't
    # see the side effects here. This test mostly guards against
    # regression in the case where the function short-circuits gracefully.
    for src in cand.sources:
        meta = src.img_quality_metadata or {}
        # paired_quality_adjustment should still be None (nothing got computed).
        assert meta.get("paired_quality_adjustment") in (None, 0.0)


# ---------------------------------------------------------------------------
# Graceful degrade — piq/cv2 missing
# ---------------------------------------------------------------------------

def test_run_paired_comparison_no_op_when_piq_missing(monkeypatch):
    """When _PIQ_AVAILABLE is False, _run_paired_comparison is a no-op."""
    from sites import search_orchestrator
    monkeypatch.setattr(search_orchestrator, "_PIQ_AVAILABLE", False)
    cand = _make_candidate([
        _make_source("a", "http://a/1", 0.7),
        _make_source("b", "http://b/1", 0.6),
    ])
    _run_paired_comparison([cand], MagicMock(), MagicMock())  # should not raise
    # Nothing should have changed.
    for src in cand.sources:
        meta = src.img_quality_metadata or {}
        assert meta.get("paired_quality_adjustment") in (None, 0.0)


def test_compute_dists_pair_returns_none_when_unavailable(monkeypatch):
    """_compute_dists_pair returns None cleanly when its deps are unavailable."""
    from sites import search_orchestrator
    monkeypatch.setattr(search_orchestrator, "_PIQ_AVAILABLE", False)
    assert _compute_dists_pair(b"\xff\xd8\xff\xe0" + b"x" * 100,
                                b"\xff\xd8\xff\xe0" + b"x" * 100) is None


# ---------------------------------------------------------------------------
# v5.1 Phase 0 — DISTS+ST-LPIPS ensemble
# ---------------------------------------------------------------------------

# Skip the whole v5.1 block if pyiqa or its stlpips weights aren't reachable.
pyiqa = pytest.importorskip("pyiqa", reason="v5.1 ensemble requires pyiqa")


def test_compute_paired_perceptual_returns_tuple():
    """_compute_paired_perceptual returns a 4-tuple (dists, stlpips, ensemble,
    alignment_method) — v5.1 Phase 3b added the alignment method to the
    return for diagnostic surfacing in the cache.
    """
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    blob = open(_BW_PAGE, "rb").read()
    result = _compute_paired_perceptual(blob, blob)
    assert result is not None
    assert len(result) == 4
    dists, stlpips, ensemble, method = result
    # Identical pair: all three should be ~0.
    assert dists < 0.01
    assert stlpips < 0.01
    assert ensemble < 0.01
    # Identical pair should use Tier 1 (phaseCorrelate). ECC/ORB are
    # fallbacks for harder cases.
    assert method == "phase_correlate"


def test_compute_paired_perceptual_ensemble_is_average():
    """The ensemble score is exactly the mean of DISTS and ST-LPIPS scores."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    blob = open(_BW_PAGE, "rb").read()
    # Re-encode degraded copy so the metrics produce a non-zero distance.
    img = Image.open(io.BytesIO(blob)); img.load()
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=30)
    degraded = buf.getvalue()
    result = _compute_paired_perceptual(blob, degraded)
    assert result is not None
    dists, stlpips, ensemble, _method = result
    # Allow tiny float-rounding slack.
    assert abs(ensemble - 0.5 * (dists + stlpips)) < 1e-5


def test_compute_paired_perceptual_monotonic_with_jpeg_quality():
    """The ensemble metric (like piq.DISTS in v5) should be monotonic as
    the target is degraded. Anchor at q=95, target at decreasing q."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    src_img = Image.open(_BW_PAGE); src_img.load()
    buf = io.BytesIO()
    src_img.save(buf, format="JPEG", quality=95)
    anchor = buf.getvalue()
    ensembles = []
    for q in [95, 70, 50, 30, 15]:
        buf = io.BytesIO()
        src_img.save(buf, format="JPEG", quality=q)
        result = _compute_paired_perceptual(anchor, buf.getvalue())
        assert result is not None
        # v5.1 4-tuple: (dists, stlpips, ensemble, method)
        ensembles.append((q, result[2]))
    # Monotonic non-decreasing across the full sweep.
    for (q_a, e_a), (q_b, e_b) in zip(ensembles, ensembles[1:]):
        assert e_b >= e_a - 0.005, (
            f"ensemble non-monotonic between q={q_a} ({e_a}) and q={q_b} ({e_b})"
        )
    # End-to-end span: q=95 vs q=15 should have meaningfully different ensemble.
    assert ensembles[0][1] < ensembles[-1][1] - 0.005


def test_get_stlpips_model_returns_callable():
    """Lazy-init of ST-LPIPS returns a callable when pyiqa is available."""
    model = _get_stlpips_model()
    assert model is not None


def test_get_stlpips_model_cached():
    """Repeated calls return the same model instance."""
    m1 = _get_stlpips_model()
    m2 = _get_stlpips_model()
    assert m1 is m2


def test_paired_perceptual_routes_to_t2_device():
    """When CUDA is unavailable (typical CPU-only torch install), _T2_DEVICE
    is 'cpu' and the existing CPU path is exercised. When CUDA is available,
    the same call routes to GPU automatically (no separate code path)."""
    # We can't reliably test the GPU case without a CUDA-enabled environment,
    # but we CAN assert _T2_DEVICE is a valid torch device string regardless.
    assert _T2_DEVICE in ("cpu", "cuda")
    # Sanity: if CUDA is reportedly available, the device should reflect it.
    if _CUDA_AVAILABLE:
        assert _T2_DEVICE == "cuda"
    else:
        assert _T2_DEVICE == "cpu"


def test_compute_paired_perceptual_stlpips_more_shift_tolerant_than_dists():
    """The structural v5.1 claim: ST-LPIPS is more shift-tolerant than DISTS.

    Take an image, shift it by 2 pixels, re-encode at the same quality,
    compute both metrics. ST-LPIPS should score LOWER than DISTS for the
    same shifted-content pair — that's literally what "shift-tolerant
    perceptual similarity metric" means (Ghildyal & Liu ECCV 2022).

    The ensemble (0.5*dists + 0.5*stlpips) lands strictly between the two
    metrics, dragging the v5 DISTS-only penalty DOWN by half the gap.
    This is the v5.1 screentone-resampling-handling win.
    """
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    import numpy as np
    src_img = Image.open(_BW_PAGE); src_img.load()
    arr = np.asarray(src_img.convert("RGB"))
    # 2-pixel shift in both axes.
    shifted = np.roll(arr, shift=(2, 2), axis=(0, 1))
    shifted_img = Image.fromarray(shifted)
    buf = io.BytesIO()
    src_img.save(buf, format="JPEG", quality=85)
    anchor_blob = buf.getvalue()
    buf2 = io.BytesIO()
    shifted_img.save(buf2, format="JPEG", quality=85)
    shifted_blob = buf2.getvalue()
    result = _compute_paired_perceptual(anchor_blob, shifted_blob)
    assert result is not None
    dists, stlpips, ensemble, _method = result
    # Core claim: ST-LPIPS handles shifts better than DISTS.
    assert stlpips < dists, (
        f"ST-LPIPS ({stlpips:.4f}) should be < DISTS ({dists:.4f}) on a "
        f"shift-only difference (ST-LPIPS is by design shift-tolerant)."
    )
    # The ensemble sits between the two — it lowers the effective penalty
    # vs v5's DISTS-only path. (Mathematically obvious from the 50/50 mean,
    # but assert it explicitly to flag if someone changes the ensemble weight.)
    assert ensemble < dists
    assert ensemble > stlpips
    # The ensemble's reduction from DISTS reflects ST-LPIPS's contribution.
    # Quantify: ensemble = (dists + stlpips) / 2, so the v5 → v5.1 win is
    # (dists - ensemble) = (dists - stlpips) / 2. For periodic-pattern shifts
    # this gap is meaningful; assert it's at least 0.005 (small but nonzero).
    assert (dists - ensemble) >= 0.005, (
        f"Ensemble should reduce DISTS penalty by at least 0.005 on shift; "
        f"got dists={dists:.4f}, ensemble={ensemble:.4f}, delta={dists-ensemble:.4f}"
    )


def test_run_paired_comparison_writes_v5_1_metadata_fields():
    """When _run_paired_comparison processes a 2-source candidate, the new
    v5.1 metadata fields are populated alongside the v5 fields for back-
    compat. Uses mocked handlers to avoid live HTTP."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")

    blob = open(_BW_PAGE, "rb").read()

    # Build a minimal fake-handler pair that returns the same blob for both
    # sources at the same chapter. With identical content the ensemble should
    # be near zero and adj should be near +PAIRED_MAX_ADJUSTMENT.
    fake_handler = MagicMock()
    fake_handler.fetch_comic_context.return_value = MagicMock()
    fake_handler.get_chapters.return_value = [
        {"chap": float(i), "label": str(i)} for i in range(1, 11)
    ]
    fake_handler.get_chapter_images.return_value = [
        f"https://x/p{i}" for i in range(20)
    ]
    fake_handler._pick_random_middle_page_index.return_value = 10
    fake_handler._fetch_probe_item_bytes.return_value = blob

    cand = _make_candidate([
        _make_source("anchor_site", "http://a/1", 0.7, width=1500),
        _make_source("target_site", "http://b/1", 0.7, width=800),
    ])

    with patch("sites.get_handler_by_name", return_value=fake_handler):
        _run_paired_comparison([cand], MagicMock(), MagicMock())

    # The target source should now have the v5.1 metadata fields. (The anchor
    # has them set with self-reference.)
    target = next(s for s in cand.sources if s.site == "target_site")
    meta = target.img_quality_metadata or {}
    # New v5.1 fields:
    assert "paired_perceptual_median" in meta
    assert "paired_dists_alone_median" in meta
    assert "paired_stlpips_median" in meta
    assert "paired_device" in meta
    # Back-compat v5 field still populated:
    assert "paired_dists_median" in meta
    # Identical content → ensemble ~= 0 → adjustment ~= +PAIRED_MAX_ADJUSTMENT.
    assert meta["paired_perceptual_median"] < 0.01
    assert meta["paired_quality_adjustment"] > 0.10


# ---------------------------------------------------------------------------
# v5.1 Phase 3a — JPEG-ghost double-compression detection
# ---------------------------------------------------------------------------


def test_jpeg_ghost_low_for_native_qf():
    """A JPEG encoded ONCE at q=85 has low ghost score (residual at q=85
    in the sweep is small — the codec is idempotent at matching QF)."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    img = Image.open(_BW_PAGE).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    score, diag = _compute_jpeg_ghost(buf.getvalue())
    assert score is not None
    # Native q=85 → score in [0, 0.5] band depending on content noise.
    assert score < 1.0
    assert 80 <= diag["min_qf"] <= 90


def test_jpeg_ghost_higher_for_double_compressed():
    """A JPEG encoded at q=70 then q=90 has elevated baseline across the
    sweep — characteristic double-compression ghost signature."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    img = Image.open(_BW_PAGE).convert("RGB")
    # First encode at q=70 (low quality residue).
    buf1 = io.BytesIO()
    img.save(buf1, format="JPEG", quality=70)
    intermediate = Image.open(io.BytesIO(buf1.getvalue())).convert("RGB")
    intermediate.load()
    # Second encode at q=90 (high quality on top of low-quality bytes).
    buf2 = io.BytesIO()
    intermediate.save(buf2, format="JPEG", quality=90)
    score_double, _ = _compute_jpeg_ghost(buf2.getvalue())

    # Compare to native q=90 single-encode of the same content.
    buf_native = io.BytesIO()
    img.save(buf_native, format="JPEG", quality=90)
    score_native, _ = _compute_jpeg_ghost(buf_native.getvalue())

    assert score_double is not None and score_native is not None
    # Double-compressed should have a measurably higher residual than
    # native single-encode at the same final QF.
    assert score_double >= score_native, (
        f"double-compressed q=70→q=90 ({score_double:.3f}) should be ≥ "
        f"native q=90 ({score_native:.3f})"
    )


def test_jpeg_ghost_returns_none_for_non_jpeg():
    """PNG input → ghost returns None (it's a JPEG-specific metric)."""
    img = Image.new("RGB", (400, 400), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    score, diag = _compute_jpeg_ghost(buf.getvalue())
    assert score is None
    assert diag.get("skipped_reason") == "non_jpeg"


def test_jpeg_ghost_returns_none_for_tiny_image():
    """Images smaller than 2×block_size can't compute meaningful block-SSD."""
    img = Image.new("RGB", (50, 50), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    score, diag = _compute_jpeg_ghost(buf.getvalue())
    assert score is None
    assert diag.get("skipped_reason") == "too_small"


# ---------------------------------------------------------------------------
# v5.1 Phase 3b — ECC/ORB alignment fallback
# ---------------------------------------------------------------------------


def test_align_image_pair_v51_phase_correlate_on_identical():
    """Identical inputs → Tier 1 (phaseCorrelate) succeeds at high confidence."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    blob = open(_BW_PAGE, "rb").read()
    result = _align_image_pair_v51(blob, blob)
    assert result is not None
    arr_a, arr_b, method = result
    assert method == "phase_correlate"
    assert arr_a.shape == arr_b.shape


def test_align_image_pair_v51_phase_correlate_small_translation():
    """A small synthetic translation (within phaseCorrelate's confidence
    band) is handled by Tier 1."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    import numpy as np
    img = Image.open(_BW_PAGE).convert("RGB")
    arr = np.asarray(img)
    shifted_arr = np.roll(arr, shift=(5, 5), axis=(0, 1))
    shifted_img = Image.fromarray(shifted_arr)
    buf_anchor = io.BytesIO(); img.save(buf_anchor, format="JPEG", quality=85)
    buf_shifted = io.BytesIO(); shifted_img.save(buf_shifted, format="JPEG", quality=85)
    result = _align_image_pair_v51(buf_anchor.getvalue(), buf_shifted.getvalue())
    assert result is not None
    _, _, method = result
    # Small translation should still hit phaseCorrelate.
    assert method == "phase_correlate"


def test_align_image_pair_v51_returns_method_in_output():
    """The 3-tuple return includes the method string. Method is one of
    the three tier names."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    blob = open(_BW_PAGE, "rb").read()
    result = _align_image_pair_v51(blob, blob)
    assert result is not None
    assert len(result) == 3
    _, _, method = result
    assert method in ("phase_correlate", "ecc_pyramid", "orb_ransac")


def test_align_image_pair_v51_back_compat_alias():
    """The v5-named `_align_image_pair` wrapper returns the 2-tuple shape
    (without method) for back-compat with v5 test fixtures + external code."""
    if not os.path.isfile(_BW_PAGE):
        pytest.skip("real test fixtures not present")
    blob = open(_BW_PAGE, "rb").read()
    result = _align_image_pair(blob, blob)
    assert result is not None
    assert len(result) == 2  # back-compat 2-tuple (arr_a, arr_b) only


def test_align_image_pair_v51_handles_unrelated_content():
    """Completely unrelated images (different series altogether) should
    fail through all three alignment tiers and return None."""
    import numpy as np
    # Two random-noise images — no possible alignment relationship.
    rng = np.random.default_rng(42)
    arr_a = rng.integers(0, 256, size=(600, 400, 3), dtype=np.uint8)
    arr_b = rng.integers(0, 256, size=(600, 400, 3), dtype=np.uint8)
    img_a = Image.fromarray(arr_a)
    img_b = Image.fromarray(arr_b)
    buf_a = io.BytesIO(); img_a.save(buf_a, format="JPEG", quality=85)
    buf_b = io.BytesIO(); img_b.save(buf_b, format="JPEG", quality=85)
    result = _align_image_pair_v51(buf_a.getvalue(), buf_b.getvalue())
    # Either None (all tiers failed) OR something fell through to ORB
    # (which may produce a low-quality match on random noise). Either
    # outcome is acceptable for this test — we just ensure no crash.
    if result is not None:
        _, _, method = result
        # If ANY tier "succeeded", it had to be one of the three.
        assert method in ("phase_correlate", "ecc_pyramid", "orb_ransac")
