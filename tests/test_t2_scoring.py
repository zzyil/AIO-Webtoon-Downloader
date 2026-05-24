"""Tests for Tier-2 deep-model NR-IQA integration
(sites/search_orchestrator.py v5).

T2 is gated on pyiqa being importable; tests skip when it's not. The
canonical T2 model is NIQE (Mittal 2013 opinion-unaware IQA, 8KB weights,
~200ms per inference on CPU). ARNIQA is opt-in via AIO_T2_ARNIQA=1.
CLIP-IQA is currently disabled due to an upstream pkg_resources.packaging
import bug in the clip-by-openai package — once that's fixed (or once we
migrate to torchmetrics' CLIPImageQualityAssessment), enable per the
docstring in _compute_t2_score.

Cross-file: targets sites/search_orchestrator.py:_compute_t2_score and
_get_niqe_model. Plan reference: ~/.claude/plans/how-robust-is-the-memoized-koala.md.
"""

from __future__ import annotations

import io
import os

import pytest

# Skip whole module when pyiqa isn't available.
pyiqa = pytest.importorskip("pyiqa", reason="T2 deep models require pyiqa")

from PIL import Image, ImageFilter

from sites.search_orchestrator import (
    _compute_t2_score,
    _get_niqe_model,
    _niqe_to_norm,
    _score_image_blob,
    warmup_t2_models,
    _PYIQA_AVAILABLE,
    _T2_READY,
)


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_DIR = os.path.join(_REPO_ROOT, "tmp_nzmj")
_BW_PAGE = os.path.join(_TMP_DIR, "ch_5", "5_0021.jpg")

_NEED_REAL = pytest.mark.skipif(
    not os.path.isfile(_BW_PAGE),
    reason="real test fixtures not present (dev-only)",
)


@pytest.fixture(scope="module", autouse=True)
def _warmup_t2():
    """Ensure T2 models are loaded before any test runs (otherwise the
    _T2_READY event isn't set and _compute_t2_score returns None)."""
    warmup_t2_models(background=False)
    yield


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def test_niqe_to_norm_in_range():
    """Normalized NIQE should be in [0, 1] across the typical raw range."""
    for raw in [0, 3, 4, 5, 6, 7, 8, 9, 10, 15, 50]:
        v = _niqe_to_norm(raw)
        assert 0.0 <= v <= 1.0


def test_niqe_to_norm_monotonic():
    """Higher raw NIQE (=worse quality) → higher norm value."""
    samples = [_niqe_to_norm(r) for r in [3, 4, 5, 6, 7, 8, 9, 10]]
    assert samples == sorted(samples)


def test_niqe_to_norm_handles_garbage():
    """Non-numeric input returns neutral 0.5 (defensive against pyiqa
    occasionally emitting NaN/Inf or string artifacts)."""
    assert _niqe_to_norm(float("nan")) >= 0.0  # clamped but defined
    assert _niqe_to_norm("not a number") == 0.5
    assert _niqe_to_norm(None) == 0.5


# ---------------------------------------------------------------------------
# Lazy-init contract
# ---------------------------------------------------------------------------

def test_get_niqe_model_returns_callable():
    """The lazy-init helper returns a callable model when pyiqa is available."""
    model = _get_niqe_model()
    assert model is not None
    assert callable(model)


def test_get_niqe_model_cached():
    """Repeated calls return the SAME model instance (idempotent init)."""
    m1 = _get_niqe_model()
    m2 = _get_niqe_model()
    assert m1 is m2


# ---------------------------------------------------------------------------
# T2 inference on real manga pages
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_compute_t2_score_real_bw_manga():
    """NIQE-only T2 on a real B&W manga page should return a meaningful
    score; t2_available=True; raw NIQE in the typical 5-10 band."""
    img = Image.open(_BW_PAGE)
    img.load()
    blob = open(_BW_PAGE, "rb").read()
    t2, meta = _compute_t2_score(blob, img, is_grayscale=True)
    assert t2 is not None
    assert meta["t2_available"] is True
    assert meta["niqe_score"] is not None
    # Empirically observed: real MangaFire B&W pages land NIQE ~6-8.
    assert 4 < meta["niqe_score"] < 12, (
        f"NIQE on real MangaFire B&W page = {meta['niqe_score']}, "
        f"outside expected 4-12 band"
    )
    # When ARNIQA is opt-in but not enabled (default), arniqa_score stays None.
    assert meta.get("arniqa_score") is None or meta.get("arniqa_score") is None


@_NEED_REAL
def test_compute_t2_score_blurred_lower():
    """A blurred page should produce a higher raw NIQE (= worse quality) and
    therefore a lower t2_composite than the sharp original. This is the
    canonical "T2 catches degradation T1 might miss" case."""
    img = Image.open(_BW_PAGE)
    img.load()
    blob = open(_BW_PAGE, "rb").read()
    t2_sharp, m_sharp = _compute_t2_score(blob, img, is_grayscale=True)

    blurred = img.filter(ImageFilter.GaussianBlur(radius=3))
    buf = io.BytesIO()
    blurred.save(buf, format="JPEG", quality=85)
    blob_blur = buf.getvalue()
    blurred_pil = Image.open(io.BytesIO(blob_blur))
    blurred_pil.load()
    t2_blur, m_blur = _compute_t2_score(blob_blur, blurred_pil, is_grayscale=True)

    assert t2_sharp > t2_blur, (
        f"sharp t2 ({t2_sharp}) should exceed blurred t2 ({t2_blur}); "
        f"niqe sharp={m_sharp['niqe_score']} blurred={m_blur['niqe_score']}"
    )


