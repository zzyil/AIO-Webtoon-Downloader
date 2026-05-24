"""B&W-manga-specific quality signal primitives.

Owns: four scoring functions consumed by `sites/search_orchestrator.py`'s
T1 layer when content_type ∈ {bw_manga, bw_manga_with_color_inserts}.

  - compute_screentone_integrity(img, content_type)
        FFT peak ratio in the mid-frequency band of midtone-masked patches.
        Measures whether the regular dot/line patterns that define B&W
        manga's grayscale fills have survived recompression / upscaler
        damage. Highest-leverage new signal per the research (Xie CVPR
        2021, Yao 2023). Pure NumPy.

  - compute_line_quality(img)
        Canny-edge-conditioned sharpness minus the variance in a
        4-pixel-wide annulus just outside each edge. The annulus variance
        is what JPEG ringing produces on high-contrast strokes (Feng-
        Allebach SPIE 2006). cv2.Canny used when available; numpy Sobel
        fallback.

  - compute_upscaler_score(img)
        AI-upscaler fingerprint detector. Two signals: half-Nyquist
        radial-spectrum bump (Wang CVPR 2020 / Tan CVPR 2024) and
        asymmetric edge overshoot (positive on one side only, the
        signature of Real-ESRGAN-anime / Waifu2x / Real-CUGAN). Used as
        a *penalty* in T1, not a positive component.

  - compute_bw_storage_signals(img, blob, fmt)
        Cheap categorical + uniformity signals: bilevel detection
        (PIL mode='1' OR histogram bimodality), chroma subsampling
        (laundering signature when 4:2:0 is forced on B&W content),
        background MAD uniformity, gutter shadow score, speckle density.

All four are pure NumPy/SciPy; cv2 is optional (gated). No PyTorch on
this code path — T1 must work even when torch isn't installed.

Cross-file: imported by `sites/search_orchestrator.py`'s
`_compute_t1_score_bw` (added Phase 3). Tests live in
`tests/test_bw_signals.py`. Weight tables and penalty constants are in
`sites/t1_constants.py`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# cv2 is optional. When unavailable, line-quality and upscaler-asymmetry
# signals fall back to numpy Sobel + threshold (the FFT-based signals
# don't need cv2 at all). Mirrors the gating pattern in
# sites/search_orchestrator.py:_CV2_AVAILABLE.
_CV2_AVAILABLE = False
try:
    import cv2 as _cv2
    _CV2_AVAILABLE = True
except Exception:
    _cv2 = None  # type: ignore


# --- 1. Screentone integrity -----------------------------------------------

# Patch size for FFT analysis. 256 gives N/2 = 128 frequency bins — enough
# resolution for the screen-frequency band [0.10, 0.50] (12-64 bins) without
# making the FFT cost dominant. Stride 128 means a 1500×2000 page produces
# ~70 patch candidates before midtone filtering — plenty of headroom to find
# 9 good ones.
SCREENTONE_PATCH_SIZE = 256
SCREENTONE_PATCH_STRIDE = 128

# Midtone luminance band. Screentones live in gray-fill regions, which sit
# between pure-black lineart (<40) and paper (>220). 60-200 is conservative
# (excludes the black-ink strokes and the bright paper) so the FFT inside a
# patch actually sees the dot pattern rather than the ink/paper boundaries.
SCREENTONE_MIDTONE_LO = 60
SCREENTONE_MIDTONE_HI = 200

# Minimum fraction of a patch that must fall inside the midtone band for
# the patch to qualify for FFT analysis. Patches that fail this gate are
# almost entirely paper or almost entirely ink — no screentone signal
# available.
SCREENTONE_PATCH_MIDTONE_MIN_COVERAGE = 0.30

# Max patches actually used per page. 9 patches × 256×256 FFT ≈ 30 ms on
# CPU. More patches → more stable median but diminishing returns past 9.
SCREENTONE_MAX_PATCHES = 9

# Radial-spectrum frequency band where screentone peaks live, expressed as
# fractions of the patch Nyquist (N/2 for an N×N patch). The lower bound
# 0.10 excludes the FFT DC mound; 0.50 stops short of the highest
# frequencies where genuine noise dominates and aliased screentones get
# masked. Covers the typical screentone range from coarse (60 lpi at 300
# DPI scan) to fine (85 lpi at 600 DPI scan).
SCREENTONE_BAND_LO_FRAC = 0.10
SCREENTONE_BAND_HI_FRAC = 0.50

# Band-energy fraction calibration. The signal we want is "what fraction
# of the (non-DC, non-block-harmonic) spectrum lives in the screen-
# frequency band?"
#
# Why fraction-of-energy rather than peak-to-median ratio: a low-pass
# operation like Waifu2x noise removal collapses both the screentone peak
# AND the surrounding noise floor, but the noise floor often drops MORE
# than the peak (the regular pattern has more inherent energy than the
# random noise around it). Peak-to-median actually INCREASES under blur
# — the wrong direction for a quality signal. Absolute energy in the
# band drops monotonically under blur, JPEG damage, AND Waifu2x denoising
# — the right direction in all three cases.
#
# Clean manga screentones: ~30-50% of non-DC, non-block energy lives in
# the screen-frequency band. Heavily-blurred / Waifu2x output: <10%.
# Threshold band: 0.10 (floor) to 0.45 (pristine).
SCREENTONE_FRAC_LO = 0.10   # below this = no preserved screentone
SCREENTONE_FRAC_HI = 0.45   # at-or-above this = pristine screentone

# JPEG-block notch. JPEG quantization introduces a peak at the 8×8 block
# frequency, which falls inside our radial band at r ≈ N/8 (= 32 for our
# 256×256 patch). Block-frequency energy is NOT screentone preservation
# — it's damage. Exclude a ±2-bin window around r=N/8 from BOTH the band
# numerator and the total denominator so JPEG damage doesn't falsely
# inflate the ratio.
SCREENTONE_NOTCH_BLOCK_FREQ = True
SCREENTONE_NOTCH_RADIUS = 2  # ±radius bins excluded around block frequency


def compute_screentone_integrity(
    img: Any,
    content_type: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """FFT peak-to-median ratio over the mid-frequency band of midtone
    patches. Returns (score, metadata).

    Returns score in [0, 1]: 1.0 = pristine screentone preservation,
    0.0 = no detectable screen pattern (JPEG-smeared or Waifu2x-denoised).
    Returns 0.5 (neutral) when the image has insufficient midtone area
    to evaluate (e.g., all-line-art splash page, mostly-paper title page).

    `content_type` is currently unused (the same algorithm applies to
    bw_manga / bw_manga_with_color_inserts / unknown). Accepted for future
    per-content-type band tuning.

    Cross-file: called from search_orchestrator._compute_t1_score_bw (T1
    layer) and from search_orchestrator._run_pairwise_ranking (T3 layer)
    on shared chapter pages.
    """
    import numpy as np
    meta: Dict[str, Any] = {
        "score": None,
        "patches_used": 0,
        "patches_qualified": 0,
        "band_energy_fraction": None,
        "reason": None,
    }
    try:
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception:
        meta["reason"] = "decode_failure"
        return 0.5, meta

    h, w = gray.shape
    if h < SCREENTONE_PATCH_SIZE or w < SCREENTONE_PATCH_SIZE:
        meta["reason"] = "too_small"
        return 0.5, meta

    # Enumerate candidate patches deterministically (left-to-right,
    # top-to-bottom). Take the first SCREENTONE_MAX_PATCHES that pass the
    # midtone-coverage gate. Determinism matters for cache stability —
    # the same image always produces the same score.
    qualified_patches = []
    for y in range(0, h - SCREENTONE_PATCH_SIZE + 1, SCREENTONE_PATCH_STRIDE):
        for x in range(0, w - SCREENTONE_PATCH_SIZE + 1, SCREENTONE_PATCH_STRIDE):
            patch = gray[y:y + SCREENTONE_PATCH_SIZE, x:x + SCREENTONE_PATCH_SIZE]
            midtone_mask = (patch >= SCREENTONE_MIDTONE_LO) & (patch <= SCREENTONE_MIDTONE_HI)
            coverage = float(midtone_mask.mean())
            if coverage >= SCREENTONE_PATCH_MIDTONE_MIN_COVERAGE:
                qualified_patches.append((patch, coverage))
                if len(qualified_patches) >= SCREENTONE_MAX_PATCHES:
                    break
        if len(qualified_patches) >= SCREENTONE_MAX_PATCHES:
            break

    meta["patches_qualified"] = len(qualified_patches)
    if len(qualified_patches) < 2:
        meta["reason"] = "insufficient_midtone_area"
        return 0.5, meta

    # FFT + radial profile + band-energy fraction per patch.
    fractions = []
    n_half = SCREENTONE_PATCH_SIZE // 2
    band_lo = int(SCREENTONE_BAND_LO_FRAC * n_half)
    band_hi = int(SCREENTONE_BAND_HI_FRAC * n_half)
    block_r = SCREENTONE_PATCH_SIZE // 8  # = 32 for 256×256
    for patch, _cov in qualified_patches:
        # Mean-subtract before FFT to suppress DC contamination. Cast to
        # float32 — int16 input to fft2 would silently upcast anyway.
        patch_f = patch.astype(np.float32)
        patch_f -= patch_f.mean()
        spectrum = np.abs(np.fft.fftshift(np.fft.fft2(patch_f)))
        cy, cx = n_half, n_half
        y_idx, x_idx = np.indices(spectrum.shape)
        r = np.sqrt((y_idx - cy) ** 2 + (x_idx - cx) ** 2).astype(np.int32)
        max_r = min(n_half, r.max())
        bin_count = max_r + 1
        bin_sum = np.bincount(r.ravel(), weights=spectrum.ravel(), minlength=bin_count)
        bin_n = np.bincount(r.ravel(), minlength=bin_count)
        # Per-radius mean energy density. Using sum/n (not raw sum) so the
        # different number of pixels at each radius doesn't bias toward
        # higher-r bins (which have more pixels in a square FFT grid).
        radial = np.where(bin_n > 0, bin_sum / np.maximum(bin_n, 1), 0.0)

        # Build the JPEG-block exclusion mask once: indices r ∈
        # [block_r ± SCREENTONE_NOTCH_RADIUS] are NEITHER band-numerator
        # nor total-denominator. They represent JPEG damage signal that
        # would inflate both terms.
        if SCREENTONE_NOTCH_BLOCK_FREQ:
            notch_lo = max(0, block_r - SCREENTONE_NOTCH_RADIUS)
            notch_hi = min(len(radial), block_r + SCREENTONE_NOTCH_RADIUS + 1)
        else:
            notch_lo = notch_hi = -1

        # Band numerator: energy in screen-frequency band, excluding the
        # block-frequency notch.
        band_energy = 0.0
        total_energy = 0.0
        # Skip the very-low frequencies (r ∈ [0, 2]) — DC mound bleed
        # would dominate the denominator and shrink the fraction
        # regardless of screentone state.
        DC_SKIP = 2
        for r_bin in range(DC_SKIP, min(n_half + 1, len(radial))):
            if notch_lo <= r_bin < notch_hi:
                continue  # excluded
            val = float(radial[r_bin])
            total_energy += val
            if band_lo <= r_bin <= band_hi:
                band_energy += val

        if total_energy <= 1e-6:
            # All-zero spectrum — degenerate patch (uniform color region
            # that snuck past the midtone-mask filter). Skip.
            continue

        fractions.append(band_energy / total_energy)

    if not fractions:
        meta["reason"] = "fft_all_degenerate"
        return 0.5, meta

    fraction_median = float(np.median(fractions))
    score = float(
        np.clip(
            (fraction_median - SCREENTONE_FRAC_LO)
            / max(SCREENTONE_FRAC_HI - SCREENTONE_FRAC_LO, 1e-6),
            0.0,
            1.0,
        )
    )
    meta["score"] = round(score, 4)
    meta["patches_used"] = len(fractions)
    meta["band_energy_fraction"] = round(fraction_median, 4)
    return score, meta


# --- 2. Line quality (sharpness − ringing) ---------------------------------

# Canny hysteresis thresholds (same as USM detector for B&W content in
# search_orchestrator._compute_usm_overshoot). Stronger thresholds reduce
# false positives from screentone dot edges — we want to measure ringing
# at GENUINE line-art strokes, not at every speck of halftone.
LINE_QUALITY_CANNY_LO = 50
LINE_QUALITY_CANNY_HI = 150

# Annulus width around each edge. 1-px inner band excluded (the gradient
# itself); 4-px outer band measured (where JPEG ringing oscillations live
# in 1500-2000 px pages).
LINE_QUALITY_EDGE_EXCLUDE = 1
LINE_QUALITY_EDGE_BAND = 4

# Calibration constants. Edge sharpness (mean Sobel magnitude at edge
# pixels) for clean B&W manga sits 30-80; ringing excess std (annulus std
# minus background std) sits 0-3 clean, 6-15 on heavily-compressed JPEGs.
LINE_QUALITY_SHARPNESS_NORM = 60.0
LINE_QUALITY_RINGING_NORM = 12.0
LINE_QUALITY_MIN_EDGES = 200  # below this, image is too uniform to measure


def _canny_or_sobel_edge_mask(gray):
    """Return a uint8 edge mask. Uses cv2.Canny when available; falls
    back to a Sobel-magnitude threshold otherwise.

    The Sobel fallback is intentionally less selective (more edges
    detected) so coverage stays reasonable, but the downstream stats
    are robust to mild over-detection.
    """
    import numpy as np
    if _CV2_AVAILABLE:
        return _cv2.Canny(gray, LINE_QUALITY_CANNY_LO, LINE_QUALITY_CANNY_HI)
    # Numpy Sobel via shift-and-subtract; same kernel weights as cv2.Sobel
    # ksize=3. Threshold at the 90th percentile of gradient magnitude so
    # we get ~10% of pixels marked as edges (matches Canny coverage on
    # typical manga line-art).
    arr = gray.astype(np.float32)
    h, w = arr.shape
    if h < 3 or w < 3:
        return np.zeros_like(gray)
    sx = np.zeros_like(arr)
    sy = np.zeros_like(arr)
    sx[1:-1, 1:-1] = (
        -arr[:-2, :-2] + arr[:-2, 2:]
        - 2.0 * arr[1:-1, :-2] + 2.0 * arr[1:-1, 2:]
        - arr[2:, :-2] + arr[2:, 2:]
    )
    sy[1:-1, 1:-1] = (
        -arr[:-2, :-2] - 2.0 * arr[:-2, 1:-1] - arr[:-2, 2:]
        + arr[2:, :-2] + 2.0 * arr[2:, 1:-1] + arr[2:, 2:]
    )
    mag = np.sqrt(sx * sx + sy * sy)
    thresh = float(np.percentile(mag, 90))
    return (mag > thresh).astype(np.uint8) * 255


def _dilate_mask(mask, radius: int):
    """Binary dilation by `radius` pixels. cv2 when available; numpy
    rolling-max otherwise.
    """
    import numpy as np
    if _CV2_AVAILABLE:
        k = 2 * radius + 1
        kernel = _cv2.getStructuringElement(_cv2.MORPH_RECT, (k, k))
        return _cv2.dilate(mask, kernel)
    # Numpy fallback: iterated max-of-3-neighbors `radius` times.
    out = (mask > 0).astype(np.uint8)
    for _ in range(radius):
        rolled = out.copy()
        rolled[1:, :] = np.maximum(rolled[1:, :], out[:-1, :])
        rolled[:-1, :] = np.maximum(rolled[:-1, :], out[1:, :])
        rolled[:, 1:] = np.maximum(rolled[:, 1:], out[:, :-1])
        rolled[:, :-1] = np.maximum(rolled[:, :-1], out[:, 1:])
        out = rolled
    return (out * 255).astype(np.uint8)


def compute_line_quality(img: Any) -> Tuple[float, Dict[str, Any]]:
    """Edge sharpness minus JPEG-ringing variance. Returns (score, meta).

    Pipeline:
      1. Grayscale + Canny → edge_mask (or Sobel-percentile fallback).
      2. Dilate edges by 1 px → exclude band (the gradient transition
         itself, where high variance is *signal* not ringing).
      3. Dilate edges by 4 px and XOR with the exclude band → ring band
         (the 3-pixel-wide annulus just outside each edge).
      4. ringing_excess = std(image[ring_band]) − std(image[background])
         (background = pixels in neither the edge nor the ring band).
      5. edge_sharpness = mean Sobel-magnitude at edge pixels.
      6. score = clamp(edge_sharpness / NORM) − 0.5 * clamp(ringing / NORM)

    Returns 0.5 (neutral) when fewer than LINE_QUALITY_MIN_EDGES are
    detected — meaning the page is too uniform (e.g., a splash page or
    a mostly-paper title page) to score meaningfully.

    Cross-file: called from _compute_t1_score_bw (T1 layer). The 0.5 *
    ringing weight asymmetry is deliberate — sharpness rewards real
    detail; ringing penalizes a specific damage mode and shouldn't
    dominate the signal.
    """
    import numpy as np
    meta: Dict[str, Any] = {
        "score": None,
        "edge_sharpness": None,
        "ringing_excess": None,
        "edge_pixels": 0,
        "reason": None,
    }
    try:
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception:
        meta["reason"] = "decode_failure"
        return 0.5, meta

    h, w = gray.shape
    if h < 64 or w < 64:
        meta["reason"] = "too_small"
        return 0.5, meta

    edges = _canny_or_sobel_edge_mask(gray)
    edge_count = int((edges > 0).sum())
    meta["edge_pixels"] = edge_count
    if edge_count < LINE_QUALITY_MIN_EDGES:
        meta["reason"] = "too_few_edges"
        return 0.5, meta

    # Build exclude and ring bands via successive dilations.
    exclude_band = _dilate_mask(edges, LINE_QUALITY_EDGE_EXCLUDE)
    extended_band = _dilate_mask(edges, LINE_QUALITY_EDGE_BAND + LINE_QUALITY_EDGE_EXCLUDE)
    ring_band = (extended_band > 0) & (exclude_band == 0)
    background = (extended_band == 0)

    # Defensive: if dilations consumed the whole image, no background
    # remains. Treat as too-uniform to measure.
    if not ring_band.any() or background.sum() < 1000:
        meta["reason"] = "no_background_baseline"
        return 0.5, meta

    ring_std = float(gray[ring_band].std())
    bg_std = float(gray[background].std())
    ringing_excess = max(0.0, ring_std - bg_std)

    # Sobel magnitude at edge pixels for sharpness component. Reuse the
    # same hand-rolled Sobel as _canny_or_sobel_edge_mask's fallback.
    arr = gray.astype(np.float32)
    sx = np.zeros_like(arr)
    sy = np.zeros_like(arr)
    sx[1:-1, 1:-1] = (
        -arr[:-2, :-2] + arr[:-2, 2:]
        - 2.0 * arr[1:-1, :-2] + 2.0 * arr[1:-1, 2:]
        - arr[2:, :-2] + arr[2:, 2:]
    )
    sy[1:-1, 1:-1] = (
        -arr[:-2, :-2] - 2.0 * arr[:-2, 1:-1] - arr[:-2, 2:]
        + arr[2:, :-2] + 2.0 * arr[2:, 1:-1] + arr[2:, 2:]
    )
    mag = np.sqrt(sx * sx + sy * sy)
    edge_mask_bool = edges > 0
    if edge_mask_bool.any():
        edge_sharpness = float(mag[edge_mask_bool].mean())
    else:
        edge_sharpness = 0.0

    sharpness_norm = min(1.0, edge_sharpness / LINE_QUALITY_SHARPNESS_NORM)
    ringing_norm = min(1.0, ringing_excess / LINE_QUALITY_RINGING_NORM)
    score = float(max(0.0, min(1.0, sharpness_norm - 0.5 * ringing_norm)))

    meta["score"] = round(score, 4)
    meta["edge_sharpness"] = round(edge_sharpness, 3)
    meta["ringing_excess"] = round(ringing_excess, 3)
    return score, meta


# --- 3. Upscaler fingerprint (penalty signal) ------------------------------

# Center-crop size for the FFT-based half-Nyquist bump detector. 1024 gives
# enough frequency resolution (N/4 = 256, N/8 = 128 — well-separated bins)
# without making FFT cost dominant.
UPSCALER_FFT_CROP = 1024

# Edge-asymmetry profile length. Sample 5 px each side of an edge — enough
# to capture the overshoot ring (1-2 px wide on typical JPEG, similar on
# Real-ESRGAN output).
UPSCALER_PROFILE_HALFLEN = 5
UPSCALER_MAX_EDGES_SAMPLED = 500
UPSCALER_HALFNYQUIST_NORM = 0.30  # ratio at which "obvious bump" saturates


def compute_upscaler_score(img: Any) -> Tuple[float, Dict[str, Any]]:
    """AI-upscaler fingerprint detection. Returns (score, meta) where
    score ∈ [0, 1] with higher = more likely upscaled.

    Two complementary signals:

      half_nyquist_bump: ratio E[radial spectrum at f=N/4] /
                              E[radial spectrum at f=N/8].
        Clean native scans show monotonically-decreasing radial spectra —
        ratio < 0.5. Real-ESRGAN / Waifu2x output shows a characteristic
        dip-then-rise pattern with ratio approaching 1.0 (Wang CVPR
        2020, Tan CVPR 2024).

      edge_asymmetry: |positive_overshoot − negative_overshoot| /
                       max(positive_overshoot, negative_overshoot).
        Real JPEG ringing is symmetric (overshoot rings on both sides
        of edges). AI upscalers produce one-sided overshoot — the
        ring exists only on the dark side of bright→dark edges.

    Combined: `score = 0.6 * half_nyquist_norm + 0.4 * asymmetry`.

    Used as a *penalty* in T1_BW: subtract `UPSCALER_PENALTY_MAX *
    score` from the composite. cv2 missing → asymmetry signal degrades
    to 0, half-Nyquist still computed.

    Cross-file: called from _compute_t1_score_bw.
    """
    import numpy as np
    meta: Dict[str, Any] = {
        "score": None,
        "halfnyquist_ratio": None,
        "edge_asymmetry": None,
        "edges_sampled": 0,
        "reason": None,
    }
    try:
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception:
        meta["reason"] = "decode_failure"
        return 0.0, meta

    h, w = gray.shape
    if h < 256 or w < 256:
        meta["reason"] = "too_small"
        return 0.0, meta

    # Center crop to UPSCALER_FFT_CROP. Smaller images get used at native
    # size (we just need a power-of-two-ish square; FFT works on any size).
    crop_size = min(UPSCALER_FFT_CROP, h, w)
    cy, cx = h // 2, w // 2
    half = crop_size // 2
    crop = gray[cy - half:cy - half + crop_size, cx - half:cx - half + crop_size]

    # 2D FFT magnitude, fftshift, radial profile via integer-radius bins.
    crop_f = crop.astype(np.float32) - crop.mean()
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(crop_f)))
    n = crop_size
    n_half = n // 2
    y_idx, x_idx = np.indices(spectrum.shape)
    r = np.sqrt((y_idx - n_half) ** 2 + (x_idx - n_half) ** 2).astype(np.int32)
    bin_sum = np.bincount(r.ravel(), weights=spectrum.ravel(), minlength=n_half + 1)
    bin_n = np.bincount(r.ravel(), minlength=n_half + 1)
    radial = np.where(bin_n > 0, bin_sum / np.maximum(bin_n, 1), 0.0)

    # Half-Nyquist bump: spectrum[N/4] / spectrum[N/8].
    r_eighth = n // 8
    r_quarter = n // 4
    if r_eighth < len(radial) and r_quarter < len(radial) and radial[r_eighth] > 1e-6:
        halfnyquist_ratio = float(radial[r_quarter] / radial[r_eighth])
    else:
        halfnyquist_ratio = 0.0
    # Map to [0, 1]: ratio 0.30 = "obvious bump" saturates at 1.0; below
    # 0.05 (monotonic decrease) = 0.0.
    halfnyquist_norm = float(np.clip(
        (halfnyquist_ratio - 0.05) / max(UPSCALER_HALFNYQUIST_NORM - 0.05, 1e-6),
        0.0, 1.0,
    ))
    meta["halfnyquist_ratio"] = round(halfnyquist_ratio, 4)

    # Edge-asymmetry signal. Skip entirely when cv2 unavailable.
    edge_asymmetry = 0.0
    if _CV2_AVAILABLE:
        edges = _cv2.Canny(gray, LINE_QUALITY_CANNY_LO, LINE_QUALITY_CANNY_HI)
        # Use the same gradient kernels as line_quality for the perp direction.
        sx = _cv2.Sobel(gray, _cv2.CV_32F, 1, 0, ksize=3)
        sy = _cv2.Sobel(gray, _cv2.CV_32F, 0, 1, ksize=3)
        ys, xs = np.where(edges > 0)
        if len(ys) > 0:
            # Deterministic stride sampling — same image always picks the
            # same edges. Avoids randomness so cache results are stable.
            if len(ys) > UPSCALER_MAX_EDGES_SAMPLED:
                step = len(ys) // UPSCALER_MAX_EDGES_SAMPLED
                ys = ys[::step][:UPSCALER_MAX_EDGES_SAMPLED]
                xs = xs[::step][:UPSCALER_MAX_EDGES_SAMPLED]
            edge_asymmetry, n_sampled = _measure_edge_asymmetry(gray, ys, xs, sx, sy)
            meta["edges_sampled"] = int(n_sampled)

    meta["edge_asymmetry"] = round(float(edge_asymmetry), 4)
    score = float(np.clip(0.6 * halfnyquist_norm + 0.4 * edge_asymmetry, 0.0, 1.0))
    meta["score"] = round(score, 4)
    return score, meta


def _measure_edge_asymmetry(gray, ys, xs, sx, sy) -> Tuple[float, int]:
    """Per-edge perpendicular-profile asymmetry. Returns (mean_asymmetry, n).

    For each (y, x) edge pixel:
      1. Compute gradient direction (atan2(sy, sx)).
      2. Sample (2 * HALFLEN + 1) intensities along the perpendicular line
         centered on (x, y).
      3. positive_overshoot = max(profile[HALFLEN+1:]) - profile[HALFLEN]
      4. negative_overshoot = profile[HALFLEN] - min(profile[:HALFLEN])
      5. asymmetry = |pos - neg| / max(pos, neg, 1e-6)

    Real JPEG ringing has |pos - neg| ≈ 0 (symmetric oscillation).
    Real-ESRGAN-anime / Waifu2x show |pos - neg| / max ≈ 0.3-0.6
    (one-sided overshoot).
    """
    import math
    import numpy as np
    h, w = gray.shape
    L = UPSCALER_PROFILE_HALFLEN
    asymmetries = []
    for py, px in zip(ys, xs):
        # Gradient angle at this pixel; perpendicular = gradient direction
        # itself (we sample along the gradient to cross the edge).
        gx = float(sx[py, px])
        gy = float(sy[py, px])
        norm = math.hypot(gx, gy)
        if norm < 1e-6:
            continue
        dx = gx / norm
        dy = gy / norm
        # Sample profile along ±L pixels.
        profile = []
        out_of_bounds = False
        for offset in range(-L, L + 1):
            sx_pos = int(round(px + offset * dx))
            sy_pos = int(round(py + offset * dy))
            if sx_pos < 0 or sy_pos < 0 or sx_pos >= w or sy_pos >= h:
                out_of_bounds = True
                break
            profile.append(int(gray[sy_pos, sx_pos]))
        if out_of_bounds or len(profile) != 2 * L + 1:
            continue
        profile_arr = np.asarray(profile, dtype=np.float32)
        center_val = float(profile_arr[L])
        pos_max = float(profile_arr[L + 1:].max())
        neg_min = float(profile_arr[:L].min())
        # Orient so positive_overshoot = excursion past the brighter end.
        if center_val > (pos_max + neg_min) / 2:
            # Edge sample sits on the bright side; flip
            pos_overshoot = max(0.0, center_val - neg_min)
            neg_overshoot = max(0.0, pos_max - center_val)
        else:
            pos_overshoot = max(0.0, pos_max - center_val)
            neg_overshoot = max(0.0, center_val - neg_min)
        denom = max(pos_overshoot, neg_overshoot, 1e-6)
        asym = abs(pos_overshoot - neg_overshoot) / denom
        asymmetries.append(asym)
    if not asymmetries:
        return 0.0, 0
    return float(sum(asymmetries) / len(asymmetries)), len(asymmetries)


# --- 4. Storage / encoding signals -----------------------------------------

# Below this fraction of pixels in the [0, 16] ∪ [240, 255] tails, the image
# is NOT bilevel — it has true mid-tone content. Above this, it's effectively
# bitonal whether or not PIL stores it as mode='1'.
BILEVEL_TAIL_FRACTION_THRESHOLD = 0.92

# Background-uniformity calibration. MAD on the paper class (Otsu high mode)
# for clean scans sits 0-2; noisy / yellowed paper hits 5-12. Normalize to
# [0, 1] via 1 - clamp(MAD / 8, 0, 1).
BG_UNIFORMITY_MAD_NORM = 8.0

# Gutter-shadow detection band. Compare the leftmost vs rightmost STRIPE_WIDTH
# pixel-columns' mean luminance. If one side is consistently darker (gutter
# shadow next to the spine), report a non-zero score.
GUTTER_STRIPE_WIDTH = 100
GUTTER_LUM_DELTA_NORM = 30.0  # 30-luma-unit delta saturates at 1.0


def compute_bw_storage_signals(
    img: Any, blob: Optional[bytes] = None, fmt: Optional[str] = None,
) -> Dict[str, Any]:
    """Categorical and uniformity signals. Returns a dict — different shape
    from the other three because there are several independent scalars
    rather than a single composite. Caller mixes them into T1 / penalties
    individually.

    Keys returned:
      bilevel: bool — PIL mode == '1' OR ≥92% of pixels in the dark/light
        tails. Penalize when True on B&W manga with screentones expected.
      chroma_subsampled: Optional[str] — "4:2:0", "4:4:4", or None.
        Forced 4:2:0 on a content_type==bw_manga page is a JPEG-laundering
        signature (born-digital B&W content should be mode='L' JPEG, not
        chroma-subsampled YCbCr).
      bg_uniformity: float in [0, 1]: 1.0 = pristine flat paper; 0.0 =
        noisy / color-cast / vignetted background.
      gutter_shadow_score: float in [0, 1]: 0 = no detectable spine
        shadow; 1 = obvious gutter shadow. Categorical "physical scan
        vs born-digital" tell.
      speckle_density: float in [0, 1]: 0 = clean; 1 = many dust/lint
        specks. (Reserved for future implementation; returns 0.0 for now —
        morphological top-hat is on the v6 roadmap but not critical
        Phase 1 leverage.)

    All keys are always present (no missing keys) so downstream consumers
    can `signals['bilevel']` without defensive checks.

    Cross-file: called from _compute_t1_score_bw.
    """
    import numpy as np
    out: Dict[str, Any] = {
        "bilevel": False,
        "chroma_subsampled": None,
        "bg_uniformity": 0.5,
        "gutter_shadow_score": 0.0,
        "speckle_density": 0.0,
    }
    try:
        # Bilevel via PIL mode first (cheapest possible signal).
        if getattr(img, "mode", None) == "1":
            out["bilevel"] = True
        # Group4-encoded CCITT-fax PNG/TIFF (rare but flag explicitly).
        info = getattr(img, "info", None) or {}
        if isinstance(info, dict) and info.get("compression") == "group4":
            out["bilevel"] = True

        # Convert to L for histogram-based checks.
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception:
        return out

    h, w = gray.shape
    if h < 32 or w < 32:
        return out

    # Bilevel detection via histogram tails (catches dithered or
    # threshold-stored content that PIL still labels mode='L').
    if not out["bilevel"]:
        n_pixels = gray.size
        tail_count = int(((gray <= 16) | (gray >= 240)).sum())
        tail_frac = tail_count / n_pixels
        if tail_frac >= BILEVEL_TAIL_FRACTION_THRESHOLD:
            out["bilevel"] = True

    # Chroma subsampling (JPEG only). PIL exposes per-component sampling
    # via `img.layer` as a list of tuples (id, h_sampling, v_sampling,
    # quant_table). For 4:4:4 each component has sampling (1,1); for
    # 4:2:0 the Y component has (2,2) and Cb/Cr each have (1,1). For
    # mode='L' JPEGs (true grayscale), `img.layer` has a single entry
    # and we report None (no chroma at all).
    if fmt and fmt.upper() in ("JPEG", "JPG"):
        try:
            layers = getattr(img, "layer", None)
            if layers and len(layers) >= 3:
                y_sampling = (layers[0][1], layers[0][2])
                if y_sampling == (2, 2):
                    out["chroma_subsampled"] = "4:2:0"
                elif y_sampling == (1, 1):
                    out["chroma_subsampled"] = "4:4:4"
                else:
                    out["chroma_subsampled"] = "other"
            elif layers and len(layers) == 1:
                out["chroma_subsampled"] = None  # true L-mode JPEG
        except Exception:
            pass

    # Background uniformity via Otsu high-mode MAD. Otsu split: histogram
    # bins, pick the threshold maximizing inter-class variance. Take the
    # higher-luminance class as "paper", compute MAD on those pixels.
    bg_uniformity = _compute_bg_uniformity(gray)
    out["bg_uniformity"] = round(bg_uniformity, 4)

    # Gutter shadow: difference of left/right strip means. A scanned book
    # has one bound edge that the platen can't fully flatten, leaving a
    # dark band. Compare the leftmost and rightmost GUTTER_STRIPE_WIDTH
    # columns' mean luminance — large delta on one side = shadow present.
    if w >= 2 * GUTTER_STRIPE_WIDTH + 100:
        left_strip = gray[:, :GUTTER_STRIPE_WIDTH].astype(np.float32).mean()
        right_strip = gray[:, -GUTTER_STRIPE_WIDTH:].astype(np.float32).mean()
        gutter_delta = abs(float(left_strip) - float(right_strip))
        out["gutter_shadow_score"] = round(
            float(np.clip(gutter_delta / GUTTER_LUM_DELTA_NORM, 0.0, 1.0)),
            4,
        )

    return out


def _compute_bg_uniformity(gray) -> float:
    """Otsu-threshold the image, take the higher-luminance (paper) class,
    return 1 - clamp(MAD / BG_UNIFORMITY_MAD_NORM, 0, 1).

    Implemented inline so we don't import cv2 just for thresholding —
    Otsu is a simple histogram-based algorithm.
    """
    import numpy as np
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = float(hist.sum())
    if total <= 0:
        return 0.5
    sum_total = float((hist * np.arange(256)).sum())
    weight_bg = 0.0
    sum_bg = 0.0
    best_var = -1.0
    best_thresh = 128
    for t in range(256):
        weight_bg += float(hist[t])
        if weight_bg == 0 or weight_bg == total:
            continue
        weight_fg = total - weight_bg
        sum_bg += float(t * hist[t])
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        var_between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var_between > best_var:
            best_var = var_between
            best_thresh = t
    # Paper class = pixels above threshold (higher luminance).
    paper_mask = gray > best_thresh
    if paper_mask.sum() < 100:
        return 0.5  # not enough paper pixels to assess uniformity
    paper_pixels = gray[paper_mask].astype(np.float32)
    median = float(np.median(paper_pixels))
    mad = float(np.median(np.abs(paper_pixels - median)))
    return float(1.0 - min(1.0, mad / BG_UNIFORMITY_MAD_NORM))
