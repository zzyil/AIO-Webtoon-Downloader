"""Tunable constants for the v5.1 quality-scoring pipeline.

Owns: T1 per-content-type weights, resolution normalization targets, USM
overshoot normalization bands, watermark region definitions, classification
thresholds. Single source of truth for everything tunable in the v5.1
scoring layer.

Cross-file:
  - sites/search_orchestrator.py imports these to drive _compute_t1_score,
    _compute_usm_overshoot (Phase 2), _detect_watermarks (Phase 1),
    _classify_series_content.
  - sites/base.py uses the WEBTOON_*/BW_MANGA_* thresholds in
    _probe_chapter_aggregate to make sampling decisions.
  - calibrate_quality_probe.py reads these to print per-content-type
    histograms and validate calibration ranges.

Why these live in a separate module: v5 had T1 weights inlined in
_compute_t1_score. v5.1 makes T1 weights content-type-dependent (webtoons
need different weights than B&W manga because anti-aliased gradients
reduce Tenengrad signal). With the constants extracted, calibration
script can override them via --t1-weights-override flag for tuning runs
without touching the production code path. Plan reference:
~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 4 section).
"""

from __future__ import annotations

# --- Series-level content classification thresholds ---------------------

# Width below which a series is considered "chunked webtoon" (each chapter
# split into many narrow pages). Real Eleceed pages are 690-800 px wide;
# 1000 leaves room for upscaled variants without catching traditional
# manga pages (typically 1500+ wide).
WEBTOON_MAX_WIDTH = 1000

# Within-chapter width variation coefficient. Chunked webtoons have rock-
# steady widths (CV ~ 0.0); traditional manga has more variation across
# pages (CV > 0.1). 0.05 is the empirically-validated cutoff per the
# Eleceed prototype (CV ≈ 0.007 in real data).
WEBTOON_WIDTH_CONSISTENCY_CV = 0.05

# Fraction of pages that must be color for color-content classifiers.
# Talentless Nana (B&W manga with color covers) sits at ~0.10-0.20 color
# ratio; pure color webtoons land >0.90.
WEBTOON_COLOR_RATIO_THRESHOLD = 0.70

# Page height above which we treat a "single page" as a vertical-scroll
# single-image webtoon. Real single-image webtoons are typically
# 100,000+ px tall; 4000 is well above traditional manga page heights
# (1500-3000) so it's a clean separator.
WEBTOON_SINGLE_IMAGE_HEIGHT = 4000

# Fraction of pages that must be grayscale to classify a series as B&W
# manga. 0.85 leaves room for occasional color spread/cover inserts.
BW_MANGA_GRAYSCALE_RATIO = 0.85

# Chroma variance threshold above which a page is considered "color". Used
# inside _compute_chroma_var to discriminate true-color pages from B&W
# pages that PIL might mis-flag as RGB mode. Empirically B&W manga JPEGs
# have chroma_var < 3 (encoder noise only); color pages > 10.
CHROMA_VARIANCE_THRESHOLD = 5.0


# --- Adaptive resolution-normalization targets (px-squared, area) -------
# v5 used a width-based linear normalization: res_norm = (w - 800) / 1600,
# clamped [0, 1]. This silently under-scored chunked webtoons (700-800 px
# wide → res_norm 0.0 regardless of how high-quality the page actually
# was). v5.1 switches to area-based with per-content-type targets so a
# 700×1100 webtoon page scores fairly against its peers.
#
# Targets are calibrated to the typical-quality reference for each content
# type. The Eleceed prototype showed 700×1100 ≈ 770k px² as the central
# tendency; 900k gives slight headroom so a true-high-quality 800×1200
# webtoon doesn't clamp to 1.0 too easily.
RES_NORM_TARGETS = {
    "bw_manga":                    2_000_000,    # ~1500×1350 — traditional manga
    "bw_manga_with_color_inserts": 2_000_000,    # same — color insets are rare
    "color_manga":                 2_000_000,    # same — full-color manga
    "color_webtoon_chunked":         900_000,    # Eleceed-grounded ~700×1280
    "color_webtoon_single_image": 10_000_000,    # tall single image, very different scale
    "unknown":                     2_000_000,    # default to manga reference
}