# ---------------------------------------------------------------------------
# Composite — T1 + T2 blend
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_score_image_blob_renormalized_composite_in_range():
    """The renormalized composite (0.90*T1 + 0.10*T2) must stay in [0, 1]
    across multiple test images."""
    for fname in ["1_0001.jpg", "1_0004.jpg", "5_0021.jpg"]:
        path = os.path.join(_TMP_DIR, "ch_1" if fname.startswith("1") else "ch_5", fname)
        if not os.path.isfile(path):
            continue
        result = _score_image_blob(open(path, "rb").read())
        assert result is not None
        score, _ = result
        assert 0.0 <= score <= 1.0


@_NEED_REAL
def test_score_image_blob_exposes_t1_pre_t2_for_audit():
    """When T2 fires, the metadata records composite_pre_t2 = original T1
    score so calibration / UI can show 'why this score?'."""
    result = _score_image_blob(open(_BW_PAGE, "rb").read())
    assert result is not None
    _, meta = result
    if meta.get("t2_available"):
        assert meta["composite_pre_t2"] is not None
        assert 0.0 <= meta["composite_pre_t2"] <= 1.0


@_NEED_REAL
def test_t2_metadata_includes_niqe_fields():
    """The v5 metadata schema requires niqe_score, niqe_norm,
    t2_score, t2_available fields populated when T2 ran."""
    result = _score_image_blob(open(_BW_PAGE, "rb").read())
    assert result is not None
    _, meta = result
    if meta.get("t2_available"):
        for f in ("niqe_score", "niqe_norm", "t2_score"):
            assert meta.get(f) is not None, f"T2 ran but {f} missing"


# ---------------------------------------------------------------------------
# Graceful degrade — pyiqa missing / T2_READY not set
# ---------------------------------------------------------------------------

def test_compute_t2_skipped_when_t2_ready_not_set(monkeypatch):
    """When _T2_READY isn't set (e.g., warmup daemon still running),
    _compute_t2_score returns (None, t2_available=False) immediately so
    workers don't serialize behind the weight download."""
    from sites import search_orchestrator
    monkeypatch.setattr(search_orchestrator._T2_READY, "is_set", lambda: False)
    img = Image.open(_BW_PAGE) if os.path.isfile(_BW_PAGE) else Image.new("RGB", (200, 200))
    img.load()
    t2, meta = _compute_t2_score(b"x" * 1024, img, is_grayscale=True)
    assert t2 is None
    assert meta["t2_available"] is False


def test_compute_t2_skipped_when_pyiqa_unavailable(monkeypatch):
    """When _PYIQA_AVAILABLE is False (import failed at module load),
    _compute_t2_score returns (None, t2_available=False)."""
    from sites import search_orchestrator
    monkeypatch.setattr(search_orchestrator, "_PYIQA_AVAILABLE", False)
    img = Image.open(_BW_PAGE) if os.path.isfile(_BW_PAGE) else Image.new("RGB", (200, 200))
    img.load()
    t2, meta = _compute_t2_score(b"x" * 1024, img, is_grayscale=True)
    assert t2 is None
    assert meta["t2_available"] is False


# ---------------------------------------------------------------------------
# Thread safety — concurrent inference
# ---------------------------------------------------------------------------

@_NEED_REAL
def test_t2_inference_thread_safe():
    """Fire 8 concurrent _compute_t2_score calls; all should complete without
    error and return finite scores. The per-model inference lock serializes
    PyTorch nn.Module forward (which isn't thread-safe on a shared model)
    but the call shouldn't deadlock or crash."""
    import concurrent.futures
    img = Image.open(_BW_PAGE)
    img.load()
    blob = open(_BW_PAGE, "rb").read()

    def _run():
        return _compute_t2_score(blob, img, is_grayscale=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_run) for _ in range(8)]
        results = [f.result(timeout=30) for f in futures]

    for t2, meta in results:
        assert t2 is not None
        assert meta["t2_available"] is True
        assert 0.0 <= t2 <= 1.0


# ---------------------------------------------------------------------------
# Warmup contract
# ---------------------------------------------------------------------------

def test_warmup_sets_t2_ready():
    """warmup_t2_models(background=False) must set the _T2_READY event
    before returning."""
    # The autouse fixture already ran warmup; verify the event is set.
    from sites.search_orchestrator import _T2_READY as event
    assert event.is_set()


def test_warmup_idempotent():
    """Calling warmup_t2_models twice should be a no-op the second time
    (event already set; the function short-circuits)."""
    warmup_t2_models(background=False)
    warmup_t2_models(background=False)  # should not raise / re-download
    from sites.search_orchestrator import _T2_READY as event
    assert event.is_set()