# --- Adaptive T1 component weights per content-type --------------------
#
# Anti-aliased webtoon gradients reduce Tenengrad signal (less sharp-edge
# energy per pixel). Drop tene from 0.15 → 0.05 and reinvest in:
#   - res (anti-cheat: catches sources that downscaled aggressively)
#   - fft_hf (still detects bilinear upscale even when there's no Tenengrad
#     signal)
# Sums to 1.00 in every branch. Source-of-truth schema: each entry is a
# dict mapping "res" / "qf" (or "lossless") / "block" / "fft_hf" / "tene"
# to weights. Per-format formulas live in _compute_t1_score and consume
# these dicts via t1_constants.T1_WEIGHTS_*[content_type].
T1_WEIGHTS_JPEG = {
    "bw_manga":                    {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
    "bw_manga_with_color_inserts": {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
    "color_manga":                 {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
    "color_webtoon_chunked":       {"res": 0.35, "qf": 0.25, "block": 0.15, "fft_hf": 0.20, "tene": 0.05},
    "color_webtoon_single_image":  {"res": 0.35, "qf": 0.25, "block": 0.15, "fft_hf": 0.20, "tene": 0.05},
    "unknown":                     {"res": 0.30, "qf": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
}

# WebP (lossy): no encoder-quality factor available, so the qf slot is
# reweighted across the others. Shape mirrors v5's `0.35 + 0.20 + 0.20 +
# 0.25` formula but indexed by content type for the same webtoon shift.
T1_WEIGHTS_WEBP = {
    "bw_manga":                    {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
    "bw_manga_with_color_inserts": {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
    "color_manga":                 {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
    "color_webtoon_chunked":       {"res": 0.40, "block": 0.20, "fft_hf": 0.25, "tene": 0.15},
    "color_webtoon_single_image":  {"res": 0.40, "block": 0.20, "fft_hf": 0.25, "tene": 0.15},
    "unknown":                     {"res": 0.35, "block": 0.20, "fft_hf": 0.20, "tene": 0.25},
}

# Lossless (PNG / WebP-VP8L): decode-quality substitute is the constant 1.0.
# Same shape as JPEG with the "qf" weight replaced by "lossless" (semantic
# difference only — keeps the table parseable by humans).
T1_WEIGHTS_LOSSLESS = {
    "bw_manga":                    {"res": 0.30, "lossless": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
    "bw_manga_with_color_inserts": {"res": 0.30, "lossless": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
    "color_manga":                 {"res": 0.30, "lossless": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
    "color_webtoon_chunked":       {"res": 0.35, "lossless": 0.25, "block": 0.15, "fft_hf": 0.20, "tene": 0.05},
    "color_webtoon_single_image":  {"res": 0.35, "lossless": 0.25, "block": 0.15, "fft_hf": 0.20, "tene": 0.05},
    "unknown":                     {"res": 0.30, "lossless": 0.25, "block": 0.15, "fft_hf": 0.15, "tene": 0.15},
}


# --- B&W-specialized T1 weights (v6, 2026-05-18) -----------------------
# When content_type ∈ {bw_manga, bw_manga_with_color_inserts}, the T1
# pipeline branches to _compute_t1_score_bw and uses these weight tables
# instead of the generic ones above. New slots:
#   "line"   — edge-conditioned sharpness minus JPEG ringing
#              (sites/bw_signals.py:compute_line_quality)
#   "screen" — FFT band-energy fraction at screen frequency
#              (sites/bw_signals.py:compute_screentone_integrity)
#   "bg"     — paper-class MAD uniformity
#              (sites/bw_signals.py:compute_bw_storage_signals[bg_uniformity])
#
# fft_hf is dropped from the B&W formula because compute_screentone_integrity
# subsumes it — both measure mid-frequency band energy, and the screentone
# version is more discriminating (notches out JPEG block artifacts and
# weighs the band against background noise rather than total energy).
#
# Why these weights: B&W manga's two defining content classes are line
# art and screentones, so they get 0.20 each. Resolution and JPEG QF drop
# from 0.30/0.25 → 0.18/0.18 because the new line/screen signals
# discriminate quality more reliably than raw resolution (which is
# spoofable by upscaling) or QF estimate (which is noisy on non-standard
# quantization tables). bg/block/tene split the remaining 0.24 evenly.
#
# Each row sums to 1.00 — verified by tests/test_t1_bw_scoring.py.

T1_WEIGHTS_JPEG_BW = {
    "bw_manga":                    {"res": 0.18, "qf": 0.18, "line": 0.20, "screen": 0.20, "bg": 0.08, "block": 0.08, "tene": 0.08},
    "bw_manga_with_color_inserts": {"res": 0.18, "qf": 0.18, "line": 0.20, "screen": 0.20, "bg": 0.08, "block": 0.08, "tene": 0.08},
}

T1_WEIGHTS_WEBP_BW = {
    "bw_manga":                    {"res": 0.22, "line": 0.22, "screen": 0.22, "bg": 0.10, "block": 0.12, "tene": 0.12},
    "bw_manga_with_color_inserts": {"res": 0.22, "line": 0.22, "screen": 0.22, "bg": 0.10, "block": 0.12, "tene": 0.12},
}

T1_WEIGHTS_LOSSLESS_BW = {
    "bw_manga":                    {"res": 0.18, "lossless": 0.18, "line": 0.20, "screen": 0.20, "bg": 0.08, "block": 0.08, "tene": 0.08},
    "bw_manga_with_color_inserts": {"res": 0.18, "lossless": 0.18, "line": 0.20, "screen": 0.20, "bg": 0.08, "block": 0.08, "tene": 0.08},
}


# --- B&W penalty & bonus constants (v6) --------------------------------
# Penalties subtract from the composite T1_BW score after weighted-sum;
# bonuses add. All bounded so a single misfire can't completely flip
# the score, and all subtracted/added AFTER the weighted sum so the
# weight tables stay self-contained / sum-to-1.

# Maximum penalty subtracted when bw_signals.compute_upscaler_score
# returns 1.0 (obvious Real-ESRGAN/Waifu2x fingerprint). Penalty scales
# linearly: actual_penalty = UPSCALER_PENALTY_MAX * upscaler_score.
UPSCALER_PENALTY_MAX = 0.10

# Fixed penalty when bw_signals.compute_bw_storage_signals reports
# bilevel=True for content classified as B&W manga (where screentones
# are expected). G4-fax / PIL mode='1' / histogram-bimodal storage
# destroys screentones; penalize.
BILEVEL_PENALTY_BW = 0.15

# Fixed penalty when JPEG is stored with YCbCr 4:2:0 subsampling on
# content classified as B&W. Real born-digital B&W manga arrives as
# mode='L' JPEG (no chroma planes at all); forced 4:2:0 on B&W is a
# laundering signature (was re-encoded through a color JPEG pipeline).
CHROMA_PENALTY_BW = 0.04

# Small bonus added when B&W content arrives in a lossless format
# (PNG, lossless WebP, JXL-lossless). Community consensus (per Phase 1
# research): lossless is genuinely better than lossy for B&W manga
# because screentones survive intact and file sizes stay small.
LOSSLESS_BONUS_BW = 0.05


# --- Resolution-normalization decay (v6) --------------------------------
# The legacy res_norm formula clamps area/target to [0, 1] — area >=
# target → score = 1.0. This rewards aggregators that upscale: a
# 3000×4250 Real-ESRGAN output of a 1500×2125 source hits res_norm =
# 1.0, no penalty. v6 introduces a soft decay above DECAY_START × target
# so extreme over-resolution gets pushed back toward the FLOOR. Pairs
# with compute_upscaler_score to catch upscaling at two layers (the
# resolution outlier AND the FFT-spectrum fingerprint).
#
# Shape: res_norm = 1.0 in [target, DECAY_START * target]; decays
# linearly to FLOOR by [3 × target]; capped at FLOOR thereafter.
# Active only when content_type ∈ bw_manga* (color manga / webtoons
# don't get this penalty because higher-res scans of those are
# legitimately better; B&W manga past ~3000px tall is almost always
# upscaled because few scanlators release at that resolution).
RES_NORM_DECAY_START = 1.5    # area / target above this → decay begins
RES_NORM_DECAY_END = 3.0      # area / target above this → res_norm = FLOOR
RES_NORM_FLOOR = 0.85         # asymptotic floor for over-resolution


# --- USM overshoot normalization per content type (Phase 2) ------------
# B&W line art shouldn't show overshoot rings naturally — sensitive
# threshold catches USM-applied fake sharpening at small magnitudes.
# Color webtoons have anti-aliased gradients that produce some natural
# overshoot — lenient threshold avoids false-positives on legit content.
USM_NORMALIZATION = {
    "bw_manga":                    0.30,
    "bw_manga_with_color_inserts": 0.30,
    "color_manga":                 0.40,
    "color_webtoon_chunked":       0.50,
    "color_webtoon_single_image":  0.50,
    "unknown":                     0.40,
}


# --- Watermark detection regions (Phase 1) -----------------------------
# Box coordinates as (x, y, x2, y2). Negative values count from the right
# or bottom edge (Python slice semantics). None means "until the edge".
# Used by _detect_watermarks in search_orchestrator.py to crop each
# region from the source image before running easyocr.
WATERMARK_REGIONS = {
    "corner_tl": (0, 0, 200, 150),
    "corner_tr": (-200, 0, None, 150),
    "corner_bl": (0, -150, 200, None),
    "corner_br": (-200, -150, None, None),
    "edge_top":      (0, 0, None, 100),
    "edge_bottom":   (0, -100, None, None),
    # center_strip uses string expressions evaluated at runtime relative
    # to image dimensions. See _resolve_region in search_orchestrator.py.
    "center_strip":  (0, "h/2 - 50", None, "h/2 + 50"),
}

# Watermark penalty constants. Per-region penalty stacks up to the max.
# Calibrated so 1-2 watermark regions dock 0.05-0.10; pages with 3+
# triggered regions saturate at 0.15 (out of the ±0.15 T1 adjustment
# headroom; watermarks always subtract).
WATERMARK_PER_REGION_PENALTY = 0.05
WATERMARK_MAX_PENALTY = 0.15

# Chroma subsampling penalty (Phase 5) — applied when JPEG SOF marker
# shows 4:2:0 subsampling AND the image actually has meaningful color
# variance. Small (0.03) because the visual difference between 4:2:0 and
# 4:4:4 is subtle and content-dependent.
CHROMA_PENALTY = 0.03
# Calibrated against tmp_1571 (Eleceed) where typical color webtoon
# chroma_complexity lands ~0.025-0.035; threshold 0.02 catches the
# typical color-content case without false-positives on near-monochrome
# scenes (which sit < 0.015 in our fixtures).
CHROMA_COMPLEXITY_THRESHOLD = 0.02    # Cb+Cr std dev normalized to [0, 1]


# --- AVIF format premium (Phase 5) -------------------------------------
# AVIF is the most encoder-efficient lossy format we support; pages that
# arrive in AVIF tend to be high-quality at small file sizes. Small bonus
# multiplier (1.05) reflects this without overweighting format.
AVIF_QUALITY_PREMIUM = 1.05


__all__ = [
    "WEBTOON_MAX_WIDTH",
    "WEBTOON_WIDTH_CONSISTENCY_CV",
    "WEBTOON_COLOR_RATIO_THRESHOLD",
    "WEBTOON_SINGLE_IMAGE_HEIGHT",
    "BW_MANGA_GRAYSCALE_RATIO",
    "CHROMA_VARIANCE_THRESHOLD",
    "RES_NORM_TARGETS",
    "T1_WEIGHTS_JPEG",
    "T1_WEIGHTS_WEBP",
    "T1_WEIGHTS_LOSSLESS",
    "T1_WEIGHTS_JPEG_BW",
    "T1_WEIGHTS_WEBP_BW",
    "T1_WEIGHTS_LOSSLESS_BW",
    "USM_NORMALIZATION",
    "WATERMARK_REGIONS",
    "WATERMARK_PER_REGION_PENALTY",
    "WATERMARK_MAX_PENALTY",
    "CHROMA_PENALTY",
    "CHROMA_COMPLEXITY_THRESHOLD",
    "AVIF_QUALITY_PREMIUM",
    "UPSCALER_PENALTY_MAX",
    "BILEVEL_PENALTY_BW",
    "CHROMA_PENALTY_BW",
    "LOSSLESS_BONUS_BW",
    "RES_NORM_DECAY_START",
    "RES_NORM_DECAY_END",
    "RES_NORM_FLOOR",
]
