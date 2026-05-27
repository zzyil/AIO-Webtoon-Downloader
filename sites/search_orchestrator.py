"""Cross-site search orchestrator.

Owns: fan-out across all search-capable handlers, title-match scoring,
candidate dedupe, ranking. Phase 1a per the plan
(C:\\Users\\legoc\\.claude\\plans\\snappy-forging-waffle.md).

Reads:
  - sites.iter_search_capable_handlers() — handler registry filtered to those
    with an overridden search() method. Sites that fail to load this filter
    just don't participate in search; matches the "no reliability tracking"
    principle.
  - sites/quality_seed.json — per-site image-quality priors used ONLY as a
    tiebreaker when title-match scores fall within 0.10 of each other. The
    seed never adds to the title-match score.

Called by: aio_search_cli.run_search_mode (which is invoked by aio-dl.py
when --search is passed).

Cross-file:
  - SearchHit dataclass lives in sites/base.py.
  - The probe-failure cache hooks into make_request's _record_rate_limit so a
    timed-out host suppresses itself for 1h via the existing cooldown machinery
    (aio-dl.py:589, aio-dl.py:628). No parallel cooldown system.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from .base import BaseSiteHandler, SearchHit


# ─── ML rating gate ────────────────────────────────────────────────────
# When False (default), all torch-backed quality scoring (T2 CLIP-IQA/NIQE,
# T3 paired-DISTS, watermark detection) is skipped. Ranking falls back to
# T1-only (numpy + PIL pixel analysis) + seed prior. Set True via
# aio_search_cli.run_search_mode when the user passes --enable-ml-rating
# OR the AIO_ENABLE_ML_RATING env var is set at process spawn.
#
# Why off by default (2026-05-20): torch's Windows import path calls
# platform.machine() inside torch/__init__.py:_load_dll_libraries, which
# Python 3.13 implements via a WMI query (platform._wmi_query). When WMI
# is degraded on the host, every aio-dl.py --search spawn hangs forever
# at module import — no output, no progress, no recovery. Even on healthy
# systems, eager torch import costs 2-5s of startup. The T2/T3 quality
# boost over T1 is ~3-8% on calibration; for the top result of popular
# queries (Frieren, Attack on Titan) T1 alone almost always picks the
# right source. Making ML opt-in lets users who want the extra accuracy
# turn it on, without imposing a torch import on every other search.
#
# Cross-file: aio-dl.py argparse defines --enable-ml-rating;
# aio_search_cli.run_search_mode reads it and calls set_ml_rating_enabled()
# BEFORE search_all so all downstream lazy availability checks see the
# correct gate state. The flag is read once per process.
_ML_RATING_ENABLED: bool = os.environ.get(
    "AIO_ENABLE_ML_RATING", ""
).lower() in ("1", "true", "yes", "on")


def set_ml_rating_enabled(enabled: bool) -> None:
    """Set the global ML-rating gate. Call BEFORE search_all() / warmup_t2_models
    so the lazy availability checks see the correct state. After search
    has already decided to skip T2/T3 in a run, calling this has no
    retroactive effect for that run.

    Cross-file: invoked by aio_search_cli.run_search_mode based on the
    --enable-ml-rating CLI flag.
    """
    global _ML_RATING_ENABLED
    _ML_RATING_ENABLED = bool(enabled)


def is_ml_rating_enabled() -> bool:
    """Read the current ML-rating gate state. Used by the warmup path and
    the T2/T3 inference branches to decide whether to import torch at all."""
    return _ML_RATING_ENABLED


# v5.1 / lazy-import refactor 2026-05-20: torchvision patch was previously
# called at module load (a side-effect import of torchvision). Now invoked
# lazily from `_get_dists_model` / `_get_stlpips_model` — the only call
# sites that actually need it. Keep the function so test code and any
# future caller can still trigger it explicitly.
#
# The patch translates the deprecated torchvision `pretrained=True` API to
# the modern `weights=VGG16_Weights.DEFAULT` enum so piq.DISTS (v0.8) and
# pyiqa.stlpips-vgg stop emitting DeprecationWarning at first instantiation.
# This is a real semantic translation (not a warning-filter): the patched
# call returns the same model the modern API would return.
def _patch_torchvision_deprecated_apis() -> None:
    try:
        import torchvision.models as _tvm
        from torchvision.models import (
            VGG16_Weights, AlexNet_Weights, SqueezeNet1_1_Weights,
        )
    except Exception:
        return

    def _translate_pretrained_kwargs(kwargs, weights_enum):
        """Mutate kwargs in place to convert deprecated args to modern API."""
        pretrained = kwargs.pop("pretrained", None)
        if pretrained is True and "weights" not in kwargs:
            kwargs["weights"] = weights_enum.IMAGENET1K_V1
        elif pretrained is False and "weights" not in kwargs:
            kwargs["weights"] = None
        # Translate deprecated string-form weights (e.g. weights="DEFAULT")
        # to the proper enum form.
        weights = kwargs.get("weights")
        if isinstance(weights, str):
            wu = weights.upper()
            if wu == "DEFAULT":
                kwargs["weights"] = weights_enum.DEFAULT
            elif hasattr(weights_enum, wu):
                kwargs["weights"] = getattr(weights_enum, wu)

    def _make_patched(orig, weights_enum):
        def _wrapped(*args, **kwargs):
            _translate_pretrained_kwargs(kwargs, weights_enum)
            return orig(*args, **kwargs)
        # Unique sentinel attribute so re-import is idempotent. We can't
        # use `__wrapped__` for this check because torchvision's own
        # @handle_legacy_interface decorator already sets that attribute,
        # which would cause our patch to incorrectly skip installation
        # on first run.
        _wrapped._aio_dl_patched = True  # type: ignore[attr-defined]
        return _wrapped

    # piq.DISTS uses vgg16. pyiqa's stlpips-vgg also uses vgg16. lpips uses
    # alexnet/vgg/squeezenet — pre-emptive patches keep them clean too in
    # case any downstream code touches them.
    for attr_name, weights_enum in (
        ("vgg16", VGG16_Weights),
        ("alexnet", AlexNet_Weights),
        ("squeezenet1_1", SqueezeNet1_1_Weights),
    ):
        orig = getattr(_tvm, attr_name, None)
        if orig is None or getattr(orig, "_aio_dl_patched", False):
            continue  # already patched (idempotent re-import) or missing
        try:
            setattr(_tvm, attr_name, _make_patched(orig, weights_enum))
        except Exception:
            pass


# --- Tunables -----------------------------------------------------------
# Title-match floor: hits below this rapidfuzz similarity are discarded.
# 0.55 keeps "Frieren" matching "Sousou no Frieren" via alt-titles but kicks
# out unrelated series that share a single word.
DEFAULT_MIN_MATCH = 0.55

# Tiebreaker window: when two sources for the same candidate score within
# this much on title-match, the quality_seed prior breaks the tie. Above this
# delta, title-match is fully decisive. See plan section "G. --search wired
# from day one (with seed-prior tiebreaker)".
TIEBREAKER_WINDOW = 0.10

# Per-site search timeout. Dead/unresponsive sites just take this long to
# self-select out per-run; combined with the probe-failure cache they cost
# zero on subsequent searches within the cache window.
DEFAULT_PER_SITE_TIMEOUT_S = 8.0

# Per-host probe-failure cache: after this many consecutive failures, suppress
# the host for PROBE_FAILURE_TTL_S via the existing _record_rate_limit
# coordinator entry. Self-healing — re-tries after the entry expires.
PROBE_FAILURE_THRESHOLD = 2
PROBE_FAILURE_TTL_S = 3600  # 1 hour

# Image-quality cache TTL (Phase 2). Covers/CDN policies change slowly; 30 days
# means we re-probe a (site, series) at most once a month under normal use.
IMG_QUALITY_TTL_S = 30 * 24 * 3600

# Chapter-image probe is heavier than cover probe (3-4 HTTP requests vs 1).
# We only run it for sites with seed_quality at or above this threshold —
# the long tail of Madara/MangaThemesia extras (default seed 0.50) gets
# cover probe to keep the total probe phase bounded. Sites with seed >= 0.65
# are the ones explicitly listed in sites/quality_seed.json — those are the
# candidates that actually compete for "best source", and the same threshold
# powers the --multi-source-quality-min default. Low-seed sites' image scores
# only matter for tiebreaking within the title-match window during search
# ranking; cover-based scores are good enough for that. This is a deliberate
# accuracy/speed tradeoff — see search_system.md "chapter-image probe" section.
CHAPTER_PROBE_MIN_SEED = 0.65

# 2026-05-08: when an "expensive-probe" handler (one that requires Playwright
# / VRF capture per chapter — currently only MangaFire) hits a result whose
# title_match falls below this threshold, the orchestrator clamps the probe
# to a SINGLE image instead of the usual 5 samples. Rationale: low-match
# results are usually noise (spinoffs, doujinshi, unrelated series sharing a
# token), and the 4 extra image fetches per low-confidence source add up —
# 4 wrong matches × 5 images = 16 wasted requests per search. We still
# probe one image to keep some signal for ranking; we just don't aggressively
# pay the bandwidth on the long tail. Strong matches (title_match ≥ this)
# still get the full 5-sample aggregate probe. Pure-HTTP handlers ignore
# this knob entirely (their EXPENSIVE_PROBE class attr is False).
#
# Threshold rationale: rapidfuzz WRatio on a clean main-series alt-title
# match typically lands ≥0.95; doujinshi with the literal English title in
# their slug land ~0.85-0.95; spinoffs / wrong-series-sharing-a-word land
# ~0.55-0.75. 0.85 keeps real-but-imperfect matches on the full probe path
# while quick-probing the noise.
EXPENSIVE_PROBE_QUICK_THRESHOLD = 0.85

# Gate for the official-publisher tiebreaker in _cmp. is_official wins
# over a non-official peer ONLY when both sources clear this title-match
# floor AND are within TIEBREAKER_WINDOW of each other; otherwise we
# fall through to title_match. Prevents a weak-match official hit (e.g.
# a Canvas series whose normalized title accidentally clustered with a
# real series via union-find) from outranking a strong-match aggregator.
# 0.85 aligns with EXPENSIVE_PROBE_QUICK_THRESHOLD: "this is the line
# below which we already treat title_match as noisy". Strong-match
# webtoons-vs-toonily case (both score ≈1.0 → within window, both
# strong → official wins) is preserved; weak-match canvas false-merge
# case (linewebtoon 1.0 + canvas 1.0 is still possible if the title
# literally matches, but Fix A + Fix C catch THAT failure path
# upstream — see SourceEntry.is_official assignment + count_outlier).
IS_OFFICIAL_REQUIRES_TITLE_MATCH = 0.85

# Hard cap on the entire probe phase. After this, we abandon any still-running
# probes (their threads keep going but we don't wait — the cache only persists
# completed probes, so unfinished ones get retried next search). Bounds worst-
# case latency from a hung handler (e.g., a site whose cover-fetch internally
# blocks past the per-request timeout, or a Playwright-VRF call stuck in a bad
# state).
#
# Bumped to 120s when the multi-page aggregate probe shipped (2026-05-07): each
# high-seed probe now does up to 5 sequential image fetches (~10-15s typical)
# instead of 1 (~2s). At parallelism=6, 22 high-seed sites take ~22/6 * 12s =
# ~44s in the new path vs ~7s in the old single-page path. 120s gives headroom
# for one fully-throttled site (5x15s = 75s upper bound per probe) without
# guillotining the rest.
#
# Bumped to 180s when v5 breadth sampling shipped (2026-05-17): each high-seed
# probe now does 8 separate `get_chapter_images` calls (one per sampled chapter)
# + 1 image fetch each + up to THROTTLE_TAIL_PAGES extra fetches. For HTML-
# scraped handlers `get_chapter_images` is ~1-2s per call; for VRF handlers
# (mangafire) it's 3-5s. Worst case per probe: 8 × 7s + 3 × 15s = ~100s for
# VRF, 8 × 3s + 3 × 5s = ~40s for HTML. At parallelism=6 with 22 high-seed
# sites, ~22/6 × 60s = ~220s mean. Mangadex API-driven handlers complete in
# ~10-20s total because their `get_chapter_images` is a single JSON call
# returning all URLs.
#
# Bumped to 240s on T2 shipping (Phase 3, same day): T2 NIQE inference at
# ~200ms × 176 invocations (22 sites × 8 chapters) = ~35s additional sequential
# floor. T2 inference is serialized per-model via _T2_NIQE_INFER_LOCK because
# PyTorch nn.Module forward isn't thread-safe on a shared instance. Workers
# can still do I/O in parallel while waiting on the lock so effective
# wall-time overhead is modest, but the deadline needs headroom.
PROBE_PHASE_DEADLINE_S = 240.0


# --- Data shapes --------------------------------------------------------
@dataclass
class SourceEntry:
    """One source's data for a particular SeriesCandidate.

    `composite_score` is what the candidate's `sources` list is sorted by.
    In Phase 1a it's just title_match (with the seed prior tie-break baked
    in via stable sort). In later phases img_quality_score, user_modifier,
    and feedback flow into composite_score directly.

    `img_quality_score=None` means "not yet measured" — the comparator falls
    back to seed_quality. A measured 0.0 (e.g., aggregate probe got 0/5
    successful samples — CDN-poisoned site like rizzchoros.cloud during a
    bad period) is distinct from un-measured and IS used by the comparator,
    so the broken-CDN signal isn't camouflaged by a high seed prior.
    """
    site: str
    url: str
    title: str
    cover: Optional[str]
    title_match: float
    seed_quality: float
    img_quality_score: Optional[float] = None  # populated in Phase 2; None = un-measured
    # Aggregate metadata from _probe_chapter_aggregate / _score_image_blob —
    # carries bpp / decode_quality / is_grayscale / outlier / format / etc. so
    # the UI can render a "why this score?" tooltip without recomputing. Shape
    # is the dict returned by _probe_chapter_aggregate (or _score_image_blob
    # in the cover-fallback path). None = un-measured.
    img_quality_metadata: Optional[Dict] = None
    user_modifier: float = 0.0      # populated in Phase 5
    composite_score: float = 0.0
    chapter_count_hint: Optional[int] = None
    actual_chapter_count: Optional[int] = None
    dmca_likely: bool = False
    raw_score: float = 0.0
    # True when the handler is the publisher's own platform (e.g. linewebtoon
    # = webtoons.com), not an aggregator re-hosting other publishers' content.
    # Populated in search_all from handler.OFFICIAL_PUBLISHER. Consumed by
    # _cmp as the top tiebreaker within a SeriesCandidate, above quality.
    is_official: bool = False
    # Wrong-match sink: True when this source's actual_chapter_count is
    # vastly below peer sources in the same candidate AND the source's
    # own chapter_count_hint doesn't claim a count comparable to peers
    # (i.e. it's not a DMCA-affected source claiming chapters it can't
    # deliver — it's a count-outlier whose union-find-merge with peers is
    # almost certainly a false-positive driven by title-string collision).
    # Treated by _cmp identically to dmca_likely (sink to back of the
    # candidate's source list) but DELIBERATELY not surfaced in to_json /
    # the UI — the UI's DMCA flag should only fire for actual DMCA-affected
    # sources to avoid users misreading wrong-match as a takedown. Set by
    # the same cross-site check at search_orchestrator.py:~5230 that
    # populates dmca_likely; the differentiator is whether own_hint
    # supports the high-count claim. See linewebtoon._populate_chapter_counts
    # for the data source on the webtoons side. NOT surfaced in to_json
    # intentionally — internal-only ranking signal.
    count_outlier: bool = False


@dataclass
class SeriesCandidate:
    canonical_title: str
    canonical_year: Optional[int]
    sources: List[SourceEntry] = field(default_factory=list)

    def to_json(self) -> Dict:
        return {
            "canonical_title": self.canonical_title,
            "canonical_year": self.canonical_year,
            "sources": [
                {
                    "site": s.site,
                    "url": s.url,
                    "title": s.title,
                    "cover": s.cover,
                    "title_match": round(s.title_match, 4),
                    "seed_quality": round(s.seed_quality, 4),
                    "img_quality_score": round(s.img_quality_score, 4) if s.img_quality_score is not None else None,
                    "img_quality_metadata": s.img_quality_metadata,
                    "composite_score": round(s.composite_score, 4),
                    "chapter_count_hint": s.chapter_count_hint,
                    "actual_chapter_count": s.actual_chapter_count,
                    "dmca_likely": s.dmca_likely,
                    "is_official": s.is_official,
                }
                for s in self.sources
            ],
        }


# --- Quality-seed loading ----------------------------------------------
_SEED_LOCK = threading.Lock()
_SEED_CACHE: Optional[Dict[str, float]] = None


def _load_quality_seed() -> Dict[str, float]:
    """Load and cache sites/quality_seed.json. Missing file or parse error
    just yields an empty dict — the orchestrator degrades to pure title-match
    ranking, which is correct (no tiebreaker means no preference between
    equally-matching sites)."""
    global _SEED_CACHE
    with _SEED_LOCK:
        if _SEED_CACHE is not None:
            return _SEED_CACHE
        path = os.path.join(os.path.dirname(__file__), "quality_seed.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
        seed: Dict[str, float] = {}
        for k, v in (data or {}).items():
            if k.startswith("_"):
                continue
            try:
                seed[str(k).lower()] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
        _SEED_CACHE = seed
        return _SEED_CACHE


# --- Probe-failure cache (in-process + JSON snapshot) ------------------
class ProbeFailureCache:
    """Track per-host search-time failures to skip dead sites cheaply.

    Persists to ~/.aio-dl/cache/probe_failures.json so back-to-back invocations
    share state. Integrates with aio-dl.py's _record_rate_limit by accepting
    a callable that records a long cooldown when a host crosses the failure
    threshold — that's how dead sites stay out of make_request's eyeballs
    without a separate suppression layer.

    Self-healing: entries expire after PROBE_FAILURE_TTL_S so a recovered site
    naturally rejoins on the next search.
    """

    def __init__(
        self,
        ttl_s: float = PROBE_FAILURE_TTL_S,
        threshold: int = PROBE_FAILURE_THRESHOLD,
        record_cooldown: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.ttl_s = ttl_s
        self.threshold = threshold
        # record_cooldown: (host, seconds) -> None. Plumbs into
        # aio-dl.py:_record_rate_limit so the existing rate-limit machinery
        # honors our suppression. Optional — if None, we still maintain the
        # in-process count and the search loop checks is_blocked().
        self.record_cooldown = record_cooldown
        self._lock = threading.Lock()
        # host -> (consecutive_failures, expires_at_epoch)
        # Both partial-failure and blocked states have an expires_at so the
        # snapshot loader can prune stale entries with one rule. is_blocked
        # is "fails >= threshold AND expires_at > now"; everything else is
        # a partial-failure counter that disappears once the entry expires.
        self._state: Dict[str, Tuple[int, float]] = {}
        self._snapshot_path = os.path.join(
            os.path.expanduser("~"), ".aio-dl", "cache", "probe_failures.json"
        )
        self._load_snapshot()

    def _load_snapshot(self) -> None:
        try:
            with open(self._snapshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                cleaned: Dict[str, Tuple[int, float]] = {}
                for host, entry in data.items():
                    if not isinstance(entry, list) or len(entry) != 2:
                        continue
                    fails, until = entry
                    try:
                        fails = int(fails)
                        until = float(until)
                    except (TypeError, ValueError):
                        continue
                    if until > now:
                        cleaned[str(host)] = (fails, until)
                self._state = cleaned
        except (OSError, json.JSONDecodeError):
            return

    def _save_snapshot(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._snapshot_path), exist_ok=True)
            payload = {h: list(v) for h, v in self._state.items()}
            tmp = self._snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._snapshot_path)
        except OSError:
            pass

    def is_blocked(self, host: str) -> bool:
        if not host:
            return False
        with self._lock:
            entry = self._state.get(host)
            if not entry:
                return False
            fails, expires_at = entry
            if expires_at <= time.time():
                self._state.pop(host, None)
                return False
            # Only block when threshold actually crossed — partial-failure
            # entries survive as counters but don't suppress the host yet.
            return fails >= self.threshold

    def record_failure(self, host: str) -> None:
        if not host:
            return
        with self._lock:
            fails, _ = self._state.get(host, (0, 0.0))
            fails += 1
            expires_at = time.time() + self.ttl_s
            self._state[host] = (fails, expires_at)
            if fails >= self.threshold and self.record_cooldown:
                try:
                    self.record_cooldown(host, self.ttl_s)
                except Exception:
                    pass
            # Persist on every change so back-to-back aio-dl.py invocations
            # accumulate consecutive failures across runs. Without this, a
            # threshold of 2 never trips because each run resets the counter.
            self._save_snapshot()

    def record_success(self, host: str) -> None:
        if not host:
            return
        with self._lock:
            if host in self._state:
                self._state.pop(host, None)
                self._save_snapshot()


# --- Image-quality probe + cache (Phase 2) -----------------------------
class ImageQualityCache:
    """Persistent (site, series_url) -> measured image-quality score cache.

    Used by search_all to replace seed_quality with measured img_quality_score
    in the rank composite once a (site, series) has been probed. Per-pair TTL
    so CDN/recompression policy changes are detected without manual cache
    invalidation.

    Schema in ~/.aio-dl/cache/img_quality.json:
      { "<site>:<series_url>": {
          "score": float,
          "metadata": {...},
          "expires_at": float-epoch,
          "schema_version": 2,
        } }

    schema_version bumps:
      - v1 (implicit, missing field): cover-image probe. Biased per-site
        because covers and chapter pages have different CDN policies.
      - v2: single-page chapter-image probe (middle page of a representative
        chapter). Better than v1 but couldn't detect CDN throttling under
        bulk fetch — a site that throttles on consecutive requests scored
        identically to a healthy site.
      - v3: multi-page aggregate probe (5 sequential pages: start,
        start-mid, mid, mid-last, last). Failed fetches count as 0 in the
        aggregate, so a flaky CDN scores proportional to its failure rate.
      - v4 (2026-05-08): content-aware decode_quality. Adds:
          * Hybrid median/mean aggregation in _probe_chapter_aggregate
            (sites/base.py:483) — median when all 5 succeed (suppresses
            color/B&W content variance), mean if any failed (preserves
            throttle-detection signal).
          * Lossless format short-circuit (_detect_lossless_blob): PNG
            and WebP-VP8L score decode_quality = 1.0 honestly instead of
            the prior monolithic 0.85.
          * Grayscale detection (_is_grayscale_pil) + dual bpp curves
            (_bpp_decode_quality) for lossy WebP/AVIF — calibrated B&W
            band, plausible-shaped color band.
          * Sanity-clip outliers: WebP < 0.05 bpp clamped to 0.1
            (broken-encoding flag); JPEG > 0.6 bpp clamped to 1.0
            (high-quality JPEG that the QT estimate undersells).
      - v5 (2026-05-17): full T1+T2+T3 rewrite of scoring + breadth
        sampling rewrite of probe pipeline. Replaces the
        0.4*res + 0.3*format + 0.3*decode formula with an objective
        T1 (resolution + Hass-LSM JPEG QF + Wang blockiness + FFT
        high-freq ratio + Tenengrad sharpness), optional T2 (CLIP-IQA
        + NIQE via pyiqa, when available), and post-probe T3 (paired
        DISTS across cross-source candidates). Sampling moves from
        "5 pages in 1 chapter" to "1 page in each of 8 chapters" plus
        a "throttle-probe tail" that emits cdn_reliability metadata.
        See plan: ~/.claude/plans/how-robust-is-the-memoized-koala.md.
        Pre-v5 entries are dropped at load so they get re-probed.
      - v6 (2026-05-17, v5.1 final): adds content_type classification,
        per-content-type T1 weights + adaptive res_norm targets, USM
        overshoot detection (damps Tenengrad), watermark detection via
        easyocr, JPEG-ghost double-compression detection, ECC/ORB
        alignment fallback tiers, AVIF support + chroma subsampling
        penalty, conditional GPU routing. Required v6 metadata includes
        `content_type` so the field-completeness gate drops pre-v6
        entries on load. Pre-v6 entries (v5 schema) are dropped at load.
      - v7 (2026-05-18): watermark detector recalibrated to suppress
        false positives. Manga thresholds raised from `n>=1 AND cov>=0.05`
        to `n>=2 AND cov>=0.20` so routine in-corner content (speech
        bubbles, sound effects, page numbers) no longer trips
        `heavy_watermark`. Cover-probe fallback path now skips the
        detector entirely (cover artwork has title/logo text in the
        cropped regions by design — flagging that as a watermark was
        always wrong). Both changes make `t1_score` and `outlier`
        differ vs v6 for the same source, so v6 entries are dropped on
        load; the schema_version field is the gate.
      - v8 (2026-05-18): B&W manga rewrite. Adds:
          * sites/bw_signals.py primitives (screentone integrity,
            line quality, upscaler fingerprint, bilevel/chroma/bg
            uniformity) wired into _compute_t1_score_bw for
            content_type ∈ bw_manga / bw_manga_with_color_inserts.
          * v6 T1_WEIGHTS_{JPEG,WEBP,LOSSLESS}_BW weight tables in
            sites/t1_constants.py, plus UPSCALER_PENALTY_MAX /
            BILEVEL_PENALTY_BW / CHROMA_PENALTY_BW / LOSSLESS_BONUS_BW
            and resolution-decay (RES_NORM_DECAY_*).
          * torchmetrics CLIP-IQA+ replacing pyiqa's broken
            clip-iqa+ path in _compute_t2_score. NIQE skipped on
            B&W content (NSS-violation). New `clip_iqa_score`
            metadata key.
          * _run_pairwise_ranking replaces anchor-based DISTS in T3:
            component-level win-rate aggregation on shared chapters.
            New `pairwise_*` metadata keys; legacy `paired_*` keys
            no longer populated (the v5.1 _run_paired_comparison is
            dormant). Pre-v8 entries dropped on load.

    No threading concerns beyond `_lock` because the orchestrator's parallel
    probe phase reads/writes from worker threads.
    """

    SCHEMA_VERSION = 8

    def __init__(self, ttl_s: float = IMG_QUALITY_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._state: Dict[str, Dict] = {}
        self._snapshot_path = os.path.join(
            os.path.expanduser("~"), ".aio-dl", "cache", "img_quality.json"
        )
        self._load_snapshot()

    @staticmethod
    def _key(site: str, series_url: str) -> str:
        return f"{site}:{series_url}"

    def _load_snapshot(self) -> None:
        try:
            with open(self._snapshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                cleaned: Dict[str, Dict] = {}
                for k, v in data.items():
                    if not isinstance(v, dict):
                        continue
                    # Schema-version mismatch entries are dropped on load.
                    # Each bump invalidates the entire cache because the
                    # score scalar's calibration changes between versions
                    # (mixing old-scalars with new-scalars in the same
                    # comparator produces inconsistent rankings). 30-day
                    # TTL means worst case is a re-probe storm on the next
                    # search after upgrade. See ImageQualityCache class
                    # docstring for the per-version migration history.
                    if v.get("schema_version") != self.SCHEMA_VERSION:
                        continue
                    # Field-completeness gate: even within a schema version,
                    # entries that lack required fields are dropped. Catches
                    # the case where a schema bump landed in one commit and
                    # the field-population code landed in a later commit
                    # within the same dev cycle, leaving "marked v5 but
                    # missing v5 fields" entries in cache. Required v5
                    # fields list MUST be kept in sync with what
                    # _compute_t1_score writes via base.py's aggregate.
                    # v6 (v5.1 final) required fields. content_type
                    # distinguishes v6 from v5 entries even when both
                    # carry t1_score / blockiness / etc. tenengrad_clean
                    # is v5.1's USM-damped Tenengrad (T1 formula uses
                    # this, not the raw tenengrad_norm). Either field
                    # missing → pre-v6 entry, drop.
                    REQUIRED_V6_FIELDS = (
                        "t1_score", "res_norm", "blockiness",
                        "fft_hf_ratio", "tenengrad_norm",
                        "content_type", "tenengrad_clean",
                    )
                    meta = v.get("metadata") or {}
                    if not all(f in meta for f in REQUIRED_V6_FIELDS):
                        continue
                    expires_at = v.get("expires_at")
                    try:
                        expires_at = float(expires_at) if expires_at is not None else 0.0
                    except (TypeError, ValueError):
                        continue
                    if expires_at > now and isinstance(v.get("score"), (int, float)):
                        cleaned[str(k)] = {
                            "score": float(v["score"]),
                            "metadata": v.get("metadata") or {},
                            "expires_at": expires_at,
                            "schema_version": self.SCHEMA_VERSION,
                        }
                self._state = cleaned
        except (OSError, json.JSONDecodeError):
            return

    def _save_snapshot(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._snapshot_path), exist_ok=True)
            tmp = self._snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self._snapshot_path)
        except OSError:
            pass

    def get(self, site: str, series_url: str) -> Optional[float]:
        if not site or not series_url:
            return None
        key = self._key(site, series_url)
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return None
            if entry.get("expires_at", 0.0) <= time.time():
                self._state.pop(key, None)
                self._save_snapshot()
                return None
            score = entry.get("score")
            return float(score) if isinstance(score, (int, float)) else None

    def get_with_metadata(
        self, site: str, series_url: str
    ) -> Optional[Tuple[float, Dict]]:
        """Like get(), but also returns the cached metadata dict so the
        orchestrator can populate src.img_quality_metadata from cache hits
        (otherwise the UI would lose bpp / is_grayscale / outlier on every
        repeat search). Returns None on miss / expiry / type mismatch.
        """
        if not site or not series_url:
            return None
        key = self._key(site, series_url)
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return None
            if entry.get("expires_at", 0.0) <= time.time():
                self._state.pop(key, None)
                self._save_snapshot()
                return None
            score = entry.get("score")
            if not isinstance(score, (int, float)):
                return None
            metadata = entry.get("metadata") or {}
            return float(score), metadata

    def set(self, site: str, series_url: str, score: float, metadata: Optional[Dict] = None) -> None:
        if not site or not series_url:
            return
        key = self._key(site, series_url)
        with self._lock:
            self._state[key] = {
                "score": max(0.0, min(1.0, float(score))),
                "metadata": metadata or {},
                "expires_at": time.time() + self.ttl_s,
                "schema_version": self.SCHEMA_VERSION,
            }
            self._save_snapshot()


# --- Image-quality scoring ---------------------------------------------
# Format ranking: avif (modern, lossy with high quality) > webp (modern,
# typical quality > jpeg-equivalent at smaller size) > png (lossless,
# overkill for manga) > jpeg (older lossy, often heavily compressed by
# aggregator CDNs) > unknown.
#
# v5 (2026-05-17): _FORMAT_BONUS is no longer used by the active scorer
# (_compute_t1_score uses per-format formula branches instead — see plan).
# Kept alive because _score_image_blob's degraded-fallback path uses it
# when numpy/PIL barf on a malformed image, AND the value still informs
# the UI's "format chip" rendering. Future cleanup (Phase 6 candidate):
# move to a UI-side constant.
_FORMAT_BONUS = {
    "AVIF": 1.0,
    "WEBP": 0.85,
    "PNG": 0.7,
    "JPEG": 0.55,
    "JPG": 0.55,
    "GIF": 0.4,
}


# --- Tier-2 deep-model integration (v5; 2026-05-17) -------------------------
# T2 layers a no-reference IQA neural network on top of T1's objective
# 5-component composite. Per the plan at
# ~/.claude/plans/how-robust-is-the-memoized-koala.md, the intent was
# CLIP-IQA+ with custom manga prompts + NIQE as a complementary photo-IQA
# signal. CLIP-IQA+ in pyiqa 0.1.15 depends on the `clip-by-openai` package
# which has an upstream `pkg_resources.packaging` import bug that's broken
# on Python 3.13 / modern setuptools (verified 2026-05-17). Until that's
# fixed upstream (or we switch to torchmetrics' CLIPImageQualityAssessment
# which has a cleaner dep tree), T2 = NIQE only by default + opt-in ARNIQA.
#
# Empirical justification for NIQE-only default: on real MangaFire pages,
# ARNIQA scores 0.586→0.586→0.587→0.585→0.575→0.525 as JPEG q drops from
# 95→85→70→50→30→15. Almost no discrimination in the practical aggregator
# range (q>=70). NIQE goes 6.69→6.59→6.65→6.91→7.78→9.07 — small but
# monotonic. Both heavy models only diverge meaningfully at q<=30 where
# T1's jpeg_qf component has already done the ranking. NIQE costs ~200ms
# per page with 8KB weights; ARNIQA costs ~1.2s per page with 107MB
# weights. The 6× cost of ARNIQA isn't justified by the marginal signal
# in our operational range — gate it behind AIO_T2_ARNIQA=1 for users
# who want the extra noise reduction.
#
# Cross-file: opt-in is read at import time of _compute_t2_score; not
# re-checked per call. Set AIO_T2_ARNIQA=1 before launching aio-dl.py.
_T2_USE_ARNIQA = os.environ.get("AIO_T2_ARNIQA", "").lower() in ("1", "true", "yes")

# pyiqa availability — lazy-initialized to avoid importing torch (which pyiqa
# pulls transitively) at module load. `_pyiqa_available()` is the only public
# accessor; readers must not check the module-level _PYIQA_AVAILABLE directly
# (it stays None until the first lazy probe). When _ML_RATING_ENABLED is
# False (the default), `_pyiqa_available()` short-circuits to False without
# attempting the import — keeps the torch DLL load path off the search hot
# path entirely. Cross-file: every consumer of pyiqa in this file (NIQE,
# ARNIQA, ST-LPIPS, _compute_t2_score, warmup_t2_models) routes through
# `_pyiqa_available()`.
_pyiqa = None  # type: ignore[assignment]
_PYIQA_AVAILABLE: Optional[bool] = None  # None = unchecked
_PYIQA_IMPORT_ERROR: Optional[str] = None


def _pyiqa_available() -> bool:
    """Lazy pyiqa import + availability check. False (without import) when
    ML rating is disabled. Caches the result so subsequent calls are O(1).
    """
    global _pyiqa, _PYIQA_AVAILABLE, _PYIQA_IMPORT_ERROR
    if not _ML_RATING_ENABLED:
        return False
    if _PYIQA_AVAILABLE is not None:
        return _PYIQA_AVAILABLE
    try:
        import pyiqa as _pi
        _pyiqa = _pi
        _PYIQA_AVAILABLE = True
    except Exception as _e:  # broader than ImportError — pyiqa pulls torch/timm
        _pyiqa = None
        _PYIQA_AVAILABLE = False
        _PYIQA_IMPORT_ERROR = str(_e)
    return _PYIQA_AVAILABLE


# CUDA / device routing — lazy. `_t2_device()` returns "cuda" when torch is
# available AND CUDA is present, else "cpu". Returns "cpu" without importing
# torch when ML rating is disabled. All `_get_*_model` helpers and tensor
# builders go through this; readers must not access `_CUDA_AVAILABLE` or
# `_T2_DEVICE` directly anymore.
_CUDA_AVAILABLE: Optional[bool] = None  # None = unchecked
_T2_DEVICE: Optional[str] = None


def _t2_device() -> str:
    """Return the device string ('cuda' or 'cpu') for torch-backed metrics.
    Lazy — does not import torch unless ML rating is enabled. Caches.
    """
    global _CUDA_AVAILABLE, _T2_DEVICE
    if _T2_DEVICE is not None:
        return _T2_DEVICE
    if not _ML_RATING_ENABLED:
        # Don't import torch just to answer "cpu" for a disabled feature.
        # Don't cache either — re-check if ML rating gets enabled mid-process.
        return "cpu"
    try:
        import torch as _torch
        _CUDA_AVAILABLE = bool(_torch.cuda.is_available())
    except Exception:
        _CUDA_AVAILABLE = False
    _T2_DEVICE = "cuda" if _CUDA_AVAILABLE else "cpu"
    return _T2_DEVICE

# Lazy-init slots. Models are constructed on first use to keep module-load
# cost zero when scoring isn't running (the orchestrator can take minutes
# to fire during search workflows that don't reach the probe phase). Init
# is double-checked-locking; inference is serialized per-model because
# PyTorch nn.Module forward calls are not thread-safe on the same model
# instance from multiple threads. Per-model lock instead of a single
# global so independent metrics can run in parallel within one worker.
_NIQE_MODEL: "Any" = None
_ARNIQA_MODEL: "Any" = None
_STLPIPS_MODEL: "Any" = None  # v5.1 Phase 0: ST-LPIPS via pyiqa for paired comparison.
_CLIP_IQA_MODEL: "Any" = None  # v6 (2026-05-18): CLIP-IQA+ via torchmetrics (NOT pyiqa).
_EASYOCR_READER: "Any" = None  # v5.1 Phase 1: easyocr Reader for watermark detection.
_T2_NIQE_INIT_LOCK = threading.Lock()
_T2_ARNIQA_INIT_LOCK = threading.Lock()
_STLPIPS_INIT_LOCK = threading.Lock()
_CLIP_IQA_INIT_LOCK = threading.Lock()
_EASYOCR_INIT_LOCK = threading.Lock()
_T2_NIQE_INFER_LOCK = threading.Lock()
_T2_ARNIQA_INFER_LOCK = threading.Lock()
_STLPIPS_INFER_LOCK = threading.Lock()
_CLIP_IQA_INFER_LOCK = threading.Lock()
_EASYOCR_INFER_LOCK = threading.Lock()  # easyocr Reader.readtext is not thread-safe.

# v6 (2026-05-18): torchmetrics availability — lazy. Used by
# _get_clip_iqa_model. We DELIBERATELY use torchmetrics'
# CLIPImageQualityAssessment, NOT pyiqa's clip-iqa+: the latter depends on
# the `clip-by-openai` PyPI package, which has a pkg_resources import bug
# broken on Python 3.13 / setuptools 81+. torchmetrics' implementation uses
# transformers' CLIP backend (openai/clip-vit-base-patch32 from
# HuggingFace) — clean dep tree, no pkg_resources usage. Tested working on
# Python 3.13.12. Module read access goes through `_torchmetrics_available()`.
_torchmetrics = None  # type: ignore[assignment]
_TORCHMETRICS_AVAILABLE: Optional[bool] = None  # None = unchecked
_TORCHMETRICS_IMPORT_ERROR: Optional[str] = None


def _torchmetrics_available() -> bool:
    global _torchmetrics, _TORCHMETRICS_AVAILABLE, _TORCHMETRICS_IMPORT_ERROR
    if not _ML_RATING_ENABLED:
        return False
    if _TORCHMETRICS_AVAILABLE is not None:
        return _TORCHMETRICS_AVAILABLE
    try:
        import torchmetrics as _tm
        _torchmetrics = _tm
        _TORCHMETRICS_AVAILABLE = True
    except Exception as _e:
        _torchmetrics = None
        _TORCHMETRICS_AVAILABLE = False
        _TORCHMETRICS_IMPORT_ERROR = str(_e)
    return _TORCHMETRICS_AVAILABLE


# v5.1 Phase 1: easyocr availability — lazy. Watermark detection is
# disabled when False but T1+T2 still work. Pure-pip easyocr ships CRAFT
# internally. Gated on _ML_RATING_ENABLED because easyocr also pulls torch.
_easyocr = None  # type: ignore[assignment]
_EASYOCR_AVAILABLE: Optional[bool] = None  # None = unchecked
_EASYOCR_IMPORT_ERROR: Optional[str] = None


def _easyocr_available() -> bool:
    global _easyocr, _EASYOCR_AVAILABLE, _EASYOCR_IMPORT_ERROR
    if not _ML_RATING_ENABLED:
        return False
    if _EASYOCR_AVAILABLE is not None:
        return _EASYOCR_AVAILABLE
    try:
        import easyocr as _eo
        _easyocr = _eo
        _EASYOCR_AVAILABLE = True
    except Exception as _e:
        _easyocr = None
        _EASYOCR_AVAILABLE = False
        _EASYOCR_IMPORT_ERROR = str(_e)
    return _EASYOCR_AVAILABLE

# v5.1 Phase 1: signaled once watermark detection weights are confirmed
# present on disk. Workers check this before invoking _detect_watermarks;
# unset state → skip watermark check, no penalty applied (graceful degrade).
_WATERMARK_READY = threading.Event()
# Sentinel marking "init was attempted and failed permanently" so future
# calls skip cheaply without re-paying the import / weight-download cost.
_T2_INIT_FAILED = object()
# Signaled once the heaviest model's weights are confirmed present on
# disk. Workers check this before invoking T2 — `False` means weights
# are still downloading (background daemon prefetch) and they should
# return T1-only this cycle to avoid serializing 6 workers behind the
# weight download.
_T2_READY = threading.Event()


def _silence_stdout_to_stderr():
    """DEPRECATED — superseded by the in-place pyiqa.arch_util monkey-patch
    in _patch_pyiqa_load_pretrained_to_stderr. Kept as a no-op context
    manager so existing call sites don't break; new code should not
    introduce additional uses.

    Why it was retired: `contextlib.redirect_stdout` modifies sys.stdout
    process-wide, which broke `--search-json` when the warmup daemon
    thread had the context active while the main thread tried to write
    JSON to stdout (the JSON went to stderr because sys.stdout was
    pointed at sys.stderr by the daemon's context). The monkey-patch
    targets the offending print call directly without touching sys.stdout.
    """
    import contextlib
    @contextlib.contextmanager
    def _noop():
        yield
    return _noop()


def _patch_pyiqa_load_pretrained_to_stderr() -> None:
    """Shadow `print` in pyiqa.archs.arch_util's module namespace so its
    `print(f'Loading pretrained model ...')` writes to stderr instead of
    stdout — without touching builtins or sys.stdout (both of which would
    be unsafe in the multi-threaded warmup-daemon + worker-pool context).

    Why module-namespace shadowing: when Python evaluates `print(...)`
    inside pyiqa.archs.arch_util.load_pretrained_network, name resolution
    walks LEGB — Local → Enclosing → module Global → Builtins. Assigning
    `arch_util.print = _stderr_print` adds a module-level binding that
    shadows the builtin lookup. ONLY pyiqa.archs.arch_util sees the
    redirect; every other module's print() is untouched. This is the
    thread-safe equivalent of contextlib.redirect_stdout for a specific
    function call site.

    The bare print() in pyiqa was corrupting `--search-json` output (which
    requires pure JSON on stdout). The earlier process-wide redirect
    attempt broke JSON output entirely because the warmup daemon's
    sys.stdout swap raced the main thread's JSON write.

    Idempotent: sets `_aio_dl_stderr_print` attribute on the shadow so
    re-imports skip. Wrapped in try/except so any pyiqa internal
    restructuring (e.g. they switch to logging) doesn't break import —
    worst case the original loud print returns.
    """
    try:
        import sys
        import pyiqa.archs.arch_util as _au
    except Exception:
        return
    existing = getattr(_au, "print", None)
    if existing is not None and getattr(existing, "_aio_dl_stderr_print", False):
        return  # already shadowed (idempotent re-import)

    def _stderr_print(*args, **kwargs):
        kwargs.setdefault("file", sys.stderr)
        # `print` builtin returns None — match the signature exactly so
        # any caller checking the return value isn't surprised.
        return __builtins__["print"](*args, **kwargs) if isinstance(__builtins__, dict) \
            else __builtins__.print(*args, **kwargs)

    _stderr_print._aio_dl_stderr_print = True  # type: ignore[attr-defined]
    _au.print = _stderr_print


# 2026-05-20: moved from module-load to first-use inside `_get_niqe_model` /
# `_get_arniqa_model` / `_get_stlpips_model`. The patch is idempotent so
# triple-calling at first-T2-use is harmless; deferring keeps the pyiqa
# import (which transitively imports torch) off the search startup path.


def _get_niqe_model():
    """Lazy-init NIQE via pyiqa. Returns the model or None on failure.

    Pyiqa's NIQE is the real opinion-unaware NIQE from Mittal 2013, not
    BRISQUE — distribution-based, no SVR overfit to LIVE-IQA photos.
    Generalizes to B&W content more gracefully than BRISQUE (whose SVR
    is photo-tuned). 8KB weights, ~200ms per inference on CPU.

    Double-checked locking so the first call from any thread does the
    init and subsequent calls return the cached instance. On any
    exception the sentinel `_T2_INIT_FAILED` is stored — future calls
    return None immediately instead of re-paying the failed-import cost.
    """
    global _NIQE_MODEL
    if _NIQE_MODEL is _T2_INIT_FAILED:
        return None
    if _NIQE_MODEL is not None:
        return _NIQE_MODEL
    if not _pyiqa_available():
        _NIQE_MODEL = _T2_INIT_FAILED
        return None
    # Shadow pyiqa's loud `print` to stderr before any pyiqa model loads.
    # Idempotent — _patch_pyiqa_load_pretrained_to_stderr is safe to call
    # repeatedly. Moved here from module-load (2026-05-20) so the pyiqa
    # import doesn't fire at search startup.
    _patch_pyiqa_load_pretrained_to_stderr()
    with _T2_NIQE_INIT_LOCK:
        # Re-check after acquiring lock (another thread may have raced).
        if _NIQE_MODEL is _T2_INIT_FAILED:
            return None
        if _NIQE_MODEL is not None:
            return _NIQE_MODEL
        try:
            # v5.1: route to _t2_device() (CUDA when available, else CPU).
            # pyiqa's pretrained-load print is redirected to stderr by the
            # `_patch_pyiqa_load_pretrained_to_stderr` module-namespace
            # shadow installed at orchestrator import — no per-call wrap
            # needed.
            model = _pyiqa.create_metric("niqe", device=_t2_device(), as_loss=False)
            # eval() to disable any training-only ops (dropout/batchnorm).
            # NIQE doesn't have these but the call is harmless.
            if hasattr(model, "eval"):
                model.eval()
            _NIQE_MODEL = model
        except Exception:
            _NIQE_MODEL = _T2_INIT_FAILED
            return None
    return _NIQE_MODEL


def _get_arniqa_model():
    """Lazy-init ARNIQA via pyiqa (opt-in via AIO_T2_ARNIQA env var).

    ARNIQA was trained on KADIS-700k's synthetic-distortion manifold —
    JPEG, blur, noise, upscale composed sequentially. The closest published
    NR-IQA training distribution to "what aggregator CDNs do". However on
    real MangaFire data its scores barely move within q=70-95 (the practical
    aggregator range) so we gate it behind an env var: ~1.2s/page × 176
    probe invocations would dominate the probe budget for marginal benefit.

    Returns None when opt-in is off OR weights/import failed.
    """
    if not _T2_USE_ARNIQA:
        return None
    global _ARNIQA_MODEL
    if _ARNIQA_MODEL is _T2_INIT_FAILED:
        return None
    if _ARNIQA_MODEL is not None:
        return _ARNIQA_MODEL
    if not _pyiqa_available():
        _ARNIQA_MODEL = _T2_INIT_FAILED
        return None
    _patch_pyiqa_load_pretrained_to_stderr()
    with _T2_ARNIQA_INIT_LOCK:
        if _ARNIQA_MODEL is _T2_INIT_FAILED:
            return None
        if _ARNIQA_MODEL is not None:
            return _ARNIQA_MODEL
        try:
            # v5.1: route to _t2_device(). pyiqa's pretrained-load print is
            # already redirected to stderr via the module-namespace shadow.
            model = _pyiqa.create_metric("arniqa", device=_t2_device(), as_loss=False)
            if hasattr(model, "eval"):
                model.eval()
            _ARNIQA_MODEL = model
        except Exception:
            _ARNIQA_MODEL = _T2_INIT_FAILED
            return None
    return _ARNIQA_MODEL


def _get_clip_iqa_model():
    """Lazy-init torchmetrics CLIPImageQualityAssessment with manga-tuned
    antonym prompts. Returns the model or None on failure.

    Why torchmetrics over pyiqa.clip-iqa+: pyiqa's CLIP-IQA path depends
    on `clip-by-openai`'s legacy setup.py which imports `pkg_resources`,
    removed in setuptools 81+ (Feb 2026). torchmetrics' implementation
    uses transformers' CLIP backbone (HuggingFace `openai/clip-vit-*`)
    with no `clip-by-openai` dep — clean install on Python 3.13.12.

    Why the default `clip_iqa` model rather than `openai/clip-vit-base-
    patch32`: torchmetrics 1.9.0 has an internal bug when CLIP-IQA's
    anchor-vector path runs against the HF Transformers CLIPModel
    output (it expects raw tensor, gets BaseModelOutputWithPooling).
    The `clip_iqa` default fetches a specially-trained CLIP-IQA
    checkpoint via the LightningAI hub that produces raw tensors —
    that path works. If torchmetrics fixes the HF path upstream, we
    can switch and gain access to the larger CLIP-L variants.

    Custom prompts: a single antonym pair tuned for manga semantics.
    CLIP's pretraining corpus (LAION, WIT) saw millions of anime/manga
    images, so the embedding space already has "clean manga" vs
    "garbage manga" axes accessible by prompt engineering. The score
    is sigmoid(cos(img, pos) − cos(img, neg)) — a calibrated probability
    in [0, 1] of the image matching the positive prompt more than the
    negative.

    First-use cost: ~150 MB weight download from HuggingFace Hub. After
    that, ~0.6 s per inference on CPU at 224×224 input (the metric
    auto-resizes any input to CLIP's fixed 224×224).

    Cross-file: called from _compute_t2_score below; gated by
    _TORCHMETRICS_AVAILABLE. Double-checked locking matches the
    NIQE/ARNIQA/ST-LPIPS pattern.
    """
    global _CLIP_IQA_MODEL
    if _CLIP_IQA_MODEL is _T2_INIT_FAILED:
        return None
    if _CLIP_IQA_MODEL is not None:
        return _CLIP_IQA_MODEL
    if not _torchmetrics_available():
        _CLIP_IQA_MODEL = _T2_INIT_FAILED
        return None
    with _CLIP_IQA_INIT_LOCK:
        if _CLIP_IQA_MODEL is _T2_INIT_FAILED:
            return None
        if _CLIP_IQA_MODEL is not None:
            return _CLIP_IQA_MODEL
        try:
            from torchmetrics.multimodal import CLIPImageQualityAssessment
            model = CLIPImageQualityAssessment(
                prompts=(
                    (
                        "a clean high-quality manga page with sharp lines and clear screentones",
                        "a blurry low-quality compressed manga page",
                    ),
                ),
            )
            # Move to CUDA when available — same _t2_device() convention.
            try:
                model = model.to(_t2_device())
            except Exception:
                pass
            if hasattr(model, "eval"):
                model.eval()
            _CLIP_IQA_MODEL = model
        except Exception:
            _CLIP_IQA_MODEL = _T2_INIT_FAILED
            return None
    return _CLIP_IQA_MODEL


def _get_stlpips_model():
    """Lazy-init ST-LPIPS via pyiqa.stlpips-vgg. Returns the model or None.

    v5.1 Phase 0 addition. ST-LPIPS (Ghildyal & Liu ECCV 2022, "Shift-Tolerant
    Perceptual Similarity Metric") attacks the specific failure mode where two
    images are perceptually identical but differ by small spatial shifts that
    destroy local feature-map similarity in periodic patterns — the screentone
    re-sampling case DISTS-alone mis-handles. Anti-aliased feature maps +
    shift-invariance training objective give near-zero score for shifted
    screentones that DISTS would penalize as legitimate texture variation.

    Used alongside (not instead of) piq.DISTS — the practical timing test on
    1114×1584 manga pages found pyiqa.dists at 4.8 s/inference (16× slower
    than piq.DISTS at 0.3 s), so we keep piq.DISTS as the bulk perceptual
    metric and add ST-LPIPS specifically for the screentone-shift signal it
    uniquely provides. Inputs are downscaled to ~800px max before ST-LPIPS
    inference to keep its 1.3 s/inference at 512×700 within the per-candidate
    budget.

    Cross-file: callable from _compute_paired_perceptual below. Double-checked
    locking + per-model inference lock matches the v5 NIQE/ARNIQA pattern.
    """
    global _STLPIPS_MODEL
    if _STLPIPS_MODEL is _T2_INIT_FAILED:
        return None
    if _STLPIPS_MODEL is not None:
        return _STLPIPS_MODEL
    if not _pyiqa_available():
        _STLPIPS_MODEL = _T2_INIT_FAILED
        return None
    # ST-LPIPS uses VGG16 via torchvision. Fire the deprecated-API patch
    # before model creation so the deprecation warnings don't leak. Patch
    # is idempotent so duplicate calls (also from _get_dists_model) are
    # safe. Moved here from module-load 2026-05-20 as part of the
    # lazy-import refactor — see _ML_RATING_ENABLED docstring.
    _patch_torchvision_deprecated_apis()
    _patch_pyiqa_load_pretrained_to_stderr()
    with _STLPIPS_INIT_LOCK:
        if _STLPIPS_MODEL is _T2_INIT_FAILED:
            return None
        if _STLPIPS_MODEL is not None:
            return _STLPIPS_MODEL
        try:
            # pyiqa's "Loading pretrained model STLPIPS from ..." print is
            # redirected to stderr via the module-namespace shadow installed
            # at orchestrator import — see _patch_pyiqa_load_pretrained_to_stderr.
            model = _pyiqa.create_metric("stlpips-vgg", device=_t2_device(), as_loss=False)
            if hasattr(model, "eval"):
                model.eval()
            _STLPIPS_MODEL = model
        except Exception:
            _STLPIPS_MODEL = _T2_INIT_FAILED
            return None
    return _STLPIPS_MODEL


# --- v5.1 Phase 1: Watermark detection via easyocr -------------------------
#
# Region-based text detection: crop the image to fixed regions (corners,
# edges, center_strip), run easyocr on each crop, count text bboxes. Pages
# with text in legitimate panel speech bubbles (Eleceed mid-panel dialogue,
# manga character speech) avoid triggering because those regions sit in
# the page CENTER (~y/2) which is outside our watermark regions. Pages
# with watermarks ("Read on X", site logos in corners) light up.
#
# Penalty stacks per-region up to WATERMARK_MAX_PENALTY (0.15). Applied
# after T1 composite is computed, before final score. Outlier flag
# `heavy_watermark` set when n_triggered_regions >= 2.
#
# Cross-file: thresholds + region definitions in sites/t1_constants.py.
# Lazy-init follows the same daemon-prefetch pattern as T2 NIQE: weights
# (~200 MB) download once on first use; `_WATERMARK_READY` event gates
# workers so the worker pool doesn't serialize behind the download.

def _get_easyocr_reader():
    """Lazy-init easyocr.Reader for watermark detection.

    Returns the Reader or None on failure. Loading is slow (~17s first
    call: downloads CRAFT detector + recognition model, instantiates
    both on _T2_DEVICE) so it's gated behind `warmup_quality_models`
    daemon prefetch. Once `_WATERMARK_READY.set()` fires the reader is
    safe to use across all worker threads (but inference is serialized
    via `_EASYOCR_INFER_LOCK` because Reader.readtext is not internally
    thread-safe).

    Cross-file: invoked from _detect_watermarks below. The `_EASYOCR_AVAILABLE`
    gate is the import-level fallback; this function adds the runtime-init
    fallback (e.g., CRAFT weight download fails on a captive network).
    """
    global _EASYOCR_READER
    if _EASYOCR_READER is _T2_INIT_FAILED:
        return None
    if _EASYOCR_READER is not None:
        return _EASYOCR_READER
    if not _easyocr_available():
        _EASYOCR_READER = _T2_INIT_FAILED
        return None
    with _EASYOCR_INIT_LOCK:
        if _EASYOCR_READER is _T2_INIT_FAILED:
            return None
        if _EASYOCR_READER is not None:
            return _EASYOCR_READER
        try:
            # English-only — sufficient for the "Read on X" / site-logo
            # watermarks we're targeting. Additional languages would
            # multiply weight-download cost without improving detection
            # of the specific manga/webtoon watermark cases. verbose=False
            # suppresses easyocr's progress prints during init.
            # verbose=False suppresses most of easyocr's prints; the rare
            # "Downloading detection model..." print (which uses urllib's
            # tqdm progress bar) goes to stderr by default. Leaving the
            # call unwrapped — process-wide redirect is unsafe (broke
            # --search-json's JSON write in earlier attempts).
            reader = _easyocr.Reader(
                ["en"], gpu=(_t2_device() == "cuda"), verbose=False,
            )
            _EASYOCR_READER = reader
        except Exception:
            _EASYOCR_READER = _T2_INIT_FAILED
            return None
    return _EASYOCR_READER


def _resolve_region(region_def, img_w: int, img_h: int):
    """Convert a t1_constants.WATERMARK_REGIONS entry into pixel coords.

    Input shape: (x1, y1, x2, y2). Each value may be:
      - int >= 0: absolute pixel offset from origin (top-left)
      - int < 0:  offset from the opposite edge (right or bottom)
      - None:     edge of image (img_w or img_h)
      - str:      "h/2 - 50" / "h/2 + 50" — the center_strip case. We
                  intentionally don't use eval; only these two specific
                  expressions are recognized.

    Returns (x1, y1, x2, y2) as ints, or None when the resolved box is
    degenerate (x2 <= x1 or y2 <= y1) — caller skips that region cleanly.

    Cross-file: consumed by _detect_watermarks below. Region tokens come
    from sites/t1_constants.py:WATERMARK_REGIONS.
    """
    x1, y1, x2, y2 = region_def

    def _resolve_x(v):
        if v is None:
            return img_w
        if isinstance(v, int):
            return v if v >= 0 else max(0, img_w + v)
        # String expressions are not used for x-axis — center_strip only
        # has them on y. Defensive: return None to signal unparseable.
        return None

    def _resolve_y(v):
        if v is None:
            return img_h
        if isinstance(v, int):
            return v if v >= 0 else max(0, img_h + v)
        if isinstance(v, str):
            normalized = v.replace(" ", "")
            if normalized == "h/2-50":
                return max(0, img_h // 2 - 50)
            if normalized == "h/2+50":
                return min(img_h, img_h // 2 + 50)
        return None

    rx1 = _resolve_x(x1)
    rx2 = _resolve_x(x2)
    ry1 = _resolve_y(y1)
    ry2 = _resolve_y(y2)
    if rx1 is None or rx2 is None or ry1 is None or ry2 is None:
        return None
    if rx2 <= rx1 or ry2 <= ry1:
        return None
    # Clamp to image bounds defensively.
    rx1 = max(0, min(img_w, rx1))
    rx2 = max(0, min(img_w, rx2))
    ry1 = max(0, min(img_h, ry1))
    ry2 = max(0, min(img_h, ry2))
    if rx2 - rx1 < 16 or ry2 - ry1 < 16:
        return None  # too small for OCR to see anything
    return rx1, ry1, rx2, ry2


def _easyocr_bbox_to_rect(bbox) -> "Tuple[int, int, int, int]":
    """easyocr returns each detection's bbox as a 4-corner polygon
    [[x1,y1],[x2,y1],[x2,y2],[x1,y2]] (approximately axis-aligned for
    horizontal text). Convert to (x_min, y_min, x_max, y_max) for area
    computation. Robust to slight rotation/perspective via min/max."""
    xs = [int(pt[0]) for pt in bbox]
    ys = [int(pt[1]) for pt in bbox]
    return min(xs), min(ys), max(xs), max(ys)


def _region_has_watermark(
    rects: "List[Tuple[int, int, int, int]]",
    region_area: int,
    content_type: str,
) -> "Tuple[bool, Dict[str, Any]]":
    """Decide whether a region's text-detection bboxes indicate a watermark.

    v5.1 (post-2026-05-17): switched from `reader.readtext()` (full OCR =
    text-DETECT + text-RECOGNIZE) to `reader.detect()` (CRAFT-only,
    bbox-only). The recognizer was the source of the `pin_memory` torch
    warning AND the bulk of inference cost. We don't need recognized text
    for watermark detection; bbox + count + region coverage is sufficient.

    The loss of per-box confidence is handled by setting `text_threshold=
    0.7` at the detect() call site — that's CRAFT's internal confidence
    gate, equivalent to the old `mean_conf >= 0.6` filter on readtext output.

    Inputs:
      rects — list of (x_min, y_min, x_max, y_max) axis-aligned bboxes
      region_area — pixel area of the cropped region (for coverage calc)
      content_type — one of t1_constants's classifications; threshold
                     stricter for manga, looser for webtoons (which
                     legitimately have larger text panels per region).

    Returns (triggered, diagnostic_dict). diagnostic_dict is stashed in
    metadata so the UI tooltip can show "why" without re-running OCR.

    Trigger logic per content type (sans confidence gate, now baked into
    CRAFT's text_threshold parameter at the detect() call site):
      manga (B&W or color):    n_boxes >= 2 AND coverage >= 0.20
      webtoon (chunked / single): n_boxes >= 2 AND coverage >= 0.10

    Rationale (v7, 2026-05-18): manga thresholds were `n>=1 AND cov>=0.05`
    pre-v7, which tripped on routine in-corner content — speech bubbles
    near panel edges, sound effects, page numbers, single-line dialogue,
    or cover title/logo art when the cover-probe fallback ran. Real piracy
    watermarks ("READ ON SITENAME" overlays, URL strips) emit multi-line
    bbox clusters that easily clear `n>=2 AND cov>=0.20`. The manga branch
    stays STRICTER than webtoon (0.20 vs 0.10) because manga panels are
    designed to place content (speech, SFX, page numbers) in the exact
    corner regions we crop; webtoons rarely do.

    Cross-file: thresholds documented in the plan section "Phase 1 —
    Watermark detector" of ~/.claude/plans/how-robust-is-the-memoized-koala.md.
    """
    if not rects or region_area <= 0:
        return False, {"n_boxes": 0, "coverage": 0.0}

    n_boxes = len(rects)
    total_text_area = 0
    for x_min, y_min, x_max, y_max in rects:
        total_text_area += max(0, x_max - x_min) * max(0, y_max - y_min)
    coverage = total_text_area / region_area

    # Content-type-aware thresholds. Manga gets the stricter coverage
    # bar (0.20 vs webtoon's 0.10); see docstring rationale.
    is_webtoon = content_type in ("color_webtoon_chunked", "color_webtoon_single_image")
    if is_webtoon:
        n_threshold = 2
        coverage_threshold = 0.10
    else:
        n_threshold = 2
        coverage_threshold = 0.20

    triggered = n_boxes >= n_threshold and coverage >= coverage_threshold
    return triggered, {
        "n_boxes": int(n_boxes),
        "coverage": round(coverage, 4),
    }


def _detect_watermarks(
    img, content_type: str = "unknown",
) -> "Dict[str, Any]":
    """Run easyocr against each WATERMARK_REGIONS crop and collect triggers.

    Returns metadata dict with shape:
      {
        "watermark_score": float in [0.0, WATERMARK_MAX_PENALTY],
        "watermark_regions": List[Dict],   # per-triggered-region details
        "watermark_detector_used": "easyocr" | None,
      }

    Score = min(WATERMARK_MAX_PENALTY, n_triggered_regions * WATERMARK_PER_REGION_PENALTY).
    Applied as a negative adjustment to T1 in `_score_image_blob`.

    When easyocr isn't available OR `_WATERMARK_READY` isn't set yet, returns
    {"watermark_score": None, ...}. None is the "we didn't measure" signal;
    callers (the T1 final-composite path) skip the penalty when score is None.
    True 0.0 score = "we checked, no watermark detected". Both states have
    distinct cache representations so the v5.1 → v6.x transition can
    differentiate.

    Cross-file: invoked from _score_image_blob after the T1 composite is
    computed but BEFORE the T2 blend so the watermark penalty applies to
    the T1 base. The watermark detection cost (~5-15 ms × 7 regions =
    ~70-100 ms per page on CPU, ~15-25 ms on GPU) is comparable to T1
    itself and fits inside the existing probe budget.
    """
    from . import t1_constants as _t1c

    if not _easyocr_available() or not _WATERMARK_READY.is_set():
        return {
            "watermark_score": None,
            "watermark_regions": [],
            "watermark_detector_used": None,
        }

    reader = _get_easyocr_reader()
    if reader is None:
        return {
            "watermark_score": None,
            "watermark_regions": [],
            "watermark_detector_used": None,
        }

    import numpy as np
    try:
        img_arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    except Exception:
        return {
            "watermark_score": None,
            "watermark_regions": [],
            "watermark_detector_used": None,
        }
    img_h, img_w = img_arr.shape[:2]
    if img_h < 200 or img_w < 200:
        # Image too small for region-based watermark detection — the
        # corner crops (200x150) wouldn't even fit.
        return {
            "watermark_score": 0.0,
            "watermark_regions": [],
            "watermark_detector_used": "easyocr",
        }

    triggered_regions: List[Dict[str, Any]] = []
    for region_name, region_def in _t1c.WATERMARK_REGIONS.items():
        resolved = _resolve_region(region_def, img_w, img_h)
        if resolved is None:
            continue
        rx1, ry1, rx2, ry2 = resolved
        crop = img_arr[ry1:ry2, rx1:rx2]
        if crop.size == 0:
            continue
        # v5.1: use reader.detect() (CRAFT-only) instead of readtext (CRAFT
        # + recognizer). detect() returns axis-aligned `horizontal_list`
        # and free-form polygon `free_list` of bboxes — no recognized text,
        # no confidence per bbox. text_threshold=0.7 sets CRAFT's internal
        # confidence gate (equivalent to the prior `mean_conf >= 0.6`
        # filter on readtext output). Skipping the recognizer also avoids
        # the torch DataLoader pin_memory warning emitted by easyocr's
        # batched recognition path on CPU-only torch.
        rects: List[Tuple[int, int, int, int]] = []
        with _EASYOCR_INFER_LOCK:
            try:
                horizontal_list, free_list = reader.detect(
                    crop, text_threshold=0.7, low_text=0.4,
                    link_threshold=0.4,
                )
            except Exception:
                continue
        # horizontal_list is a list per input image; we passed one image,
        # so it's a list of one element holding the axis-aligned bboxes.
        # Each bbox is [x_min, x_max, y_min, y_max] (note unusual order).
        try:
            for bbox in (horizontal_list[0] if horizontal_list else []):
                if len(bbox) < 4:
                    continue
                x_min, x_max, y_min, y_max = (
                    int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]),
                )
                rects.append((x_min, y_min, x_max, y_max))
            # free_list contains rotated/free-form polygons. Convert to
            # axis-aligned bboxes via min/max so they participate in the
            # coverage calculation.
            for poly in (free_list[0] if free_list else []):
                if not poly or len(poly) < 3:
                    continue
                xs = [int(pt[0]) for pt in poly]
                ys = [int(pt[1]) for pt in poly]
                rects.append((min(xs), min(ys), max(xs), max(ys)))
        except (IndexError, TypeError, ValueError):
            # Defensive: easyocr's return shape can vary by version. Skip
            # the region cleanly rather than crash.
            continue
        triggered, diag = _region_has_watermark(
            rects, region_area=(ry2 - ry1) * (rx2 - rx1),
            content_type=content_type,
        )
        if triggered:
            triggered_regions.append({
                "region": region_name,
                **diag,
            })

    n_triggered = len(triggered_regions)
    watermark_score = min(
        _t1c.WATERMARK_MAX_PENALTY,
        n_triggered * _t1c.WATERMARK_PER_REGION_PENALTY,
    )
    return {
        "watermark_score": round(float(watermark_score), 4),
        "watermark_regions": triggered_regions,
        "watermark_detector_used": "easyocr",
    }


# --- Tier-3 paired DISTS comparison (v5; 2026-05-17) ------------------------
# T3 is cross-source paired image-quality comparison: when 2+ measured sources
# exist for the same series, fetch the same chapter from each and compute
# DISTS (Ding 2020, texture-resampling-robust FR-IQA — explicitly designed
# to be invariant under the screentone-translation property that breaks
# SSIM on manga). Median DISTS to anchor → paired_quality_adjustment added
# in-place to img_quality_score.
#
# Why DISTS specifically: per the research report
# (~/.claude/plans/how-robust-is-the-memoized-koala-agent-a42650755ce151e5a.md
# §6), DISTS is the only paired-compare metric that's robust to screentone
# resampling artifacts. SSIM/LPIPS misfire on manga because screentones are
# perceptually translation-invariant (a shifted dot pattern looks identical
# to humans but has near-zero pixel correlation, crashing pixel-aligned
# metrics).
#
# Cross-file: invoked from search_all post-probe-phase. piq dep is gated;
# T3 cleanly degrades to no-op when piq or cv2 isn't available.

_piq = None  # type: ignore[assignment]
_PIQ_AVAILABLE: Optional[bool] = None  # None = unchecked
_PIQ_IMPORT_ERROR: Optional[str] = None


def _piq_available() -> bool:
    """Lazy piq import. Gated on ML rating because piq pulls torch."""
    global _piq, _PIQ_AVAILABLE, _PIQ_IMPORT_ERROR
    if not _ML_RATING_ENABLED:
        return False
    if _PIQ_AVAILABLE is not None:
        return _PIQ_AVAILABLE
    try:
        import piq as _pq
        _piq = _pq
        _PIQ_AVAILABLE = True
    except Exception as _e:
        _piq = None
        _PIQ_AVAILABLE = False
        _PIQ_IMPORT_ERROR = str(_e)
    return _PIQ_AVAILABLE


# cv2 (OpenCV) — used by T3's ORB+RANSAC alignment, doesn't pull torch.
# Lazy for module-load consistency but NOT gated on _ML_RATING_ENABLED.
# When ML rating is off, T3 is skipped anyway (because _piq_available()
# returns False), so cv2's availability becomes moot.
_cv2 = None  # type: ignore[assignment]
_CV2_AVAILABLE: Optional[bool] = None  # None = unchecked
_CV2_IMPORT_ERROR: Optional[str] = None


def _cv2_available() -> bool:
    global _cv2, _CV2_AVAILABLE, _CV2_IMPORT_ERROR
    if _CV2_AVAILABLE is not None:
        return _CV2_AVAILABLE
    try:
        import cv2 as _opencv
        _cv2 = _opencv
        _CV2_AVAILABLE = True
    except Exception as _e:
        _cv2 = None
        _CV2_AVAILABLE = False
        _CV2_IMPORT_ERROR = str(_e)
    return _CV2_AVAILABLE

# DISTS lazy-init (PyTorch model with VGG16 backbone, weights downloaded on
# first use). ~0.3s per inference on CPU, much faster than ARNIQA. Per-model
# inference lock because PyTorch nn.Module isn't thread-safe on a shared
# instance.
_DISTS_MODEL: "Any" = None
_DISTS_INIT_LOCK = threading.Lock()
_DISTS_INFER_LOCK = threading.Lock()


def _get_dists_model():
    """Lazy-init DISTS via piq. Returns the model or None on failure."""
    global _DISTS_MODEL
    if _DISTS_MODEL is _T2_INIT_FAILED:
        return None
    if _DISTS_MODEL is not None:
        return _DISTS_MODEL
    if not _piq_available():
        _DISTS_MODEL = _T2_INIT_FAILED
        return None
    # piq.DISTS uses VGG16 via torchvision. Fire the deprecated-API patch
    # before model creation so the deprecation warnings don't leak. Patch
    # is idempotent; safe even if _get_stlpips_model also called it.
    _patch_torchvision_deprecated_apis()
    with _DISTS_INIT_LOCK:
        if _DISTS_MODEL is _T2_INIT_FAILED:
            return None
        if _DISTS_MODEL is not None:
            return _DISTS_MODEL
        try:
            # piq.DISTS() returns a callable nn.Module; weights download once.
            # torchvision's download progress goes to stderr via tqdm.
            model = _piq.DISTS()
            if hasattr(model, "eval"):
                model.eval()
            _DISTS_MODEL = model
        except Exception:
            _DISTS_MODEL = _T2_INIT_FAILED
            return None
    return _DISTS_MODEL


# Per-candidate wall-time cap (seconds). T3 is expensive so we strictly bound
# it. When a candidate has 5+ sources and the chapter alignments are dense,
# the work can blow up. v5.1 (2026-05-17): bumped from 10s to 30s after
# live testing showed ORB-fallback alignment + ST-LPIPS inference make
# each pair take ~5-7s; 10s budget produced only 1 pair per non-anchor
# source, which is too noisy for the +/-0.15 adjustment to be reliable.
# 30s gives 4-5 pairs per source — enough median samples for a robust
# adjustment.
PAIRED_PER_CANDIDATE_BUDGET_S = 30.0

# Total wall-time cap across all candidates. Separate from
# PROBE_PHASE_DEADLINE_S because T3 runs AFTER the probe phase joins;
# we want a separate budget so a slow probe doesn't starve T3 (or vice
# versa).
PAIRED_COMPARISON_BUDGET_S = 60.0

# Number of shared chapters to sample per candidate. 3 is the sweet spot per
# the research §6.6: within-chapter DISTS variance for same-source-different-
# recompress is low, so 3 pairs give a stable median; more is wasteful.
PAIRED_CHAPTERS_PER_CANDIDATE = 3

# Maximum number of sources to include in paired comparison per candidate.
# Top 3 by score keeps the work bounded (3 sources × 1 anchor = 2 non-anchor
# DISTS computations × 3 chapters = 6 DISTS calls per candidate max).
PAIRED_TOP_SOURCES = 3

# Minimum measured score for a source to qualify for T3. Sources below this
# floor are too broken to make a meaningful paired comparison — DISTS on a
# corrupt source would just be high (= penalty) regardless of anchor.
PAIRED_MIN_SOURCE_SCORE = 0.3

# Maximum |adjustment| applied to img_quality_score from T3. Caps the
# influence of paired comparison so a single noisy DISTS measurement can't
# completely flip a ranking — composite img_quality_score after T3 stays
# within ±0.15 of the T1+T2 baseline.
PAIRED_MAX_ADJUSTMENT = 0.15


def _phase_correlate_translation(img_a_gray, img_b_gray) -> "Tuple[float, float, float]":
    """Estimate the translation (dx, dy) that aligns img_b to img_a via
    cv2.phaseCorrelate. Returns (dx, dy, response) — response is the
    confidence (0-1, higher = more reliable alignment).

    Both inputs must be float32 numpy arrays of the same shape and grayscale.
    """
    import numpy as np
    a32 = img_a_gray.astype(np.float32)
    b32 = img_b_gray.astype(np.float32)
    (dx, dy), response = _cv2.phaseCorrelate(a32, b32)
    return float(dx), float(dy), float(response)


def _align_image_pair(blob_anchor: bytes, blob_target: bytes,
                       target_dim_cap: int = 1200) -> "Optional[Tuple[Any, Any]]":
    """v5.1 alignment with 3-tier fallback chain. See _align_image_pair_v51
    for the actual implementation; this wrapper preserves the 2-tuple
    return signature for back-compat with v5 callers.

    Returns (anchor_rgb, target_rgb) numpy uint8 arrays, or None on failure.
    """
    result = _align_image_pair_v51(blob_anchor, blob_target, target_dim_cap)
    if result is None:
        return None
    arr_a, arr_b, _method = result
    return arr_a, arr_b


def _align_image_pair_v51(
    blob_anchor: bytes, blob_target: bytes, target_dim_cap: int = 1200,
) -> "Optional[Tuple[Any, Any, str]]":
    """v5.1 Phase 3b: 3-tier alignment with method-name return.

    Tier 1 — phaseCorrelate (fast, translation-only). Used when its
        response confidence ≥ 0.5.
    Tier 2 — ECC pyramid (affine: small scale + rotation). Falls back
        when phaseCorrelate confidence is too low (0.2-0.5 band).
    Tier 3 — ORB + RANSAC homography (handles crop, heavy rotation).
        Final fallback when ECC convergence fails.

    Returns (anchor_arr, target_arr, alignment_method_name) where method is
    one of "phase_correlate", "ecc_pyramid", "orb_ransac". None when all
    three tiers fail.

    The Tier 1 path is unchanged from v5 (only the threshold bumped from
    0.2 to 0.5). When confidence is 0.2-0.5, v5 would skip the pair; v5.1
    tries ECC instead. v5's < 0.2 path stays a skip until ECC, which can
    sometimes recover heavily-warped pairs.

    Cross-file: consumed by _compute_paired_perceptual. cv2.findTransformECC
    and cv2.findHomography come from the cv2 contrib build (available in
    standard opencv-python >= 4.5).
    """
    if not _cv2_available():
        return None
    import numpy as np
    try:
        from PIL import Image as _PILImage
        import io as _io
        img_a = _PILImage.open(_io.BytesIO(blob_anchor))
        img_a.load()
        img_b = _PILImage.open(_io.BytesIO(blob_target))
        img_b.load()
    except Exception:
        return None
    if img_a.size[0] < 100 or img_b.size[0] < 100:
        return None

    # Unify resolution by Lanczos downscale.
    target_h = min(img_a.size[1], img_b.size[1], target_dim_cap)
    sa = target_h / img_a.size[1]
    sb = target_h / img_b.size[1]

    def _resize_lanczos(im, scale):
        w = max(1, int(im.size[0] * scale))
        h = max(1, int(im.size[1] * scale))
        return im.resize((w, h), _PILImage.LANCZOS)

    img_a_rs = _resize_lanczos(img_a, sa).convert("RGB")
    img_b_rs = _resize_lanczos(img_b, sb).convert("RGB")
    arr_a = np.asarray(img_a_rs, dtype=np.uint8)
    arr_b = np.asarray(img_b_rs, dtype=np.uint8)
    H = min(arr_a.shape[0], arr_b.shape[0])
    W = min(arr_a.shape[1], arr_b.shape[1])
    if H < 100 or W < 100:
        return None
    arr_a = arr_a[:H, :W]
    arr_b = arr_b[:H, :W]

    gray_a = np.dot(arr_a[..., :3], [0.299, 0.587, 0.114]).astype(np.float32)
    gray_b = np.dot(arr_b[..., :3], [0.299, 0.587, 0.114]).astype(np.float32)

    # --- Tier 1: phaseCorrelate (translation-only, fast) ---
    try:
        dx, dy, response = _phase_correlate_translation(gray_a, gray_b)
    except Exception:
        dx, dy, response = 0.0, 0.0, 0.0

    if response >= 0.5:
        if abs(dx) + abs(dy) > 1.0:
            M = np.array([[1.0, 0.0, -dx], [0.0, 1.0, -dy]], dtype=np.float32)
            arr_b = _cv2.warpAffine(arr_b, M, (W, H), borderMode=_cv2.BORDER_REFLECT)
        pad = max(2, int(max(abs(dx), abs(dy)) + 2))
        if H - 2 * pad < 100 or W - 2 * pad < 100:
            return None
        arr_a_out = arr_a[pad:H - pad, pad:W - pad]
        arr_b_out = arr_b[pad:H - pad, pad:W - pad]
        return arr_a_out, arr_b_out, "phase_correlate"

    # --- Tier 2: ECC pyramid (affine — handles small scale + rotation) ---
    if response >= 0.05:  # has SOME phase correlation, try ECC refinement
        try:
            gray_a_u8 = gray_a.astype(np.uint8)
            gray_b_u8 = gray_b.astype(np.uint8)
            warp_matrix = np.eye(2, 3, dtype=np.float32)
            criteria = (_cv2.TERM_CRITERIA_COUNT + _cv2.TERM_CRITERIA_EPS, 50, 1e-4)
            _, warp_matrix = _cv2.findTransformECC(
                gray_a_u8, gray_b_u8, warp_matrix,
                _cv2.MOTION_AFFINE, criteria,
                None, 5,  # input_mask, gaussFilterSize
            )
            arr_b_warp = _cv2.warpAffine(
                arr_b, warp_matrix, (W, H), borderMode=_cv2.BORDER_REFLECT,
            )
            # Crop a conservative margin (pixels that may have come from
            # the reflected border or out-of-bounds source).
            pad = 8
            if H - 2 * pad < 100 or W - 2 * pad < 100:
                return None
            return arr_a[pad:H - pad, pad:W - pad], arr_b_warp[pad:H - pad, pad:W - pad], "ecc_pyramid"
        except _cv2.error:
            pass
        except Exception:
            pass

    # --- Tier 3: ORB + RANSAC homography (crop + rotation tolerant) ---
    try:
        gray_a_u8 = gray_a.astype(np.uint8)
        gray_b_u8 = gray_b.astype(np.uint8)
        orb = _cv2.ORB_create(nfeatures=2000)
        kp_a, des_a = orb.detectAndCompute(gray_a_u8, None)
        kp_t, des_t = orb.detectAndCompute(gray_b_u8, None)
        if des_a is None or des_t is None or len(kp_a) < 10 or len(kp_t) < 10:
            return None
        bf = _cv2.BFMatcher(_cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(des_a, des_t)
        if len(matches) < 10:
            return None
        # Note: src/dst convention — homography maps target → anchor space.
        src_pts = np.float32([kp_t[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp_a[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
        H_mat, mask = _cv2.findHomography(src_pts, dst_pts, _cv2.RANSAC, 5.0)
        if H_mat is None or mask is None or int(mask.sum()) < 8:
            return None
        arr_b_warp = _cv2.warpPerspective(
            arr_b, H_mat, (W, H), borderMode=_cv2.BORDER_REFLECT,
        )
        pad = 10
        if H - 2 * pad < 100 or W - 2 * pad < 100:
            return None
        return arr_a[pad:H - pad, pad:W - pad], arr_b_warp[pad:H - pad, pad:W - pad], "orb_ransac"
    except Exception:
        return None


# v5.1 Phase 0: input-size cap for ST-LPIPS inference. Empirically pyiqa
# ST-LPIPS at 1114×1584 costs ~6 s/inference; downscale to ~800px max edge
# brings it to ~1.3 s (verified 2026-05-17). piq.DISTS is fast at any size
# (~0.3 s) and runs at the full aligned resolution to preserve its
# screentone-resampling-vulnerability signal.
PAIRED_STLPIPS_MAX_EDGE = 800


def _resize_for_stlpips(arr):
    """Downscale a numpy HWC uint8 image to PAIRED_STLPIPS_MAX_EDGE on its
    longer axis using PIL Lanczos. Returns the original array unchanged if
    already smaller. Used only for the ST-LPIPS branch so we keep its
    per-inference cost bounded; piq.DISTS sees the full aligned pair.
    """
    import numpy as np
    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest <= PAIRED_STLPIPS_MAX_EDGE:
        return arr
    scale = PAIRED_STLPIPS_MAX_EDGE / longest
    new_h = max(1, int(round(h * scale)))
    new_w = max(1, int(round(w * scale)))
    from PIL import Image as _PIL
    img = _PIL.fromarray(arr)
    return np.asarray(img.resize((new_w, new_h), _PIL.LANCZOS), dtype=np.uint8)


def _arr_to_tensor(arr):
    """Convert a numpy HWC uint8 array to a torch tensor (1, 3, H, W) float in
    [0, 1] on _t2_device(). Used by paired metrics."""
    import torch
    # .copy() forces writable array (cv2.warpAffine can produce read-only views).
    t = torch.from_numpy(arr.copy()).permute(2, 0, 1).unsqueeze(0).float() / 255.0
    device = _t2_device()
    if device != "cpu":
        try:
            t = t.to(device)
        except Exception:
            pass
    return t


# --- v5.1 Phase 3a: JPEG-ghost double-compression detection ---------------
# Algorithm (Farid IEEE TIFS 2009, "Exposing Digital Forgeries from JPEG
# Ghosts"): re-encode the target at a sweep of quality factors. A
# natively-encoded source has very low SSD at its NATIVE QF (the encoder
# is idempotent at the same quality). A double-compressed source has
# elevated SSD across the sweep — characteristic "ghost" pattern from
# the first encoding's quantization-table residue.
#
# Score is per-target (not paired against anchor) so it contributes T3
# signal even when alignment fails completely. Combined adjustment:
#   paired_adjustment = clamp(-0.15, +0.15,
#                             -0.30 * ensemble - 0.10 * ghost_score)
# When ensemble (alignment-dependent) is unavailable, ghost alone caps
# at -0.10 contribution.

GHOST_QF_SWEEP = (50, 60, 70, 80, 85, 90, 95)
GHOST_BLOCK_SIZE = 64
GHOST_SSD_NORMALIZATION = 50.0  # Calibrated against tmp_nzmj/tmp_1571.


def _compute_jpeg_ghost(target_blob: bytes) -> "Tuple[Optional[float], Dict[str, Any]]":
    """Detect JPEG double-compression via the Farid ghost-residuals method.

    Re-encode the target at a sweep of quality factors; the per-block SSD
    minimum is small when the source was natively encoded (the codec is
    idempotent at the matching QF) and elevated when the source was
    re-encoded from a different QF (the first compression's quantization
    leaves a residual that all sweep points reveal).

    Returns (ghost_score, metadata) where ghost_score is the residual
    minimum normalized to [0, 1+] via GHOST_SSD_NORMALIZATION. Score
    near 0 = natively-encoded clean source; score > 0.3 = clear
    double-compression. Returns (None, ...) on decode failure or
    non-JPEG input (caller checks the format before invoking).

    Cost: ~7 re-encodes × ~30 ms = ~210 ms / call on CPU. The QF sweep
    skips 5-QF granularity (50/60/70/80/85/90/95 instead of every-5
    50-95) to keep cost bounded while still resolving the typical
    aggregator QF range.

    Cross-file: invoked from _run_paired_comparison per target source
    (independent of paired alignment, runs as a separate signal slot).
    """
    if not target_blob or len(target_blob) < 256:
        return None, {"ghost_score": None}
    try:
        from PIL import Image as _PILImage
        import io as _io
        import numpy as np
        img = _PILImage.open(_io.BytesIO(target_blob))
        img.load()
        if img.format not in ("JPEG", "JPG"):
            return None, {"ghost_score": None, "skipped_reason": "non_jpeg"}
        # Force RGB conversion so the residual math works regardless of
        # PIL's internal mode (some L-mode JPEGs need RGB for accurate
        # re-encoding comparison).
        target_arr = np.asarray(img.convert("RGB"), dtype=np.int32)
    except Exception:
        return None, {"ghost_score": None, "skipped_reason": "decode_failure"}

    h, w = target_arr.shape[:2]
    if h < GHOST_BLOCK_SIZE * 2 or w < GHOST_BLOCK_SIZE * 2:
        # Too small for meaningful block-SSD analysis.
        return None, {"ghost_score": None, "skipped_reason": "too_small"}

    residuals: List[Tuple[int, float]] = []
    for qf in GHOST_QF_SWEEP:
        try:
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=int(qf))
            recoded = _PILImage.open(_io.BytesIO(buf.getvalue()))
            recoded.load()
            recoded_arr = np.asarray(recoded.convert("RGB"), dtype=np.int32)
        except Exception:
            continue
        if recoded_arr.shape != target_arr.shape:
            continue
        # Per-block SSD (averaged within 64×64 blocks, then MEDIAN across
        # blocks). Per-block mean smooths pixel-level noise; the across-block
        # median is the robust outlier-rejecting statistic the Farid JPEG-
        # ghost method calls for. The previous reduction (`block_means.mean()`)
        # was mathematically equal to `diff_sq.mean()` (uniform block sizes
        # → mean of means = global mean), defeating the per-block grouping's
        # entire purpose: border anti-aliasing / CMS profile-conversion
        # ringing produces a handful of high-residual outlier blocks that
        # dominated the score even on un-recompressed images. Median floors
        # the score at the structural mid-block residual the method actually
        # wants to measure.
        diff_sq = (target_arr - recoded_arr) ** 2
        # Truncate to block-aligned dimensions to avoid partial blocks.
        bh = (h // GHOST_BLOCK_SIZE) * GHOST_BLOCK_SIZE
        bw = (w // GHOST_BLOCK_SIZE) * GHOST_BLOCK_SIZE
        trimmed = diff_sq[:bh, :bw]
        # Reshape into (nblocks_y, GHOST_BLOCK_SIZE, nblocks_x, GHOST_BLOCK_SIZE, channels)
        # then mean over the 64×64 block + channel axes — yields one SSD
        # value per 64×64 block of the page.
        n_by = bh // GHOST_BLOCK_SIZE
        n_bx = bw // GHOST_BLOCK_SIZE
        blocks = trimmed.reshape(n_by, GHOST_BLOCK_SIZE, n_bx, GHOST_BLOCK_SIZE, -1)
        block_means = blocks.mean(axis=(1, 3, 4))
        ssd = float(np.median(block_means))
        residuals.append((int(qf), ssd))

    if not residuals:
        return None, {"ghost_score": None, "skipped_reason": "all_encodes_failed"}

    min_qf, min_ssd = min(residuals, key=lambda t: t[1])
    # Ghost score: native sources have min_ssd ~ 5-20 (small residual at
    # matched QF); recompressed sources have min_ssd ~ 50-150 (elevated
    # across the sweep). Map (min_ssd / GHOST_SSD_NORMALIZATION) to ~[0, 1+].
    ghost_score = max(0.0, float(min_ssd / GHOST_SSD_NORMALIZATION))
    return ghost_score, {
        "ghost_score": round(ghost_score, 4),
        "min_qf": int(min_qf),
        "min_ssd": round(float(min_ssd), 2),
        "residuals": [(qf, round(s, 2)) for qf, s in residuals],
    }


def _compute_paired_perceptual(
    blob_anchor: bytes, blob_target: bytes,
) -> "Optional[Tuple[float, float, float, str]]":
    """Score two aligned image blobs via the v5.1 paired perceptual ensemble.

    Returns (dists_score, stlpips_score, ensemble_score) where each is
    [0, 1+] with lower=better, or None on alignment / inference failure.

      ensemble_score = 0.5 * dists_score + 0.5 * stlpips_score

    The 50/50 ensemble gives DISTS's general perceptual + JPEG-compression
    sensitivity plus ST-LPIPS's specific shift-tolerance for screentone
    re-sampling artifacts. The downstream T3 adjustment formula uses the
    ensemble (`-0.30 * ensemble`) so the total weight matches v5's
    DISTS-only `-0.30 * dists_median` — no comparator re-tuning needed.

    Why both metrics (not just ST-LPIPS): empirical timing on 1114×1584
    pages found piq.DISTS at 0.3 s vs pyiqa.dists at 4.8 s (16×). The
    plan's "drop piq" goal is impractical at production scale; we keep
    the fast piq.DISTS for the bulk perceptual signal and add ST-LPIPS
    (which exists only in pyiqa) for the orthogonal screentone-shift
    signal that DISTS doesn't have. ST-LPIPS input is downscaled to
    PAIRED_STLPIPS_MAX_EDGE to keep its inference time at ~1.3 s instead
    of ~6 s.

    Failures are silent (return None). Callers — typically
    `_run_paired_comparison` — treat None as "skip this pair".

    Cross-file: replaces v5's `_compute_dists_pair`. Both piq and pyiqa
    must be available (the existing _piq_available() + _pyiqa_available()
    gates already guard).
    """
    if not _piq_available() or not _cv2_available():
        return None
    dists = _get_dists_model()
    if dists is None:
        return None
    # v5.1 Phase 3b: 3-tier alignment. Returns (anchor, target, method)
    # where method ∈ {"phase_correlate", "ecc_pyramid", "orb_ransac"}.
    aligned_v51 = _align_image_pair_v51(blob_anchor, blob_target)
    if aligned_v51 is None:
        return None
    arr_a, arr_b, alignment_method = aligned_v51

    import torch

    # piq.DISTS at full aligned resolution — fast enough not to need downscale.
    a_tensor = _arr_to_tensor(arr_a)
    b_tensor = _arr_to_tensor(arr_b)
    dists_score: Optional[float] = None
    with _DISTS_INFER_LOCK:
        try:
            with torch.no_grad():
                d = dists(a_tensor, b_tensor)
            dists_score = float(d.item()) if hasattr(d, "item") else float(d)
        except Exception:
            dists_score = None

    if dists_score is None:
        return None

    # ST-LPIPS at downscaled resolution — gated by PYIQA availability +
    # successful model init. When ST-LPIPS isn't available (weight download
    # failed, pyiqa missing, etc.), we degrade to DISTS-only and the ensemble
    # collapses to dists_score (caller still gets a meaningful comparison).
    stlpips_score: Optional[float] = None
    stlpips_model = _get_stlpips_model()
    if stlpips_model is not None:
        arr_a_small = _resize_for_stlpips(arr_a)
        arr_b_small = _resize_for_stlpips(arr_b)
        # Inputs to ST-LPIPS must be the same size; _resize_for_stlpips
        # uses the same scale ratio for both arrays so shapes already match.
        a_small_tensor = _arr_to_tensor(arr_a_small)
        b_small_tensor = _arr_to_tensor(arr_b_small)
        with _STLPIPS_INFER_LOCK:
            try:
                with torch.no_grad():
                    s = stlpips_model(a_small_tensor, b_small_tensor)
                stlpips_score = float(s.item()) if hasattr(s, "item") else float(s)
            except Exception:
                stlpips_score = None

    if stlpips_score is None:
        # ST-LPIPS unavailable; ensemble degrades to DISTS-alone. Returning the
        # same scalar as both `stlpips_score` and `ensemble_score` preserves
        # the 4-tuple shape so callers don't need null-handling per slot —
        # they read `ensemble_score` and treat the diagnostic slots as bonus
        # info when present.
        return dists_score, dists_score, dists_score, alignment_method

    ensemble_score = 0.5 * dists_score + 0.5 * stlpips_score
    return dists_score, stlpips_score, ensemble_score, alignment_method


# Back-compat alias for any callers still using the v5 name. Renamed in v5.1
# because the ensemble's return shape changed (Optional[float] →
# Optional[Tuple[float, float, float]]); a stub-style alias would just hide
# the shape change. Existing tests will be updated to use the new name. If
# external code grepped for `_compute_dists_pair`, the migration is to call
# `_compute_paired_perceptual(...)` and use the third tuple slot for the old
# DISTS-only behavior, or the first slot for diagnostic per-metric value.
def _compute_dists_pair(blob_anchor: bytes, blob_target: bytes) -> Optional[float]:
    """DEPRECATED v5 wrapper retained for any external test code. Returns
    the ensemble score (the v5.1 successor) so the scalar contract still
    works in the rare case external code expected a single float. Prefer
    `_compute_paired_perceptual` for new code (returns all four diagnostic
    slots including the alignment method).
    """
    result = _compute_paired_perceptual(blob_anchor, blob_target)
    if result is None:
        return None
    # 4-tuple: (dists, stlpips, ensemble, alignment_method) — back-compat
    # wrapper discards stlpips + method, returns the ensemble scalar.
    _, _, ensemble, _ = result
    return ensemble


# --- v6 pairwise ranking constants (2026-05-18) ---------------------------
# Aliases that document the new T3 design. Reuse the v5.1 PAIRED_* values
# so external constants importers keep working.
PAIRWISE_MAX_ADJ = PAIRED_MAX_ADJUSTMENT
PAIRWISE_TOP_SOURCES = PAIRED_TOP_SOURCES
PAIRWISE_CHAPTERS_PER_BUCKET = PAIRED_CHAPTERS_PER_CANDIDATE
PAIRWISE_BUDGET_S = PAIRED_COMPARISON_BUDGET_S
PAIRWISE_PER_CANDIDATE_BUDGET_S = PAIRED_PER_CANDIDATE_BUDGET_S
PAIRWISE_MIN_SOURCE_SCORE = PAIRED_MIN_SOURCE_SCORE


def _compute_pairwise_components(
    blob: bytes, content_type: str,
) -> "Optional[Dict[str, Optional[float]]]":
    """Build the per-page T1 component vector used by _run_pairwise_ranking.

    Returns a dict mapping component_key → scalar in [0, 1] (or None when
    that component isn't available for the given content type). All keys
    are always present in the returned dict so callers can iterate
    COMPONENT_KEYS without defensive checks.

    Component schema (higher = better for all keys):
      res_norm        — area / target ratio with v6 decay
      qf_or_lossless  — JPEG QF normalized to [0, 1], or 1.0 for lossless
      screentone      — bw_signals.compute_screentone_integrity score (B&W only)
      line            — bw_signals.compute_line_quality score (B&W only)
      bg_uniformity   — bw_signals.compute_bw_storage_signals[bg_uniformity] (B&W only)
      block_inv       — 1 − Wang-Bovik blockiness
      fft_hf          — _compute_fft_hf_ratio (color/unknown only)
      tenengrad       — _compute_tenengrad's USM-cleaned norm

    On decode failure or undersized images, returns None and the caller
    skips this (source, chapter) entry.

    Cross-file: invoked from _run_pairwise_ranking below. Dispatches
    through _compute_t1_score_bw (B&W) or _compute_t1_score (color) so
    the same per-content-type formulas that built the source-level T1
    score get used for the per-chapter pairwise comparison.
    """
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(blob))
        img.load()
    except Exception:
        return None
    try:
        fmt = (img.format or "UNKNOWN").upper()
        width, height = img.size
    except Exception:
        return None
    if width < 100 or height < 100:
        return None
    is_grayscale = _is_grayscale_pil(img)
    is_lossless = bool(_detect_lossless_blob(blob))
    try:
        if content_type in ("bw_manga", "bw_manga_with_color_inserts"):
            _, meta = _compute_t1_score_bw(
                blob, img, fmt, width, height, is_grayscale, is_lossless, content_type,
            )
        else:
            _, meta = _compute_t1_score(
                blob, img, fmt, width, height, is_grayscale, is_lossless,
                content_type=content_type,
            )
    except Exception:
        return None

    components: "Dict[str, Optional[float]]" = {
        "res_norm": meta.get("res_norm"),
        "qf_or_lossless": None,
        "screentone": meta.get("bw_screentone_score"),
        "line": meta.get("bw_line_quality"),
        "bg_uniformity": meta.get("bw_bg_uniformity"),
        "block_inv": None,
        "fft_hf": meta.get("fft_hf_ratio"),
        "tenengrad": meta.get("tenengrad_clean"),
    }
    if is_lossless:
        components["qf_or_lossless"] = 1.0
    elif meta.get("jpeg_qf_norm") is not None:
        components["qf_or_lossless"] = float(meta["jpeg_qf_norm"])
    blockiness = meta.get("blockiness")
    if blockiness is not None:
        components["block_inv"] = 1.0 - float(blockiness)
    return components


_PAIRWISE_COMPONENT_KEYS = (
    "res_norm",
    "qf_or_lossless",
    "screentone",
    "line",
    "bg_uniformity",
    "block_inv",
    "fft_hf",
    "tenengrad",
)


def _run_pairwise_ranking(
    candidates: "List[SeriesCandidate]",
    scraper_factory: Callable,
    make_request: Callable,
    on_status: Optional[Callable[[str], None]] = None,
    probe_failure_cache: "Optional[ProbeFailureCache]" = None,
) -> None:
    """v6 anchor-free pairwise T3 ranking. In-place updates to
    candidate.sources[].img_quality_score with `pairwise_adjustment`.

    Replaces v5.1's anchor-based DISTS+ST-LPIPS comparison
    (_run_paired_comparison, retained as dead code for back-compat).
    The anchor circularity disappears because every pair is symmetric
    — no source is treated as the reference.

    Per-candidate algorithm:
      1. Filter measured sources (score > PAIRWISE_MIN_SOURCE_SCORE AND
         real chapter probe — samples_succeeded > 0).
      2. Bucket sources by structural content_type (manga_pages /
         webtoon_chunked / single_image / unknown_bucket). Skip the
         unknown bucket and any bucket with <2 sources.
      3. For each pairable bucket, take the top PAIRWISE_TOP_SOURCES by
         current img_quality_score.
      4. Fetch chapter lists for each top source; intersect
         whole-numbered chapter numbers across all sources to find
         shared chapters.
      5. Sample PAIRWISE_CHAPTERS_PER_BUCKET evenly-spaced shared
         chapters and fetch the deterministic middle page from each
         source for each picked chapter.
      6. Compute T1 component vector (_compute_pairwise_components) for
         each (source, chapter) page.
      7. For each source pair (A, B), for each shared chapter, for each
         component: increment wins[A][component] or wins[B][component]
         based on the comparison. Ties (or both None) contribute zero.
      8. winrate_i = total_wins_i / (total_wins_i + total_losses_i).
         pairwise_adj = (winrate − 0.5) × 2 × PAIRWISE_MAX_ADJ.
      9. JPEG ghost penalty (per-source, alignment-independent):
         compute on the first successful blob per source, subtract
         0.10 × ghost_score from the adjustment.
     10. Clamp adjustment to [−PAIRWISE_MAX_ADJ, +PAIRWISE_MAX_ADJ],
         update src.img_quality_score in-place, populate pairwise_*
         metadata fields.

    Budgeted: PAIRWISE_BUDGET_S total + PAIRWISE_PER_CANDIDATE_BUDGET_S
    per candidate. Skipped (no-op) when candidates is empty.

    Cross-file: invoked from search_all post-probe-phase (replacing the
    previous _run_paired_comparison call). JPEG-ghost helper
    _compute_jpeg_ghost is reused unchanged.
    """
    if not candidates:
        return

    from . import get_handler_by_name
    from collections import defaultdict
    from .chapter_merger import _extract_chapter_num
    from itertools import combinations

    def _host_blocked(handler) -> bool:
        """Probe-cooldown gate: skip pairwise fetches against hosts that
        the search-phase ProbeFailureCache already suppressed (record_cooldown
        threshold crossed). Without this gate the pairwise phase re-hits
        the same dead host within the cooldown window, eats per_cand_deadline
        budget on guaranteed-failure requests, and attributes the failure
        to the SOURCE's score rather than the rate-limit state. Returns
        False when probe_failure_cache is None or the handler has no
        canonical host."""
        if probe_failure_cache is None:
            return False
        host = ""
        try:
            host = (handler.domains[0] if getattr(handler, "domains", None) else "") or ""
        except Exception:
            host = ""
        return bool(host) and probe_failure_cache.is_blocked(host)

    # Monotonic clock for all in-process deadlines (probe / pairwise /
    # paired-comparison). Wall-clock (time.time()) drifts on NTP slews,
    # jumps backward on DST roll-back, and ages forward on manual clock
    # corrections — any of which silently expires or extends budgets at
    # the wrong moment. time.monotonic() is jump-free and the canonical
    # choice for relative-elapsed-time gates. Cache TTL writes
    # (ProbeFailureCache._state, ImageQualityCache._state) keep time.time()
    # because they persist across process restarts and need wall-clock
    # semantics. Grep target: time.monotonic() shows all deadline gates;
    # time.time() now only appears for persistence.
    total_deadline = time.monotonic() + PAIRWISE_BUDGET_S
    candidates_compared = 0

    for cand in candidates:
        if time.monotonic() > total_deadline:
            if on_status:
                on_status(
                    f"[!] T3 pairwise: budget "
                    f"({PAIRWISE_BUDGET_S:.0f}s) exhausted after "
                    f"{candidates_compared} candidate(s); skipping remaining"
                )
            return

        def _has_real_probe(s):
            m = s.img_quality_metadata or {}
            try:
                return int(m.get("samples_succeeded") or 0) > 0
            except (TypeError, ValueError):
                return False

        measured = [
            s for s in cand.sources
            if s.img_quality_score is not None
            and s.img_quality_score > PAIRWISE_MIN_SOURCE_SCORE
            and _has_real_probe(s)
        ]
        if len(measured) < 2:
            continue

        def _bucket(s):
            ct = (s.img_quality_metadata or {}).get("content_type") or "unknown"
            if ct == "color_webtoon_chunked":
                return "webtoon_chunked"
            if ct in ("color_manga", "bw_manga", "bw_manga_with_color_inserts"):
                return "manga_pages"
            if ct == "color_webtoon_single_image":
                return "single_image"
            return "unknown_bucket"

        bucket_to_sources: "Dict[str, List]" = defaultdict(list)
        for s in measured:
            bucket_to_sources[_bucket(s)].append(s)
        pairable_buckets = [
            (name, srcs) for name, srcs in bucket_to_sources.items()
            if name != "unknown_bucket" and len(srcs) >= 2
        ]
        if not pairable_buckets:
            continue

        any_bucket_ran = False
        for bucket_name, bucket_sources in pairable_buckets:
            if time.monotonic() > total_deadline:
                break

            top = sorted(
                bucket_sources, key=lambda s: -(s.img_quality_score or 0.0),
            )[:PAIRWISE_TOP_SOURCES]
            if len(top) < 2:
                continue

            per_cand_deadline = time.monotonic() + PAIRWISE_PER_CANDIDATE_BUDGET_S

            # Fetch chapter lists for each top source.
            chapter_lists: "Dict[str, List[Dict]]" = {}
            for src in top:
                if time.monotonic() > per_cand_deadline:
                    break
                handler = get_handler_by_name(src.site)
                if handler is None:
                    continue
                # Skip hosts that the search-phase cooldown is suppressing.
                # `_fetch_probe_item_bytes` (called below) bypasses
                # make_request and goes straight through scraper.get, so the
                # rate-limit machinery downstream of make_request can't see
                # these probes either — we have to gate here directly.
                if _host_blocked(handler):
                    if on_status:
                        on_status(
                            f"  [T3] skip {src.site} for pairwise — host in cooldown"
                        )
                    continue
                try:
                    scraper = scraper_factory(handler)
                    ctx = handler.fetch_comic_context(src.url, scraper, make_request)
                    chapters = handler.get_chapters(ctx, scraper, "en", make_request)
                    if chapters:
                        chapter_lists[src.site] = chapters
                except Exception:
                    continue
            if len(chapter_lists) < 2:
                continue

            # Find shared whole-numbered chapter numbers across ALL sources.
            per_source_by_chapnum: "Dict[str, Dict[float, Dict]]" = {}
            shared_chapnums: "Optional[set]" = None
            for src in top:
                if src.site not in chapter_lists:
                    continue
                by_chap: "Dict[float, Dict]" = {}
                for ch in chapter_lists[src.site]:
                    num = _extract_chapter_num(ch.get("chap") or ch.get("title"))
                    if num is not None and float(num) == int(float(num)):
                        by_chap[float(int(num))] = ch
                per_source_by_chapnum[src.site] = by_chap
                chap_set = set(by_chap.keys())
                shared_chapnums = chap_set if shared_chapnums is None else (shared_chapnums & chap_set)

            if not shared_chapnums:
                continue
            shared_sorted = sorted(shared_chapnums)
            if len(shared_sorted) > PAIRWISE_CHAPTERS_PER_BUCKET:
                step = len(shared_sorted) / PAIRWISE_CHAPTERS_PER_BUCKET
                idx = [int(step * (i + 0.5)) for i in range(PAIRWISE_CHAPTERS_PER_BUCKET)]
                idx = [min(i, len(shared_sorted) - 1) for i in idx]
                picked_chapnums = [shared_sorted[i] for i in idx]
            else:
                picked_chapnums = shared_sorted

            # Fetch middle pages + compute components per (source, chapter).
            components_by_source: "Dict[str, Dict[float, Dict]]" = defaultdict(dict)
            ghost_by_source: "Dict[str, Optional[float]]" = {}
            for src in top:
                if src.site not in per_source_by_chapnum:
                    continue
                if time.monotonic() > per_cand_deadline:
                    break
                handler = get_handler_by_name(src.site)
                if handler is None:
                    continue
                # Mirror the cooldown gate from the chapter-list loop above.
                # A host could have flipped to suppressed BETWEEN the two
                # loops (record_cooldown is set when make_request crosses a
                # rate-limit on retries elsewhere); re-checking here is
                # cheap and prevents an in-flight pairwise pass from
                # racing the cache update.
                if _host_blocked(handler):
                    continue
                try:
                    scraper = scraper_factory(handler)
                except Exception:
                    continue
                src_content_type = (src.img_quality_metadata or {}).get("content_type") or "unknown"
                first_blob_for_ghost: Optional[bytes] = None
                for chapnum in picked_chapnums:
                    if time.monotonic() > per_cand_deadline:
                        break
                    ch = per_source_by_chapnum[src.site].get(chapnum)
                    if ch is None:
                        continue
                    try:
                        pages = handler.get_chapter_images(ch, scraper, make_request)
                    except Exception:
                        continue
                    if not pages:
                        continue
                    idx_page = handler._pick_random_middle_page_index(
                        len(pages), src.url, int(chapnum),
                    )
                    if idx_page is None:
                        continue
                    try:
                        blob = handler._fetch_probe_item_bytes(pages[idx_page], scraper)
                    except Exception:
                        continue
                    if not blob:
                        continue
                    page_components = _compute_pairwise_components(blob, src_content_type)
                    if page_components is None:
                        continue
                    components_by_source[src.site][chapnum] = page_components
                    if first_blob_for_ghost is None:
                        first_blob_for_ghost = blob

                # JPEG-ghost (per-source) on the first successful blob.
                if first_blob_for_ghost is not None:
                    try:
                        ghost_score, _g_diag = _compute_jpeg_ghost(first_blob_for_ghost)
                        ghost_by_source[src.site] = ghost_score
                    except Exception:
                        ghost_by_source[src.site] = None
                else:
                    ghost_by_source[src.site] = None

            # Pairwise win counting.
            wins_per_source: "Dict[str, Dict[str, int]]" = defaultdict(lambda: defaultdict(int))
            losses_per_source: "Dict[str, Dict[str, int]]" = defaultdict(lambda: defaultdict(int))
            for A, B in combinations(top, 2):
                a_chaps = components_by_source.get(A.site, {})
                b_chaps = components_by_source.get(B.site, {})
                for chapnum in picked_chapnums:
                    a_comps = a_chaps.get(chapnum)
                    b_comps = b_chaps.get(chapnum)
                    if not a_comps or not b_comps:
                        continue
                    for comp_key in _PAIRWISE_COMPONENT_KEYS:
                        a_val = a_comps.get(comp_key)
                        b_val = b_comps.get(comp_key)
                        if a_val is None or b_val is None:
                            continue
                        if a_val > b_val:
                            wins_per_source[A.site][comp_key] += 1
                            losses_per_source[B.site][comp_key] += 1
                        elif b_val > a_val:
                            wins_per_source[B.site][comp_key] += 1
                            losses_per_source[A.site][comp_key] += 1

            # Apply adjustments.
            for src in top:
                wins = wins_per_source.get(src.site, {})
                losses = losses_per_source.get(src.site, {})
                total_wins = int(sum(wins.values()))
                total_losses = int(sum(losses.values()))
                total_comparisons = total_wins + total_losses
                ghost = ghost_by_source.get(src.site)
                ghost_penalty = 0.10 * (ghost if ghost is not None else 0.0)
                meta = dict(src.img_quality_metadata or {})
                if total_comparisons == 0:
                    # No usable component comparisons — leave score
                    # unchanged but still apply ghost penalty.
                    total_adj = -ghost_penalty
                    total_adj = max(-PAIRWISE_MAX_ADJ, min(PAIRWISE_MAX_ADJ, total_adj))
                    if total_adj != 0.0:
                        src.img_quality_score = max(
                            0.0, min(1.0, (src.img_quality_score or 0.0) + total_adj),
                        )
                    meta["pairwise_adjustment"] = round(total_adj, 4)
                    meta["pairwise_winrate"] = None
                    meta["pairwise_total_comparisons"] = 0
                else:
                    winrate = total_wins / total_comparisons
                    pairwise_adj = (winrate - 0.5) * 2.0 * PAIRWISE_MAX_ADJ
                    total_adj = pairwise_adj - ghost_penalty
                    total_adj = max(-PAIRWISE_MAX_ADJ, min(PAIRWISE_MAX_ADJ, total_adj))
                    src.img_quality_score = max(
                        0.0, min(1.0, (src.img_quality_score or 0.0) + total_adj),
                    )
                    meta["pairwise_adjustment"] = round(total_adj, 4)
                    meta["pairwise_winrate"] = round(winrate, 4)
                    meta["pairwise_total_comparisons"] = total_comparisons
                meta["pairwise_wins_by_component"] = dict(wins)
                meta["pairwise_bucket"] = bucket_name
                meta["pairwise_ghost_score"] = (
                    round(float(ghost), 4) if ghost is not None else None
                )
                meta["pairwise_ghost_penalty"] = round(ghost_penalty, 4)
                src.img_quality_metadata = meta

            any_bucket_ran = True

        if any_bucket_ran:
            candidates_compared += 1


def _run_paired_comparison(
    candidates: "List[SeriesCandidate]",
    scraper_factory: Callable,
    make_request: Callable,
    on_status: Optional[Callable[[str], None]] = None,
) -> None:
    """Cross-source paired DISTS comparison; in-place updates to
    candidate.sources[].img_quality_score with a paired_quality_adjustment.

    For each candidate with 2+ measured sources where img_quality_score is
    non-None and above PAIRED_MIN_SOURCE_SCORE:
      1. Take the top PAIRED_TOP_SOURCES by current score.
      2. Anchor = source with highest metadata.width.
      3. Fetch chapter lists for each top source.
      4. Find PAIRED_CHAPTERS_PER_CANDIDATE chapters present in BOTH the
         anchor and the target (matched by chap_num).
      5. For each shared chapter:
         - Pick a deterministic middle page on each side
         - Fetch that page from anchor + target
         - Align via phaseCorrelate + crop
         - Compute DISTS
      6. Median DISTS for the target source → paired_quality_adjustment
         clamped to [-PAIRED_MAX_ADJUSTMENT, +PAIRED_MAX_ADJUSTMENT].
         Negative DISTS_median maps to a positive adjustment (boost when
         target is similar to anchor); high DISTS_median is a penalty.
      7. Update src.img_quality_score += paired_quality_adjustment AND
         metadata fields (paired_anchor_site, paired_dists_median,
         paired_pairs_compared).

    Skipped silently when piq/cv2 unavailable. Budgeted at
    PAIRED_COMPARISON_BUDGET_S total + PAIRED_PER_CANDIDATE_BUDGET_S per
    candidate. Anchor's own adjustment is always 0 (by definition).

    Cross-file: invoked from search_all post-probe-phase. The cache helper
    `ImageQualityCache.set_paired_adjustment` persists T3 fields without
    bumping expiry (cache-warm sources accumulate T3 without triggering
    re-probe). chapter_merger.align_chapter_lists is NOT used here — we
    do direct chap_num matching for simplicity; the merger's collapse
    rules are deliberately bypassed because T3 wants raw chapter matches.
    """
    if not _piq_available() or not _cv2_available():
        return
    if not candidates:
        return

    from . import get_handler_by_name

    total_deadline = time.monotonic() + PAIRED_COMPARISON_BUDGET_S
    candidates_compared = 0
    for cand in candidates:
        if time.monotonic() > total_deadline:
            if on_status:
                on_status(
                    f"[!] T3 paired-compare: total budget "
                    f"({PAIRED_COMPARISON_BUDGET_S:.0f}s) exhausted after "
                    f"{candidates_compared} candidate(s); skipping remaining"
                )
            return

        # Filter measured sources above the floor. v5.1 fix: also require
        # that the source has actual CHAPTER PROBE metadata (samples_succeeded
        # > 0). Sources whose img_quality_score came from the cover-probe
        # fallback path don't populate samples_succeeded; they look
        # high-quality (cover JPEGs are usually 0.7+) but can't actually
        # serve chapter pages — picking them as anchor causes T3 to fetch
        # pages from a DMCA-affected/unreachable source and silently skip
        # every pair. Real-world driver: Eleceed on MangaDex (DMCA-hollowed
        # to 102 chapters with cover-probe score 0.80; if picked as anchor,
        # T3 ran 0 pairs across all targets because mangadex.get_chapter_images
        # raised for the residual chapters).
        def _has_real_probe(s):
            m = s.img_quality_metadata or {}
            try:
                return int(m.get("samples_succeeded") or 0) > 0
            except (TypeError, ValueError):
                return False

        measured = [
            s for s in cand.sources
            if s.img_quality_score is not None
            and s.img_quality_score > PAIRED_MIN_SOURCE_SCORE
            and _has_real_probe(s)  # exclude cover-probe-only sources
        ]
        if len(measured) < 2:
            continue

        # v5.1 (2026-05-17): only pair within structurally-compatible
        # buckets. The page LAYOUT must match for phaseCorrelate / ECC /
        # ORB alignment to work — and even for ORB to give meaningful
        # signal. Three buckets:
        #   webtoon_chunked  — vertical-scroll chunked into ~80 narrow pages
        #                      per chapter (~700 wide). Pages are slices.
        #   manga_pages      — traditional manga pages (1-2 panels per page,
        #                      portrait orientation, color or B&W).
        #   single_image     — one huge tall image per chapter
        #                      (color_webtoon_single_image, mangadex DMCA).
        # Cross-bucket pairings are structurally incompatible: linewebtoon
        # chunk 200 (1/80th of chapter) vs mangakatana page 200 (1/5th of
        # chapter) compare unrelated content; alignment always falls to
        # ORB tier and produces garbage DISTS. Worse: it can produce a
        # confident-looking but bogus penalty (e.g. -0.15 on linewebtoon
        # vs mangakatana, despite linewebtoon being the canonical source).
        #
        # Per-bucket execution (not majority-pick): every bucket with ≥2
        # sources runs its own anchor/non_anchor T3 round. The previous
        # majority-pick approach failed for series like Eleceed where the
        # MAJORITY of indexing sites are color_manga reskins (3 of 5) but
        # the *user-relevant* sources are the 2 webtoon_chunked canonicals
        # (linewebtoon, weebcentral) — majority logic let the reskins
        # compete while leaving the canonicals unpaired. Per-bucket
        # comparison fixes this: each format competes within its own
        # bucket. Unknown/unbucketed sources skip pairing — they could
        # legitimately fall either way and would risk false cross-format
        # comparisons.
        def _bucket(s):
            ct = (s.img_quality_metadata or {}).get("content_type") or "unknown"
            if ct == "color_webtoon_chunked":
                return "webtoon_chunked"
            if ct in ("color_manga", "bw_manga", "bw_manga_with_color_inserts"):
                return "manga_pages"
            if ct == "color_webtoon_single_image":
                return "single_image"
            return "unknown_bucket"

        # Width as anchor tiebreaker assumes pixel-density correlates with
        # source fidelity; for ties we fall through to the original sort
        # order (which is by img_quality_score, so the higher-scored
        # source wins). Hoisted out of the bucket loop because it's reused.
        def _width(s):
            m = s.img_quality_metadata or {}
            try:
                return int(m.get("width") or 0)
            except (TypeError, ValueError):
                return 0

        from collections import defaultdict as _BucketDefault
        bucket_to_sources: "Dict[str, List]" = _BucketDefault(list)
        for s in measured:
            bucket_to_sources[_bucket(s)].append(s)
        # Skip the unknown bucket entirely (we can't trust the format
        # match). Only buckets with ≥2 members can be paired.
        pairable_buckets = [
            (name, srcs) for name, srcs in bucket_to_sources.items()
            if name != "unknown_bucket" and len(srcs) >= 2
        ]
        if not pairable_buckets:
            continue

        any_bucket_ran = False
        for bucket_name, bucket_sources in pairable_buckets:
            # Per-bucket total-budget check: if we've blown past the
            # candidate-wide total, stop scheduling new buckets. (The
            # outer per-candidate `total_deadline` was already checked
            # above; this re-check is for the cumulative case where
            # bucket 1 took its full per-cand budget and bucket 2 would
            # push the candidate over.)
            if time.monotonic() > total_deadline:
                break

            # Top N by score (within this bucket only).
            top = sorted(
                bucket_sources, key=lambda s: -(s.img_quality_score or 0.0),
            )[:PAIRED_TOP_SOURCES]
            anchor = max(top, key=_width)
            non_anchors = [s for s in top if s is not anchor]
            if not non_anchors:
                continue

            per_cand_deadline = time.monotonic() + PAIRED_PER_CANDIDATE_BUDGET_S

            # Fetch fresh chapter lists for each source in `top`. Reuse the
            # scraper-per-handler pattern so cookies / WAF state are honored.
            chapter_lists: Dict[str, "List[Dict]"] = {}
            for src in top:
                if time.monotonic() > per_cand_deadline:
                    break
                handler = get_handler_by_name(src.site)
                if handler is None:
                    continue
                try:
                    scraper = scraper_factory(handler)
                    ctx = handler.fetch_comic_context(src.url, scraper, make_request)
                    chapters = handler.get_chapters(ctx, scraper, "en", make_request)
                    if chapters:
                        chapter_lists[src.site] = chapters
                except Exception:
                    continue
            if anchor.site not in chapter_lists:
                continue  # anchor failed; skip this bucket, try next

            # Build chap_num → chapter dict mapping for anchor.
            from .chapter_merger import _extract_chapter_num
            anchor_by_chapnum: Dict[float, Dict] = {}
            for ch in chapter_lists[anchor.site]:
                num = _extract_chapter_num(ch.get("chap") or ch.get("title"))
                if num is not None and float(num) == int(float(num)):
                    anchor_by_chapnum[float(int(num))] = ch  # prefer whole-numbered

            if not anchor_by_chapnum:
                continue

            # For each non-anchor source, find shared chap_nums, sample N, fetch
            # one page each, align, run paired metrics. v5.1: collects DISTS,
            # ST-LPIPS, and ensemble scores per pair so the downstream median
            # has all three signals available for the cache + UI tooltip.
            per_target_pairs: "Dict[str, List[Tuple[float, float, float, str]]]" = {}
            # v5.1 Phase 3a: stash one target blob per source for the JPEG-ghost
            # analysis. Ghost is a per-source signal (not per-pair) so we only
            # need one blob per target; we keep the first successful one to
            # avoid redundant fetches and processing.
            per_target_ghost_blobs: "Dict[str, bytes]" = {}
            for tgt in non_anchors:
                if time.monotonic() > per_cand_deadline:
                    break
                tgt_chapters = chapter_lists.get(tgt.site)
                if not tgt_chapters:
                    continue
                # Build target's chap_num → chapter dict, same rule.
                tgt_by_chapnum: Dict[float, Dict] = {}
                for ch in tgt_chapters:
                    num = _extract_chapter_num(ch.get("chap") or ch.get("title"))
                    if num is not None and float(num) == int(float(num)):
                        tgt_by_chapnum[float(int(num))] = ch
                shared = sorted(set(anchor_by_chapnum.keys()) & set(tgt_by_chapnum.keys()))
                if not shared:
                    continue
                # Evenly-spaced selection across the shared chapter range (so we
                # sample early, middle, and late chapters — captures any drift
                # in quality over the series).
                if len(shared) > PAIRED_CHAPTERS_PER_CANDIDATE:
                    step = len(shared) / PAIRED_CHAPTERS_PER_CANDIDATE
                    indices = [int(step * (i + 0.5)) for i in range(PAIRED_CHAPTERS_PER_CANDIDATE)]
                    indices = [min(i, len(shared) - 1) for i in indices]
                    picked = [shared[i] for i in indices]
                else:
                    picked = list(shared)

                anchor_handler = get_handler_by_name(anchor.site)
                target_handler = get_handler_by_name(tgt.site)
                if anchor_handler is None or target_handler is None:
                    continue
                try:
                    anchor_scraper = scraper_factory(anchor_handler)
                    target_scraper = scraper_factory(target_handler)
                except Exception:
                    continue

                for chap_num in picked:
                    if time.monotonic() > per_cand_deadline:
                        break
                    anchor_chap = anchor_by_chapnum[chap_num]
                    target_chap = tgt_by_chapnum[chap_num]
                    try:
                        anchor_pages = anchor_handler.get_chapter_images(
                            anchor_chap, anchor_scraper, make_request,
                        )
                        target_pages = target_handler.get_chapter_images(
                            target_chap, target_scraper, make_request,
                        )
                    except Exception:
                        continue
                    if not anchor_pages or not target_pages:
                        continue
                    # Pick deterministic middle pages (same SHA-1 seed pattern as
                    # _pick_random_middle_page_index).
                    a_idx = anchor_handler._pick_random_middle_page_index(
                        len(anchor_pages), anchor.url, int(chap_num),
                    )
                    t_idx = target_handler._pick_random_middle_page_index(
                        len(target_pages), tgt.url, int(chap_num),
                    )
                    if a_idx is None or t_idx is None:
                        continue
                    try:
                        a_blob = anchor_handler._fetch_probe_item_bytes(
                            anchor_pages[a_idx], anchor_scraper,
                        )
                        t_blob = target_handler._fetch_probe_item_bytes(
                            target_pages[t_idx], target_scraper,
                        )
                    except Exception:
                        continue
                    if not a_blob or not t_blob:
                        continue
                    # v5.1: ensemble paired metric returns
                    # (dists, stlpips, ensemble, alignment_method).
                    quad = _compute_paired_perceptual(a_blob, t_blob)
                    if quad is None:
                        continue
                    per_target_pairs.setdefault(tgt.site, []).append(quad)
                    # v5.1 Phase 3a: stash one target blob per source for the
                    # JPEG-ghost analysis below. Ghost runs on a SINGLE page
                    # per target source (no need to ghost-analyze every paired
                    # page); we keep the first successful pair's target blob.
                    per_target_ghost_blobs.setdefault(tgt.site, t_blob)

            # Apply paired adjustments to non-anchor sources. v5.1: the adjustment
            # is driven by the ensemble (50/50 DISTS + ST-LPIPS) median; the
            # per-metric medians are stashed in metadata for diagnosis but don't
            # feed the adjustment directly. Keeping the total weight at 0.30
            # matches v5's DISTS-only weight so comparator re-tuning isn't needed.
            from statistics import median as _median
            for tgt in non_anchors:
                triples = per_target_pairs.get(tgt.site, [])
                if not triples:
                    # No pairings completed for this target — leave T3 metadata
                    # null so downstream knows it wasn't compared.
                    continue
                dists_values = [t[0] for t in triples]
                stlpips_values = [t[1] for t in triples]
                ensemble_values = [t[2] for t in triples]
                # v5.1 Phase 3b: alignment-method tracking. Most-common method
                # across the pairs surfaces in metadata; useful diagnostic when
                # one source consistently degrades to ORB (= heavily-warped
                # vs anchor) or ECC (= scale/rotation issues).
                alignment_methods = [t[3] for t in triples]
                from collections import Counter as _Counter
                most_common_method = _Counter(alignment_methods).most_common(1)[0][0]
                d_median = float(_median(dists_values))
                s_median = float(_median(stlpips_values))
                e_median = float(_median(ensemble_values))

                # v5.1 Phase 3a: JPEG-ghost double-compression detection on
                # one target page. Independent signal from the paired-perceptual
                # ensemble — adds penalty when target is re-encoded JPEG, even
                # when alignment-based comparison can't distinguish content.
                ghost_score: Optional[float] = None
                ghost_blob = per_target_ghost_blobs.get(tgt.site)
                if ghost_blob is not None:
                    try:
                        g_raw, _g_diag = _compute_jpeg_ghost(ghost_blob)
                        if g_raw is not None:
                            ghost_score = float(g_raw)
                    except Exception:
                        ghost_score = None

                # v5.1: only count RELIABLE-alignment pairs in the
                # adjustment computation. The historical bug: ORB-RANSAC
                # (Tier 3) finds enough keypoints between unrelated
                # content to produce a "best-effort" homography and a
                # comparable but meaningless DISTS reading. Real-world
                # driver: linewebtoon ↔ weebcentral (both serve Eleceed
                # as color_webtoon_chunked) but each aggregator chunks
                # the canonical webtoon at DIFFERENT vertical boundaries
                # — so "chunk 30" on linewebtoon and "chunk 30" on
                # weebcentral don't depict the same content. Phase-
                # correlate and ECC correctly fail; ORB still produces
                # an alignment and the resulting e_median triggers the
                # max -0.15 penalty against the official publisher.
                # Solution: require ≥2 pairs aligned via Tier 1 or 2
                # before the ensemble adjustment fires. ORB-only stays
                # as a diagnostic (alignment_method records the tier).
                # Ghost (per-target, alignment-independent) still applies.
                RELIABLE_ALIGNMENT = ("phase_correlate", "ecc_pyramid")
                reliable_triples = [
                    t for t in triples if t[3] in RELIABLE_ALIGNMENT
                ]
                n_reliable = len(reliable_triples)
                if n_reliable >= 2:
                    reliable_e_median = float(
                        _median([t[2] for t in reliable_triples])
                    )
                    # Inner ratio is the per-magnitude penalty signal: at
                    # e_median=0   → +1.0 (max positive lift),
                    # at e_median=0.15 → 0.0 (neutral),
                    # at e_median=0.3  → -1.0 (max negative lift).
                    # The clamp to [-1, 1] preserves graded discrimination
                    # ABOVE e_median=0.3 — without it, e_median=0.6 produces
                    # an inner of -3.0, e_median=0.95 produces -5.33, and
                    # the outer clamp at line 2869 squashes both into the
                    # same -PAIRED_MAX_ADJUSTMENT tier. After the inner
                    # clamp, every reliable_e_median ≥ 0.3 still pins at
                    # -PAIRED_MAX_ADJUSTMENT (the design intent — heavy
                    # divergence == max penalty), but the symmetric clamp
                    # leaves headroom for `ghost_penalty` to nudge the
                    # final `adj` strictly within range so the outer clamp
                    # is now a defensive belt-and-suspenders rather than a
                    # silent discrimination-killer.
                    inner_ratio = 1.0 - 2.0 * (reliable_e_median / 0.3)
                    inner_ratio = max(-1.0, min(1.0, inner_ratio))
                    ensemble_adj = PAIRED_MAX_ADJUSTMENT * inner_ratio
                else:
                    ensemble_adj = 0.0
                ghost_penalty = 0.10 * (ghost_score or 0.0)
                adj = ensemble_adj - ghost_penalty
                adj = max(-PAIRED_MAX_ADJUSTMENT, min(PAIRED_MAX_ADJUSTMENT, adj))
                tgt.img_quality_score = max(
                    0.0, min(1.0, (tgt.img_quality_score or 0.0) + adj),
                )
                meta = dict(tgt.img_quality_metadata or {})
                meta["paired_quality_adjustment"] = round(adj, 4)
                meta["paired_anchor_site"] = anchor.site
                # v5.1 cache fields. Keep `paired_dists_median` populated for
                # back-compat with v5 UI / external consumers that haven't
                # adopted the new naming yet; the v6 schema bump (Phase 6)
                # makes paired_perceptual_median the canonical field.
                meta["paired_perceptual_median"] = round(e_median, 4)
                meta["paired_dists_alone_median"] = round(d_median, 4)
                meta["paired_stlpips_median"] = round(s_median, 4)
                # v5.1 Phase 3a: ghost score stash. None when ghost couldn't
                # be computed (non-JPEG target, too small, encode failure).
                meta["paired_ghost_score"] = (
                    round(float(ghost_score), 4) if ghost_score is not None else None
                )
                # v5.1 Phase 3b: which alignment tier this pair used. Useful
                # diagnostic for sources that consistently fall to ORB/ECC
                # (indicates crop/rotation drift vs anchor).
                meta["paired_alignment_method"] = most_common_method
                meta["paired_dists_median"] = round(e_median, 4)  # v5 back-compat alias
                meta["paired_pairs_compared"] = len(triples)
                # v5.1: count of Tier-1/Tier-2 (reliable) alignments
                # among the paired pairs. UI shows this alongside the
                # total so users can see "3 pairs (0 reliable) → no
                # adjustment" — explains why two structurally-similar
                # sources didn't trigger a penalty.
                meta["paired_pairs_reliable"] = n_reliable
                # Note the device used so the UI can surface a "GPU" badge.
                meta["paired_device"] = _t2_device()
                tgt.img_quality_metadata = meta

            # Anchor's own paired metadata: adjustment = 0, but record the
            # anchor site (and bucket) for transparency.
            meta = dict(anchor.img_quality_metadata or {})
            meta["paired_quality_adjustment"] = 0.0
            meta["paired_anchor_site"] = anchor.site  # self-reference, signals "is anchor"
            meta["paired_perceptual_median"] = 0.0
            meta["paired_dists_alone_median"] = 0.0
            meta["paired_stlpips_median"] = 0.0
            meta["paired_dists_median"] = 0.0  # v5 back-compat alias
            meta["paired_ghost_score"] = None  # anchors don't get ghost-analyzed
            meta["paired_alignment_method"] = "anchor"  # marks self-reference
            meta["paired_pairs_compared"] = 0
            meta["paired_device"] = _t2_device()
            anchor.img_quality_metadata = meta
            any_bucket_ran = True

        if any_bucket_ran:
            candidates_compared += 1


def warmup_t2_models(background: bool = True) -> None:
    """Warm up T2 models so the first search doesn't pay the weight-download
    tax synchronously.

    On a fresh install with no cached weights, the first call to
    `_get_niqe_model()` blocks on HTTP to huggingface. With background=True
    we fire the warmup in a daemon thread at orchestrator startup; workers
    check `_T2_READY.is_set()` before invoking T2 and skip to T1-only when
    weights aren't yet cached (avoids serializing the worker pool behind a
    multi-minute download).

    NIQE's weights are tiny (8KB) so warmup is essentially instant after
    the first run. ARNIQA's are 107MB — when AIO_T2_ARNIQA=1 the first
    run can take 30-60s on a typical connection.

    v5.1: also warms ST-LPIPS (~56 MB, used by T3 paired comparison). Lives
    in the same daemon-prefetch path because the same `_T2_READY` event
    gates both probe-phase T2 and post-probe T3.

    v6: also warms CLIP-IQA+ via torchmetrics (~150 MB first run for the
    `clip_iqa` checkpoint from LightningAI's HuggingFace hub). Same
    daemon-prefetch slot — `_T2_READY` event gates probe-phase T2 which
    is where CLIP-IQA is invoked.

    Cross-file: called by `aio_search_cli.run_search_mode` once the user
    invokes --search, before the orchestrator's probe phase. Idempotent.
    """
    def _warmup():
        try:
            _get_niqe_model()
            if _T2_USE_ARNIQA:
                _get_arniqa_model()
            # v5.1: pre-load ST-LPIPS too. piq.DISTS is a v5 dep that
            # downloads on first use as well, but its weights are tiny
            # (12 KB) and `_get_dists_model` already handles it lazily.
            _get_stlpips_model()
            # v6: pre-load CLIP-IQA+ (torchmetrics). ~150 MB on cold
            # cache, instant once HF Hub cache is warm.
            if _torchmetrics_available():
                _get_clip_iqa_model()
        finally:
            _T2_READY.set()

    def _warmup_watermark():
        try:
            # easyocr Reader loads CRAFT detector + recognition model.
            # ~200 MB total on first run. The signal-set wraps the call
            # so workers that race the warmup get watermark_score=None
            # (no-op) instead of blocking.
            _get_easyocr_reader()
        finally:
            _WATERMARK_READY.set()

    # 2026-05-20: when ML rating is disabled (the default), short-circuit the
    # entire warmup — `_pyiqa_available()` and `_easyocr_available()` both
    # return False without importing anything, so the warmup threads would
    # be no-ops anyway. Setting the gates here avoids even spawning the
    # daemon threads, keeping process startup snappy.
    if not _ML_RATING_ENABLED:
        _T2_READY.set()
        _WATERMARK_READY.set()
        return
    if not _pyiqa_available():
        _T2_READY.set()  # nothing to wait for
    # v5.1 Phase 1: also kick off easyocr warmup. easyocr is independent
    # from pyiqa (T2) so we always-fire the watermark warmup even when T2
    # is missing — graceful degrade: T2-disabled but watermark-enabled is
    # a valid state.
    if not _easyocr_available():
        _WATERMARK_READY.set()  # nothing to wait for
    if _T2_READY.is_set() and _WATERMARK_READY.is_set():
        return
    if background:
        if not _T2_READY.is_set():
            t = threading.Thread(target=_warmup, name="t2-warmup", daemon=True)
            t.start()
        if not _WATERMARK_READY.is_set():
            t = threading.Thread(target=_warmup_watermark, name="watermark-warmup", daemon=True)
            t.start()
    else:
        if not _T2_READY.is_set():
            _warmup()
        if not _WATERMARK_READY.is_set():
            _warmup_watermark()


def _niqe_to_norm(niqe_score: float) -> float:
    """Normalize raw NIQE (typical range 3-10, lower=better) to a 0-1 score
    where 1 = pristine. Clamping band derived from observed real-world data.

    Calibration on tmp_nzmj real MangaFire pages: q=95→6.69, q=15→9.07.
    Reasonable photos sit in [3, 5]; degraded content [7, 10]+. We map
    [3, 10] → [0, 1] for niqe_norm (so higher=worse), then caller takes
    (1 - niqe_norm) to fold into the composite as a positive contribution.
    """
    try:
        v = float(niqe_score)
    except (TypeError, ValueError):
        return 0.5  # neutral fallback when input isn't sensible
    return max(0.0, min(1.0, (v - 3.0) / 7.0))


def _pil_to_tensor_rgb(img):
    """Convert a PIL Image to a torch tensor of shape (1, 3, H, W), float in
    [0, 1]. B&W pages are replicated across 3 channels so pyiqa models
    that expect RGB input don't choke. Lazy-imports torch + torchvision
    so the import cost lands once T2 is actually invoked.

    v5.1: tensor is moved to _t2_device() (CUDA when available, else CPU). The
    receiving model is also on _t2_device() so this matches automatically.
    """
    import torch
    from torchvision import transforms
    rgb = img.convert("RGB")
    t = transforms.ToTensor()(rgb).unsqueeze(0)
    device = _t2_device()
    if device != "cpu":
        try:
            t = t.to(device)
        except Exception:
            # Fall back to CPU silently if device move fails for any reason
            # (CUDA OOM mid-search, device disappeared, etc.). The model lives
            # on _t2_device() so this branch only triggers under explicit failure.
            pass
    return t


def _pil_to_tensor_clip(img):
    """Convert PIL → torch tensor (1, 3, H, W) with values in [0, 255]
    for torchmetrics CLIPImageQualityAssessment. The metric pipes the
    input through HuggingFace's CLIPImageProcessor which expects
    uint8-style float values (NOT the [0, 1] convention of
    torchvision.transforms.ToTensor). Using ToTensor for CLIP-IQA
    would double-rescale and yield nonsense scores.

    Shape: keeps the original image dimensions (CLIP-IQA resizes
    internally to 224×224 via its CLIPImageProcessor). Move to
    _t2_device() same as _pil_to_tensor_rgb.
    """
    import torch
    import numpy as np
    rgb = img.convert("RGB")
    arr = np.asarray(rgb, dtype=np.float32)  # (H, W, 3) in [0, 255]
    t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).contiguous()
    device = _t2_device()
    if device != "cpu":
        try:
            t = t.to(device)
        except Exception:
            pass
    return t


def _compute_t2_score(
    blob: bytes, img, is_grayscale: bool, content_type: str = "unknown",
) -> "Tuple[Optional[float], Dict]":
    """Run T2 deep-model scoring. Returns (t2_composite, t2_metadata) or
    (None, {"t2_available": False}) when T2 is disabled / unavailable.

    v6 (2026-05-18): adds torchmetrics CLIP-IQA+ with manga-tuned prompts
    as a parallel signal alongside NIQE. content_type drives the signal
    mix:

      B&W content (content_type ∈ bw_manga, bw_manga_with_color_inserts):
        Skip NIQE entirely — Mittal-Bovik 2013's NSS distribution
        assumption is violated by manga (flat regions, hard edges,
        screentones), and empirical evidence in our calibration shows
        NIQE scores are unreliable on B&W pages.
          t2_composite = mean(clip_iqa_score [+ arniqa_score if opted-in])

      Color / unknown content:
        Keep NIQE (works well on color manga / webtoons) + CLIP-IQA+.
          t2_composite = mean(1 - niqe_norm, clip_iqa_score
                              [+ arniqa_score if opted-in])

    ARNIQA stays opt-in via AIO_T2_ARNIQA=1 — its cost/discrimination
    trade-off (~1.2 s/page for ≤0.06 score variation in the operational
    q=70-95 range) is poor and we default it off.

    Returns the raw scores in metadata so callers can audit / surface
    in UI:
      niqe_score, niqe_norm, arniqa_score, clip_iqa_score, t2_score,
      t2_available

    Failures are silent — any exception in inference falls through to
    "t2_available=False" so the orchestrator's composite drops to T1-only
    cleanly.

    Cross-file: invoked from _score_image_blob below.
    """
    if not _pyiqa_available() or not _T2_READY.is_set():
        return None, {"t2_available": False}

    # v6 degenerate-image gate. CLIP-IQA doesn't have NIQE's built-in
    # degenerate-content detector (NSS / nancov warning path) — it
    # happily returns ~0.5-0.95 on near-uniform images because CLIP
    # sees them as "plausibly a clean photo of a flat surface". For
    # quality scoring, near-uniform IS the degenerate case: synthetic
    # placeholders, broken super-low-q WebP encodes, plain-color CDN
    # sentinels. Variance < 10 on 8-bit luminance is the cutoff —
    # real manga pages sit > 100 (line art + screentones contribute
    # mass to the variance). Below the gate we return T2 unavailable
    # so _score_image_blob's blend is skipped and downstream caps
    # like webp_below_floor stay intact.
    try:
        import numpy as np
        _lum = np.asarray(img.convert("L"), dtype=np.uint8)
        if float(_lum.astype(np.float32).var()) < 10.0:
            return None, {"t2_available": False, "t2_skipped_reason": "degenerate_uniform"}
    except Exception:
        pass

    is_bw = content_type in ("bw_manga", "bw_manga_with_color_inserts")

    metadata: Dict[str, Any] = {"t2_available": False}
    niqe_score: Optional[float] = None
    niqe_norm: Optional[float] = None
    arniqa_score: Optional[float] = None
    clip_iqa_score: Optional[float] = None

    # Convert image once for the pyiqa-style models (NIQE/ARNIQA).
    try:
        tensor_rgb = _pil_to_tensor_rgb(img)
    except Exception:
        return None, metadata

    # --- NIQE (color / unknown content only — skipped for B&W) ----------
    if not is_bw:
        niqe_model = _get_niqe_model()
        if niqe_model is not None:
            # Two-gate NIQE inference (patch count + degenerate-content
            # warning-to-exception). Same logic as v5.1.
            try:
                h, w = tensor_rgb.shape[-2], tensor_rgb.shape[-1]
            except Exception:
                h = w = 0
            NIQE_BLOCK = 96
            n_patches = (h // NIQE_BLOCK) * (w // NIQE_BLOCK)
            if n_patches >= 4:
                import warnings as _warnings
                with _T2_NIQE_INFER_LOCK:
                    try:
                        import torch
                        with torch.no_grad(), _warnings.catch_warnings():
                            _warnings.simplefilter("error", UserWarning)
                            raw = niqe_model(tensor_rgb)
                        niqe_score = float(raw.item()) if hasattr(raw, "item") else float(raw)
                        niqe_norm = _niqe_to_norm(niqe_score)
                    except (Exception, UserWarning):
                        niqe_score = None
                        niqe_norm = None

    # --- ARNIQA (opt-in for all content types) -------------------------
    if _T2_USE_ARNIQA:
        arniqa_model = _get_arniqa_model()
        if arniqa_model is not None:
            with _T2_ARNIQA_INFER_LOCK:
                try:
                    import torch
                    with torch.no_grad():
                        raw = arniqa_model(tensor_rgb)
                    arniqa_score = float(raw.item()) if hasattr(raw, "item") else float(raw)
                except Exception:
                    arniqa_score = None

    # --- CLIP-IQA+ via torchmetrics (manga-tuned prompts) --------------
    # Fires for ALL content types — CLIP's pretraining corpus (LAION,
    # WIT) saw enough anime/manga that the prompt-engineered antonym
    # discriminates non-photographic content too. For B&W content this
    # is the primary T2 signal (NIQE is skipped); for color it's a
    # complementary signal alongside NIQE.
    clip_model = _get_clip_iqa_model()
    if clip_model is not None:
        try:
            tensor_clip = _pil_to_tensor_clip(img)
        except Exception:
            tensor_clip = None
        if tensor_clip is not None:
            with _CLIP_IQA_INFER_LOCK:
                try:
                    import torch
                    with torch.no_grad():
                        raw = clip_model(tensor_clip)
                    clip_iqa_score = float(raw.item()) if hasattr(raw, "item") else float(raw)
                    # Clamp to [0, 1] defensively — the metric is already
                    # in this range but external floats can drift.
                    clip_iqa_score = float(max(0.0, min(1.0, clip_iqa_score)))
                except Exception:
                    clip_iqa_score = None

    # --- Compose ------------------------------------------------------
    components: List[float] = []
    if niqe_norm is not None:
        components.append(1.0 - niqe_norm)
    if clip_iqa_score is not None:
        components.append(clip_iqa_score)
    if arniqa_score is not None:
        components.append(float(min(1.0, max(0.0, arniqa_score))))

    t2_composite: Optional[float] = None
    if components:
        t2_composite = sum(components) / len(components)
        metadata["t2_available"] = True

    metadata["niqe_score"] = round(niqe_score, 4) if niqe_score is not None else None
    metadata["niqe_norm"] = round(niqe_norm, 4) if niqe_norm is not None else None
    metadata["arniqa_score"] = round(arniqa_score, 4) if arniqa_score is not None else None
    metadata["clip_iqa_score"] = round(clip_iqa_score, 4) if clip_iqa_score is not None else None
    metadata["t2_score"] = round(t2_composite, 4) if t2_composite is not None else None
    # Legacy v5 keys preserved (set to None — they were always None in v5.1
    # because pyiqa's clip-iqa was broken on Py3.13; in v6 we use
    # torchmetrics' CLIP-IQA which gives a SINGLE scalar score, not a per-
    # prompt dict, so clip_iqa_scores / clip_iqa_mean stay None for
    # back-compat with any cache reader expecting them).
    metadata["clip_iqa_scores"] = None
    metadata["clip_iqa_mean"] = None
    return t2_composite, metadata


def _detect_lossless_blob(blob: bytes) -> Optional[bool]:
    """Phase H2 (2026-05-08): detect whether an image blob is a known
    lossless format from its container header — quick byte-twiddle parse,
    no PIL decode needed.

    Returns True for PNG and WebP-VP8L (lossless WebP), False for known
    lossy (JPEG, WebP-VP8), None for ambiguous/unknown formats. Caller
    short-circuits decode_quality=1.0 for True, falls through to
    quantization/bpp estimates otherwise.

    Why bother with byte-twiddling: the prior scorer hardcoded
    decode_quality=0.85 for every non-JPEG, which silently underrated
    actually-lossless WebP/PNG sources by 15%. Detecting the lossless
    case honestly before hitting the bpp curve fixes that.

    Cross-file: called from _score_image_blob below; no other consumers.
    """
    if len(blob) < 16:
        return None
    # PNG: 8-byte magic
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    # WebP RIFF container: bytes 0-3 'RIFF', bytes 8-11 'WEBP',
    # bytes 12-15 chunk type.
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        chunk = blob[12:16]
        if chunk == b"VP8L":
            return True   # lossless WebP
        if chunk == b"VP8 ":
            return False  # lossy WebP
        if chunk == b"VP8X":
            # Extended container — VP8X header is bytes 12-29 (18 bytes:
            # 4-byte chunk id + 4-byte size + 10-byte body). After it,
            # OPTIONAL chunks may appear before the inner VP8/VP8L
            # bitstream: ICCP (color profile — common on Photoshop/Krita
            # exports with sRGB or wider gamut), ANIM (animation params),
            # ALPH (separate alpha for lossy WebP+alpha), EXIF, XMP.
            # We MUST chunk-walk; a naive byte-30 read misclassifies any
            # lossless WebP carrying a color profile, which is a sizeable
            # fraction of high-quality WebP exports in the wild.
            pos = 30  # first chunk after the VP8X header
            while pos + 8 <= len(blob):
                ctype = blob[pos:pos + 4]
                # Chunk size is little-endian 32-bit immediately following
                # the 4-byte chunk id; chunks are word-aligned (1 pad byte
                # if size is odd).
                csize = int.from_bytes(blob[pos + 4:pos + 8], "little")
                if ctype == b"VP8L":
                    return True
                if ctype == b"VP8 ":
                    return False
                pos += 8 + csize + (csize & 1)
            return None
    # JPEG: bytes 0-1 0xFFD8 → always lossy; let the JPEG path handle it.
    if blob[:2] == b"\xff\xd8":
        return False
    return None


def _is_grayscale_pil(img) -> bool:
    """Phase H3 (2026-05-08): detect whether an image is effectively
    grayscale via a sparse-grid chroma probe. ~400 samples (20×20
    thumbnail), <1ms in PIL.

    Uses the **90th-percentile** chroma deviation, not the max:
      - JPEG re-encodes of B&W scans pick up localized chroma noise from
        encoder ringing. A single noisy pixel out of 400 would otherwise
        push max above the threshold and force a B&W page into the color
        curve. The misclassification is much more punishing than the bpp
        curve itself: a clean B&W manga page at 0.10 bpp scores
        (0.10-0.03)/0.20 = 0.35 in the B&W curve vs (0.10-0.08)/0.40 =
        0.05 in the color curve — false-color is the dominant error.
      - Sepia/restoration scans carry consistent low-but-nonzero chroma
        (~10-20/255); max-based detection always declares them color
        even though they're functionally monochromatic. p90 is more
        forgiving and the threshold can be tuned upward (5 → 8 or 10)
        to fold sepia into the grayscale class if desired.

    Cross-file: called from _score_image_blob below to drive
    _bpp_decode_quality's curve selection.
    """
    if getattr(img, "mode", None) in ("L", "1"):
        return True
    try:
        # Local import — keeps the module importable when PIL isn't
        # available (probe phase no-ops in that case anyway).
        from PIL import Image
        import numpy as np
        sample = img.convert("RGB").resize((20, 20), Image.LANCZOS)
    except Exception:
        return False
    # Vectorized chroma deviation. Cast to int16 first so r-g underflow
    # in uint8 doesn't wrap; ravel() the 20×20 result for percentile.
    # (Previous impl used `for r, g, b in sample.getdata()` which is
    # deprecated in Pillow 14 — the numpy path also runs ~50× faster
    # on the 400-pixel sample.)
    arr = np.asarray(sample).astype(np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    chroma = np.maximum.reduce([np.abs(r - g), np.abs(r - b), np.abs(g - b)])
    chroma_sorted = np.sort(chroma.ravel())
    if chroma_sorted.size == 0:
        return False
    # 90th percentile: tolerates up to ~40 noisy pixels (out of 400).
    p90 = int(chroma_sorted[chroma_sorted.size * 9 // 10])
    return p90 < 5


# --- v5.1 Phase 4: content-type detection -----------------------------
# Per-page features feed _classify_series_content (called once per source
# from _probe_chapter_aggregate after all samples land). The classifier
# returns one of:
#   "bw_manga"                    — pure B&W traditional manga
#   "bw_manga_with_color_inserts" — B&W manga with occasional color covers
#   "color_manga"                 — full-color traditional manga
#   "color_webtoon_chunked"       — aggregator-split vertical-scroll
#                                   (consistent narrow widths)
#   "color_webtoon_single_image"  — single tall image per chapter
#   "unknown"                     — ambiguous, default to bw_manga weights

def _compute_chroma_var(img) -> float:
    """Return chroma variance on a 64x64 downsample of the image.

    Used to distinguish color pages from B&W pages that PIL might store in
    RGB mode (e.g., a B&W JPEG decoded into RGB has chroma_var ~0; a color
    page has chroma_var > 10). Cheap (~2 ms on a 1500×2000 page) because
    the downsample dominates work.

    Cross-file: called from _compute_page_content_features below. See
    sites/t1_constants.py:CHROMA_VARIANCE_THRESHOLD for the cutoff used
    in classification.
    """
    import numpy as np
    try:
        from PIL import Image as _PIL
        # 64×64 RGB downsample. Lanczos preserves chroma faithfully.
        small = img.convert("RGB").resize((64, 64), _PIL.LANCZOS)
        arr = np.asarray(small, dtype=np.float32)
    except Exception:
        return 0.0
    # Chroma variance = std deviation of per-pixel (R-G), (G-B), (R-B).
    # Sum the three to get a single scalar.
    if arr.shape[-1] < 3:
        return 0.0
    rg = arr[..., 0] - arr[..., 1]
    gb = arr[..., 1] - arr[..., 2]
    rb = arr[..., 0] - arr[..., 2]
    return float((rg.std() + gb.std() + rb.std()) / 3.0)


def _compute_page_content_features(img, blob: bytes) -> "Dict[str, Any]":
    """Build per-page features that _classify_series_content aggregates.

    Inputs:
      img — decoded PIL.Image (may be in L, RGB, or other modes)
      blob — raw bytes (used for format detection only)

    Output: dict with keys:
      width, height, aspect (w/h), is_grayscale_page, chroma_var

    Cross-file: invoked by _probe_chapter_aggregate per sampled page;
    aggregated features feed _classify_series_content below.
    """
    try:
        w, h = img.size
    except Exception:
        return {"width": 0, "height": 0, "aspect": 1.0,
                "is_grayscale_page": False, "chroma_var": 0.0}
    aspect = (w / h) if h > 0 else 1.0
    return {
        "width": int(w),
        "height": int(h),
        "aspect": float(aspect),
        "is_grayscale_page": bool(_is_grayscale_pil(img)),
        "chroma_var": float(_compute_chroma_var(img)),
    }


def _classify_series_content(features: "List[Dict[str, Any]]") -> str:
    """Classify a series's content_type from a list of per-page features.

    Decision order (first match wins):
      1. median_height > WEBTOON_SINGLE_IMAGE_HEIGHT → single-image webtoon
      2. width_cv < threshold + median_w < threshold + color_ratio > threshold
         → chunked webtoon
      3. grayscale_ratio > BW_MANGA_GRAYSCALE_RATIO → bw_manga
      4. 0.3 < grayscale_ratio < BW_MANGA_GRAYSCALE_RATIO → bw_manga_with_color_inserts
      5. color_ratio > 0.7 AND median_aspect < 0.85 → color_manga
      6. otherwise → "unknown"

    The width_cv check uses sample stdev so it requires n >= 2. Single-
    sample input (e.g., the rare cover-only fallback case) reports CV=0
    which trivially passes the chunked-webtoon check; the median_w and
    color_ratio gates still need to agree, so "unknown" is the typical
    single-sample outcome.

    Cross-file: thresholds live in sites/t1_constants.py.
    """
    from statistics import median, stdev
    from . import t1_constants as _t1c
    n = len(features)
    if n == 0:
        return "unknown"

    median_w = float(median(f["width"] for f in features))
    median_h = float(median(f["height"] for f in features))
    median_aspect = float(median(f["aspect"] for f in features))
    if n > 1 and median_w > 0:
        width_cv = float(stdev(f["width"] for f in features) / median_w)
    else:
        width_cv = 0.0
    color_ratio = sum(
        1 for f in features
        if f["chroma_var"] > _t1c.CHROMA_VARIANCE_THRESHOLD
    ) / n
    grayscale_ratio = sum(1 for f in features if f["is_grayscale_page"]) / n

    # Rule 1: single-image vertical scroll webtoon (rare).
    if median_h > _t1c.WEBTOON_SINGLE_IMAGE_HEIGHT:
        return "color_webtoon_single_image"

    # Rule 2: chunked webtoon. Requires narrow + consistent + color.
    if (
        median_w < _t1c.WEBTOON_MAX_WIDTH
        and width_cv < _t1c.WEBTOON_WIDTH_CONSISTENCY_CV
        and color_ratio > _t1c.WEBTOON_COLOR_RATIO_THRESHOLD
    ):
        return "color_webtoon_chunked"

    # Rule 3-4: B&W manga decisions based on grayscale ratio.
    if grayscale_ratio > _t1c.BW_MANGA_GRAYSCALE_RATIO:
        return "bw_manga"
    if 0.3 < grayscale_ratio <= _t1c.BW_MANGA_GRAYSCALE_RATIO:
        return "bw_manga_with_color_inserts"

    # Rule 5: color manga (portrait aspect).
    if color_ratio > 0.7 and median_aspect < 0.85:
        return "color_manga"

    return "unknown"


def _bpp_decode_quality(
    width: int, height: int, size_bytes: int, is_grayscale: bool
) -> float:
    """Phase H3 (2026-05-08): convert bytes-per-pixel into a 0..1
    decode-quality estimate, with separate curves for grayscale (B&W
    manga compresses much smaller at the same encoder quality) vs
    color content. Caller is responsible for detecting grayscale via
    _is_grayscale_pil.

    Curves below come from the calibrate_quality_probe.py run on
    Talentless Nana ch ~60 across atsumaru (WebP) and mangafire (JPEG):

      grayscale (calibrated): (bpp - 0.05) / 0.20 → 0.05 bpp = 0.0,
                                                     0.25 bpp = 1.0
        - atsumaru 0.18 bpp → 0.65    (lossy WebP B&W, the "low" baseline)
        - mangafire 0.26 bpp → 1.0   (mid-range JPEG B&W, doesn't apply
                                       — JPEG uses QT path — but bpp
                                       lands above the ceiling for this
                                       curve, validating the band)
        - clean B&W q=90 WebP at 0.10 bpp → 0.25  (poor)
        - lossless WebP at 0.50+ bpp → handled by H2 short-circuit, 1.0

      color     (starting):   (bpp - 0.08) / 0.40 → 0.08 bpp = 0.0,
                                                     0.48 bpp = 1.0
        - The test set didn't include a color manga, so this curve is
          unvalidated. Plausible-shaped based on expected encoder
          behavior; expect to retune after seeing real color-source
          probe data.

    Both clamped to [0.0, 1.0]. Below the floor = "fundamentally broken
    encoding"; above the ceiling = "near-lossless." The H4 outlier clip
    in _score_image_blob handles WebP < 0.05 bpp separately (clamps to
    0.1 and flags the metadata).

    Cross-file: called from _score_image_blob below for non-JPEG/non-
    lossless formats. JPEG keeps its quantization-table estimate
    (independent of bpp) augmented by H4's high-bpp clip.
    """
    pixels = max(1, width * height)
    bpp = size_bytes / pixels
    if is_grayscale:
        return max(0.0, min(1.0, (bpp - 0.05) / 0.20))
    return max(0.0, min(1.0, (bpp - 0.08) / 0.40))


def _jpeg_quality_estimate(quantization: Optional[Dict]) -> float:
    """Reverse-derive JPEG quality (0-100) from PIL's quantization tables.

    Higher values in the QT correspond to coarser quantization and thus
    lower quality. We use the average of the luminance table (key 0) as a
    proxy for the libjpeg quality factor, then map to bucketed quality.
    Robust enough for ranking — exact reverse-mapping isn't necessary.
    """
    if not quantization or not isinstance(quantization, dict):
        return 50.0
    qt = quantization.get(0)
    if not qt:
        # Some JPEGs only have chroma (key 1); use that as fallback
        qt = quantization.get(1)
    if not qt:
        return 50.0
    avg = sum(qt) / max(1, len(qt))
    if avg < 1.5:
        return 100.0
    if avg < 5.0:
        return 92.0
    if avg < 12.0:
        return 82.0
    if avg < 25.0:
        return 68.0
    if avg < 60.0:
        return 50.0
    return 30.0


# --- v5.1 Phase 5: AVIF/JXL format opt-in registration --------------------
# AVIF via pillow-avif-plugin (auto-registers on import). JXL via jxlpy as
# an OPTIONAL dep — when jxlpy is installed PIL gains JXL decode support;
# when it's missing JXL blobs fail to decode and _score_image_blob
# correctly returns None (graceful degrade — the orchestrator falls back
# to the seed prior for that source).
try:
    import pillow_avif as _pillow_avif  # noqa: F401 — import is for side-effect (PIL plugin register)
    _AVIF_AVAILABLE = True
except Exception:
    _AVIF_AVAILABLE = False

try:
    import jxlpy as _jxlpy  # noqa: F401
    _JXL_AVAILABLE = True
except Exception:
    _JXL_AVAILABLE = False


def _detect_chroma_subsampling(blob: bytes) -> Optional[str]:
    """Parse JPEG SOF marker to determine chroma subsampling.

    Returns "4:4:4", "4:2:2", "4:2:0", or None (not a JPEG, parse failed,
    or non-YCbCr colorspace). Direct byte-level parsing — no jpeglib
    dependency. JPEG marker spec: 0xFF 0xC0 (baseline SOF0) or 0xFF 0xC2
    (progressive SOF2) header indicates the component sampling factors.

    The chroma subsampling string is determined by the Y component's
    sampling factor:
      Y(2,2) Cb(1,1) Cr(1,1) → "4:2:0"  (most aggressive)
      Y(2,1) Cb(1,1) Cr(1,1) → "4:2:2"
      Y(1,1) Cb(1,1) Cr(1,1) → "4:4:4"  (no subsampling)

    Cross-file: invoked from _compute_chroma_penalty below. Skipped on
    non-JPEG containers (which can't have chroma subsampling by definition).
    """
    if not blob or len(blob) < 8:
        return None
    # JPEG magic: 0xFF 0xD8 (SOI marker).
    if blob[:2] != b"\xff\xd8":
        return None
    # Walk markers until we find SOF0 / SOF2.
    pos = 2
    while pos < len(blob) - 1:
        if blob[pos] != 0xFF:
            return None  # marker stream corrupted
        marker = blob[pos + 1]
        # Skip 0xFF padding bytes.
        if marker == 0xFF:
            pos += 1
            continue
        # SOI (0xD8) and EOI (0xD9) have no segment payload — but we
        # already consumed SOI at the start.
        if marker == 0xD9:
            return None  # EOI without finding SOF
        # SOF markers: 0xC0 (baseline), 0xC1 (extended), 0xC2 (progressive),
        # 0xC3 (lossless). Most JPEGs are 0xC0 or 0xC2.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            # Found SOF. Segment: 2-byte length, 1-byte precision,
            # 2-byte height, 2-byte width, 1-byte n_components, then
            # 3 bytes per component (ID, sampling, qtable).
            if pos + 10 > len(blob):
                return None
            seg_len = (blob[pos + 2] << 8) | blob[pos + 3]
            n_components = blob[pos + 9]
            if n_components != 3:
                # Grayscale (1) or CMYK (4) — chroma subsampling N/A.
                return None
            # Component descriptors start at pos + 10.
            comp_start = pos + 10
            if comp_start + 3 * n_components > len(blob):
                return None
            # Y is conventionally component 1 (the first descriptor).
            y_sampling = blob[comp_start + 1]
            y_h = (y_sampling >> 4) & 0x0F
            y_v = y_sampling & 0x0F
            if y_h == 1 and y_v == 1:
                return "4:4:4"
            if y_h == 2 and y_v == 1:
                return "4:2:2"
            if y_h == 2 and y_v == 2:
                return "4:2:0"
            # Other patterns (rare): 4:1:1, etc. Treat as unknown.
            return None
        # Generic segment: skip past its length-prefixed payload.
        if pos + 4 > len(blob):
            return None
        seg_len = (blob[pos + 2] << 8) | blob[pos + 3]
        if seg_len < 2:
            return None
        pos += 2 + seg_len
    return None


def _compute_chroma_penalty(
    blob: bytes, img_arr, content_type: str,
) -> "Tuple[float, Dict[str, Any]]":
    """Compute the v5.1 Phase 5 chroma-subsampling penalty.

    Returns (penalty, diag_dict). Penalty is in [0, CHROMA_PENALTY] and
    only triggers when:
      1. The format is JPEG with detectable 4:2:0 subsampling, AND
      2. The image content has meaningful chroma complexity (Cb/Cr std
         deviation above CHROMA_COMPLEXITY_THRESHOLD), AND
      3. content_type is color (B&W manga skips this — chroma irrelevant).

    Cross-file: invoked from _compute_t1_score for color content. Constants
    in sites/t1_constants.py.
    """
    from . import t1_constants as _t1c
    if content_type == "bw_manga":
        return 0.0, {"chroma_subsampling": None, "chroma_complexity": None}
    subsampling = _detect_chroma_subsampling(blob)
    if subsampling != "4:2:0":
        return 0.0, {"chroma_subsampling": subsampling, "chroma_complexity": None}
    # Compute chroma complexity in YCbCr space. Low complexity (mostly
    # uniform color) means 4:2:0 is visually acceptable; high complexity
    # (rich color gradients) suffers from the subsampling.
    if not _cv2_available():
        return 0.0, {"chroma_subsampling": subsampling, "chroma_complexity": None}
    import numpy as np
    if img_arr.ndim != 3 or img_arr.shape[-1] < 3:
        return 0.0, {"chroma_subsampling": subsampling, "chroma_complexity": None}
    try:
        ycbcr = _cv2.cvtColor(img_arr, _cv2.COLOR_RGB2YCrCb)
        cb_std = float(ycbcr[:, :, 2].std() / 255.0)
        cr_std = float(ycbcr[:, :, 1].std() / 255.0)
        chroma_complexity = (cb_std + cr_std) / 2.0
    except Exception:
        return 0.0, {"chroma_subsampling": subsampling, "chroma_complexity": None}
    if chroma_complexity < _t1c.CHROMA_COMPLEXITY_THRESHOLD:
        return 0.0, {"chroma_subsampling": subsampling,
                     "chroma_complexity": round(chroma_complexity, 4)}
    return _t1c.CHROMA_PENALTY, {
        "chroma_subsampling": subsampling,
        "chroma_complexity": round(chroma_complexity, 4),
    }


# --- Tier-1 scoring helpers (v5; 2026-05-17) ---------------------------
# Per the plan at ~/.claude/plans/how-robust-is-the-memoized-koala.md the
# old "0.4*res + 0.3*format + 0.3*decode" formula is replaced by an
# objective 5-component composite. These helpers run on every probed page
# (~80 ms/page total on a 1500×2100 manga page). All operate on PIL Images
# + a numpy view; numpy is now an explicit dep in requirements.txt.
#
# Why the redesign: see plan's Context section. Headline failures of the
# old formula: 5 pages from 1 chapter (variance source ignored); JPEG QF
# bucketed at 5 levels (0.339 bpp pristine JPEG scored identical to
# 0.176 bpp degraded version); no upscaling detection (aggregators that
# bilinear-upscale 720p → 1500px were rewarded with a higher res_score);
# no actual sharpness measurement.

# Standard ITU T.81 Annex K luminance + chrominance quantization tables.
# Reference values that Hass 2024 LSM compares the file's actual QT against
# to back-solve the original encoder Q. Lazy-loaded inside the function
# to keep module-load cheap when scoring isn't running.
_STD_LUM_QT: "Optional[Any]" = None
_STD_CHROM_QT: "Optional[Any]" = None


def _get_standard_jpeg_tables():
    """Return (std_lum_qt, std_chrom_qt) as 8×8 numpy arrays.

    Cached on first call; reused for every JPEG scored. Tables from ITU
    T.81 Annex K — also the libjpeg default and what `cjpeg -quality 50`
    emits. Hass 2024 LSM scales these by the Q-derived S factor and
    compares to the actual file's QTs to back-solve Q.
    """
    global _STD_LUM_QT, _STD_CHROM_QT
    if _STD_LUM_QT is None:
        import numpy as np
        _STD_LUM_QT = np.array([
            [16, 11, 10, 16, 24, 40, 51, 61],
            [12, 12, 14, 19, 26, 58, 60, 55],
            [14, 13, 16, 24, 40, 57, 69, 56],
            [14, 17, 22, 29, 51, 87, 80, 62],
            [18, 22, 37, 56, 68, 109, 103, 77],
            [24, 35, 55, 64, 81, 104, 113, 92],
            [49, 64, 78, 87, 103, 121, 120, 101],
            [72, 92, 95, 98, 112, 100, 103, 99],
        ], dtype=np.int32)
        _STD_CHROM_QT = np.array([
            [17, 18, 24, 47, 99, 99, 99, 99],
            [18, 21, 26, 66, 99, 99, 99, 99],
            [24, 26, 56, 99, 99, 99, 99, 99],
            [47, 66, 99, 99, 99, 99, 99, 99],
            [99, 99, 99, 99, 99, 99, 99, 99],
            [99, 99, 99, 99, 99, 99, 99, 99],
            [99, 99, 99, 99, 99, 99, 99, 99],
            [99, 99, 99, 99, 99, 99, 99, 99],
        ], dtype=np.int32)
    return _STD_LUM_QT, _STD_CHROM_QT


def _pil_quant_to_8x8(qt_obj):
    """Convert PIL's quantization-table representation to a flat 8×8 numpy array.

    PIL exposes `img.quantization` as a dict {table_index: tuple_or_list_of_64_ints}
    using zig-zag scan order. We need it in 8×8 natural form for LSM scoring.
    PIL doesn't apply zig-zag-to-natural reordering on read, but for the LSM
    algorithm what matters is element-wise SSE between the file's QT and the
    scaled standard QT — and the scaled standard QT is also in natural order.
    So we reshape into 8×8 and let LSM operate on like-vs-like.

    Returns None when the input is unusable. Length must be exactly 64.
    """
    if qt_obj is None:
        return None
    try:
        import numpy as np
        arr = np.asarray(list(qt_obj), dtype=np.int32)
        if arr.size != 64:
            return None
        return arr.reshape(8, 8)
    except Exception:
        return None


def _estimate_jpeg_qf_lsm(
    blob: bytes, img
) -> "Tuple[Optional[float], Optional[float]]":
    """Hass 2024 least-squares-matching JPEG quality-factor estimation.

    Returns (qf, nse) where qf ∈ [1, 100] (None on failure) and nse is the
    Nash-Sutcliffe efficiency confidence (None when not computable). nse < 0.5
    indicates a non-standard QT (Adobe/Photoshop/ImageMagick) and the qf
    estimate should be down-weighted by the caller.

    Algorithm (per https://bitsgalore.org/2024/10/30/jpeg-quality-estimation-using-simple-least-squares-matching-of-quantization-tables.html):
      For each candidate Q in [1, 100]:
        S = 5000/Q if Q<50 else 200-2*Q
        ref_lum = clip(floor((S * STD_LUM + 50)/100), 1, 255)
        sse_q = sum((file_qt - ref_lum)^2) [+ chroma SSE]
      Best Q = argmin(sse_q)
      NSE = 1 - best_sse / sum((file_qt - mean(file_qt))^2)

    We read QTs from PIL's `img.quantization` rather than jpeglib because PIL
    is already a hard dep, jpeglib requires native wheels (Windows install
    friction), and both yield identical QT data. Future work: add jpeglib
    as an optional dep for raw DCT-coefficient access (Phase 4 advanced
    JPEG quality refinement).

    Cross-file: called from _compute_t1_score below; callers must ensure
    fmt is JPEG before invoking.
    """
    import numpy as np
    quant = getattr(img, "quantization", None)
    if not quant or 0 not in quant:
        return None, None
    qt_lum = _pil_quant_to_8x8(quant.get(0))
    qt_chrom = _pil_quant_to_8x8(quant.get(1))
    if qt_lum is None:
        return None, None
    std_lum, std_chrom = _get_standard_jpeg_tables()

    best_q = 50
    best_sse = float("inf")
    for Q in range(1, 101):
        if Q < 50:
            S = 5000.0 / Q
        else:
            S = 200.0 - 2.0 * Q
        ref_lum = np.clip(np.floor((S * std_lum + 50) / 100), 1, 255)
        sse = float(((qt_lum - ref_lum) ** 2).sum())
        if qt_chrom is not None:
            ref_chrom = np.clip(np.floor((S * std_chrom + 50) / 100), 1, 255)
            sse += float(((qt_chrom - ref_chrom) ** 2).sum())
        if sse < best_sse:
            best_sse = sse
            best_q = Q

    # Nash-Sutcliffe efficiency. Denominator is the variance of qt_lum
    # around its mean — when the QT is constant (degenerate) we can't
    # compute confidence, so return None.
    qt_mean = float(qt_lum.mean())
    denom = float(((qt_lum - qt_mean) ** 2).sum())
    if denom <= 0:
        return float(best_q), None
    nse = 1.0 - best_sse / denom
    return float(best_q), float(nse)


def _compute_blockiness_wang(img) -> float:
    """Wang–Bovik 2002 NR JPEG blockiness via row/col FFT peak detection.

    Reasoning: JPEG creates discontinuities at 8×8 block boundaries — visible
    as periodic "edges" every 8 pixels. The 1D FFT of the mean-along-axis
    difference signal exposes these as peaks at n/8, 2n/8, 3n/8, 4n/8
    frequency bins. A clean image has flat-ish spectrum; a blocky one has
    pronounced 1/8-period peaks. Score = mean(peak_bins) / mean(non-peak_bins).

    Returns 0..1: 0 = no detectable blocking, 1 = severe (heavily-compressed
    low-quality JPEG). The (1-blockiness) value feeds the T1 composite at
    weight 0.15.

    Notes:
      - Works on any format (the algorithm doesn't care about JPEG provenance
        — webp/png with no blocking just score ~0).
      - Skips small images (<32 px on either axis) where the 8-pixel period
        can't reliably resolve.

    Cross-file: called from _compute_t1_score below; reference paper at
    https://ece.uwaterloo.ca/~z70wang/publications/icip02.pdf.
    """
    import numpy as np
    arr = np.asarray(img.convert("L"), dtype=np.int16)
    h, w = arr.shape
    if h < 32 or w < 32:
        return 0.0

    def _peak_ratio(sig: "np.ndarray") -> float:
        n = len(sig)
        if n < 16:
            return 0.0
        # Remove DC and take magnitude of real-FFT.
        spectrum = np.abs(np.fft.rfft(sig - sig.mean()))
        # Peak bins at n/8, 2n/8, 3n/8, 4n/8 (Nyquist).
        peak_idx = [round(k * n / 8) for k in range(1, 5)]
        peak_idx = [i for i in peak_idx if 0 < i < len(spectrum)]
        if not peak_idx:
            return 0.0
        peak_energy = float(spectrum[peak_idx].mean())
        # Non-peak: everything except DC (bin 0) and the peak bins ±2 neighborhood.
        mask = np.ones(len(spectrum), dtype=bool)
        mask[0] = False
        for p in peak_idx:
            for d in range(-2, 3):
                if 0 <= p + d < len(spectrum):
                    mask[p + d] = False
        non_peak_energy = float(spectrum[mask].mean()) if mask.any() else 1.0
        ratio = peak_energy / (non_peak_energy + 1e-6)
        # Calibration: ratio=1 → no blocking (0.0); ratio=10+ → severe (1.0).
        # Empirically the noise floor on clean PNGs sits around ratio=0.5-2;
        # heavily-blocked JPEGs cross ratio=5-15. Map (ratio - 1) / 9.
        return float(np.clip((ratio - 1.0) / 9.0, 0.0, 1.0))

    # Differences along rows expose vertical block boundaries (col discontinuities).
    diff_h = np.abs(np.diff(arr, axis=1)).astype(np.float32)
    sig_h = diff_h.mean(axis=0)
    # Differences along cols expose horizontal block boundaries (row discontinuities).
    diff_v = np.abs(np.diff(arr, axis=0)).astype(np.float32)
    sig_v = diff_v.mean(axis=1)
    return (_peak_ratio(sig_h) + _peak_ratio(sig_v)) / 2.0


def _compute_fft_hf_ratio(img) -> float:
    """Energy fraction in the outer 60% of the 2D FFT spectrum.

    Real high-resolution scans have substantial high-frequency content from
    line work, screentones, and sharp panel borders. Bilinear/Lanczos-upscaled
    images have almost no energy above the original Nyquist limit — the
    high-frequency bins are flat and near-zero. This metric exposes that
    asymmetry in ~30 ms per page.

    Returns 0..1 where 0.6-0.8 is typical for native scans, 0.15-0.30 is
    typical for upscaled-from-720p content. The value feeds the T1 composite
    at weight 0.15 (or 0.20 for WebP where there's no JPEG QF component).

    Skips images smaller than 32×32 (can't compute meaningful FFT bins) and
    returns 0.5 (neutral) in that case so they neither help nor hurt.

    Cross-file: implementation follows the classic FFT-based resampling-
    detection idea from Kirchner 2008 simplified to a single ratio.
    """
    import numpy as np
    arr = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = arr.shape
    if h < 32 or w < 32:
        return 0.5
    F = np.abs(np.fft.fftshift(np.fft.fft2(arr)))
    cy, cx = h // 2, w // 2
    # "Inner 40%": center ±20% on each axis. Outer 60% = 1 - inner_fraction.
    inner_h = max(1, h // 5)
    inner_w = max(1, w // 5)
    inner_energy = float(F[cy - inner_h:cy + inner_h, cx - inner_w:cx + inner_w].sum())
    total = float(F.sum())
    if total <= 0:
        return 0.5
    return float(np.clip(1.0 - inner_energy / total, 0.0, 1.0))


# --- v5.1 Phase 2: USM / fake-sharpness overshoot detection ----------------
# Algorithm (Cao, Zhao, Ni — IEEE SPL 2011): real edges have a smooth
# luminance ramp; USM-sharpened edges have characteristic overshoot
# (bright halo on the bright side, dark halo on the dark side). Sample
# perpendicular profiles at Canny-detected edges, count edges showing
# overshoot, average the fraction. Real B&W manga has overshoot ~0.05;
# Photoshop-USM or Real-ESRGAN-anime sharpened pages land 0.30-0.60.
#
# Score is used to DAMP Tenengrad (the existing T1 sharpness component)
# so fake-sharpened pages don't get an artificial Tenengrad boost from
# their overshoot rings:
#   tene_clean = tene_norm * (1 - clamp(0, 1, usm / USM_NORMALIZATION[content_type]))
#
# Per-content-type normalization in t1_constants.USM_NORMALIZATION.
# B&W manga has the sensitive threshold (line art shouldn't show
# overshoot naturally); webtoons have anti-aliased edges → lenient
# threshold. ~50 ms / page CPU; trivially small budget impact.


def _sample_perpendicular(gray, px: int, py: int, theta: float, length: int = 5):
    """Sample a luminance profile perpendicular to an edge.

    `theta` is the gradient angle (in radians) at the edge pixel. The
    perpendicular direction is rotated 90°: dx=cos(theta), dy=sin(theta).
    Returns a 1D numpy array of (2*length+1) intensity values centered on
    (px, py), or None when the line exits the image bounds.
    """
    import math
    import numpy as np
    h, w = gray.shape
    dx = math.cos(theta)
    dy = math.sin(theta)
    samples = []
    for offset in range(-length, length + 1):
        x = int(round(px + offset * dx))
        y = int(round(py + offset * dy))
        if x < 0 or y < 0 or x >= w or y >= h:
            return None
        samples.append(int(gray[y, x]))
    return np.asarray(samples, dtype=np.int32)


def _compute_usm_overshoot(
    img_arr, content_type: str = "unknown",
) -> "Tuple[float, Dict[str, Any]]":
    """Detect characteristic USM overshoot rings via Laplacian-ratio analysis.

    Returns (score, diagnostic_dict) where score is in [0, 1+] (higher =
    more USM-like sharpening signatures present). Damping band per
    content_type is in t1_constants.USM_NORMALIZATION.

    Algorithm: USM amplifies the Laplacian (2nd derivative) across the
    whole image — clean B&W manga has lap_var/img_var ~ 12; USM r=2 %=200
    pushes it to ~30; USM r=3 %=400 to ~33. The ratio of `Laplacian
    variance` to `image variance` is a robust scale-invariant proxy:

      raw_ratio = Var(Laplacian(gray)) / Var(gray)
      score = max(0, (raw_ratio - 12) / 20)

    The (-12)/20 mapping normalizes to ~[0, 1] for the operational range
    so the downstream USM_NORMALIZATION per-content-type band damps
    Tenengrad in [0, 1] cleanly. Calibrated against PIL UnsharpMask
    applied to tmp_nzmj + tmp_1571 fixtures (2026-05-17).

    Why not perpendicular-profile sampling (the original v5.1 plan):
    empirically, profiles of 11 pixels on real manga line-art cross
    multiple adjacent edges (manga has DENSE detail), so the plateau
    logic produces noisy garbage. Why not P95/P50 of |Lap| at edges:
    USM amplifies all edges uniformly, so the ratio actually COMPRESSES
    slightly (clean 2.86 → USM r2 2.32). Variance ratio expands cleanly
    because absolute Laplacian magnitudes increase faster than image
    pixel-variance under USM.

    Per-content-type Canny thresholds (anti-aliased webtoon edges are
    softer than B&W manga line art, so lower hysteresis):
      webtoon: (30, 100)
      manga / unknown: (50, 150)

    Skips when cv2 unavailable (returns 0.0, score-only). Cross-file:
    consumed by _compute_t1_score to derive `tenengrad_clean`.
    """
    if not _cv2_available():
        return 0.0, {"n_edges": 0, "edges_with_overshoot": 0,
                     "usm_unavailable": True}
    import numpy as np
    # Ensure grayscale 2D input.
    if img_arr.ndim == 3:
        gray = _cv2.cvtColor(img_arr, _cv2.COLOR_RGB2GRAY)
    else:
        gray = img_arr
    if gray.dtype != np.uint8:
        gray = gray.astype(np.uint8)

    # Content-type-aware Canny thresholds. Soft edges in webtoons need
    # lower thresholds to be detected at all.
    if content_type in ("color_webtoon_chunked", "color_webtoon_single_image"):
        low, high = 30, 100
    else:
        low, high = 50, 150

    edges = _cv2.Canny(gray, low, high)
    edge_count = int((edges > 0).sum())
    if edge_count < 100:
        # Not enough edges to compute a meaningful overshoot estimate
        # (uniform/blank pages). Score 0 = no fake-sharpness detected.
        return 0.0, {"n_edges": edge_count, "edges_with_overshoot": 0,
                     "lap_var_ratio": 0.0}

    # Laplacian and image variance over the whole canvas. 3×3 kernel
    # captures the single-pixel overshoot rings USM creates; ksize=5
    # would average them out.
    lap = _cv2.Laplacian(gray, _cv2.CV_64F, ksize=3)
    lap_var = float(lap.var())
    img_var = float(gray.astype(np.float64).var())
    if img_var < 1.0:
        # Near-uniform image (e.g. mostly-black title page) — variance
        # ratio degenerate. No meaningful USM signature available.
        return 0.0, {"n_edges": edge_count, "edges_with_overshoot": 0,
                     "lap_var_ratio": 0.0}

    raw_ratio = lap_var / img_var
    score = max(0.0, (raw_ratio - 12.0) / 20.0)
    # Count pixels where |Lap| > 3*sigma_lap → likely overshoot ring pixels.
    # Useful for diagnosis but doesn't feed the composite score.
    sigma = float(np.sqrt(lap_var))
    edges_with_overshoot = int((np.abs(lap) > 3.0 * sigma).sum())
    return score, {
        "n_edges": int(edge_count),
        "edges_with_overshoot": int(edges_with_overshoot),
        "lap_var_ratio": round(raw_ratio, 3),
    }


def _compute_tenengrad(img) -> "Tuple[float, float]":
    """Tenengrad sharpness measure: mean Sobel-gradient magnitude.

    Returns (raw_mean, normalized 0..1). Normalization clamps the raw value
    (typically 5-100 on 8-bit grayscale) to [0, 1] via raw/80.

    Why Tenengrad over Laplacian variance: per the 2024 OpenCV autofocus
    comparative study, Tenengrad is the most noise-robust of the three top
    focus measures (Tenengrad / Laplacian variance / Sobel+Variance). For
    manga specifically this matters because screentones are high-frequency
    near-noise content that the Laplacian operator amplifies (and screentones
    look like "noise" to a 4-neighbor Laplacian, dragging its variance up).
    Tenengrad's gradient-magnitude is steadier — it tracks actual edge
    density rather than per-pixel second-derivative noise.

    Pure numpy implementation (no OpenCV dep yet at T1 layer). Skips tiny
    images (<3×3) returning (0, 0).

    Cross-file: called from _compute_t1_score; reference at
    https://opencv.org/blog/autofocus-using-opencv-a-comparative-study-of-focus-measures-for-sharpness-assessment/.
    """
    import numpy as np
    arr = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = arr.shape
    if h < 3 or w < 3:
        return 0.0, 0.0
    # Hand-rolled 3×3 Sobel via slicing (faster than scipy.signal.convolve2d
    # for small kernels; avoids the OpenCV dep at T1).
    sx = (
        -1.0 * arr[:-2, :-2] + 1.0 * arr[:-2, 2:]
        + -2.0 * arr[1:-1, :-2] + 2.0 * arr[1:-1, 2:]
        + -1.0 * arr[2:, :-2] + 1.0 * arr[2:, 2:]
    )
    sy = (
        -1.0 * arr[:-2, :-2] + -2.0 * arr[:-2, 1:-1] + -1.0 * arr[:-2, 2:]
        + 1.0 * arr[2:, :-2] + 2.0 * arr[2:, 1:-1] + 1.0 * arr[2:, 2:]
    )
    mag = np.sqrt(sx * sx + sy * sy)
    raw = float(mag.mean())
    # Calibration target: a sharp 1500×2100 manga page lands ~50-80; a blurred
    # version (Gaussian σ=2-3) lands ~10-25; a heavily-upscaled image lands
    # in the same low range. The /80 normalization puts the "good" band near
    # 0.6-1.0 and the "weak" band near 0.1-0.3.
    return raw, float(min(1.0, raw / 80.0))


def _compute_t1_score(
    blob: bytes,
    img,
    fmt: str,
    width: int,
    height: int,
    is_grayscale: bool,
    is_lossless: bool,
    content_type: str = "unknown",
) -> "Tuple[float, Dict]":
    """Tier-1 composite score (per-image, ~80 ms). Returns (score, metadata).

    v5.1: per-content-type weights + per-content-type res_norm target.
    Weights and targets live in sites/t1_constants.py. The content_type
    parameter defaults to "unknown" for backward compatibility with v5
    callers that don't pass it; "unknown" maps to the same weights/target
    as "bw_manga" so v5 callers see no behavior change.

    Per-format formula structure (specific weights per content_type in
    sites/t1_constants.py:T1_WEIGHTS_*):
      JPEG:           w_res*res + w_qf*jpeg_qf + w_block*(1-block) + w_fft*fft_hf + w_tene*tene
      PNG/WebP-VP8L:  w_res*res + w_lossless*1.0 + w_block*(1-block) + w_fft*fft_hf + w_tene*tene
      WebP/AVIF/etc:  w_res*res + w_block*(1-block) + w_fft*fft_hf + w_tene*tene

    res_norm uses ADAPTIVE area-based normalization (v5.1) instead of v5's
    width-based linear band [800, 2400]. v5's formula silently under-scored
    chunked webtoons (700-800 px wide pages → res_norm 0.0). v5.1 keys
    against RES_NORM_TARGETS[content_type] in t1_constants — so a 700×1100
    Eleceed page in webtoon context scores ~0.85 res_norm against the
    Eleceed-grounded 900k target.

    Why no format_bonus anymore: modern WebP q85 is visually indistinguishable
    from JPEG q95 (we measured this in bench/webtoon_encode_bench.py — SSIM
    0.994). Penalizing JPEG with format=0.55 vs WebP=0.85 was a 30% punishment
    on equivalent quality. The new formula judges files on what they actually
    show, not their container.

    The returned metadata dict carries every component so the UI tooltip and
    calibration tooling can show "why this score?" without recomputing. Shape
    is the v5 schema documented in ImageQualityCache class docstring,
    extended with v5.1 fields (`content_type`, `res_norm_target`).

    Cross-file: called only from _score_image_blob below; v5/v6 cache reads
    /writes this metadata verbatim.
    """
    import numpy as np
    from . import t1_constants as _t1c
    # v5.1 area-based res_norm. Target keyed by content_type; "unknown"
    # falls back to the bw_manga 2M-px reference.
    res_target = _t1c.RES_NORM_TARGETS.get(
        content_type, _t1c.RES_NORM_TARGETS["unknown"]
    )
    area = max(1, int(width) * int(height))
    res_norm = float(np.clip(area / res_target, 0.0, 1.0))

    blockiness = _compute_blockiness_wang(img)
    fft_hf = _compute_fft_hf_ratio(img)
    tene_raw, tene_norm = _compute_tenengrad(img)
    bpp = len(blob) / max(1, width * height)

    # v5.1 Phase 2: USM overshoot detection + Tenengrad damping. Real
    # high-resolution detail has smooth edge profiles; USM-sharpened or
    # Real-ESRGAN-upscaled content has characteristic overshoot rings.
    # The score damps Tenengrad so fake-sharpened pages don't get an
    # artificial Tenengrad boost from the overshoot artifacts themselves.
    img_arr_for_usm = np.asarray(img.convert("RGB"), dtype=np.uint8) if img.mode != "L" else np.asarray(img, dtype=np.uint8)
    try:
        usm_score, usm_diag = _compute_usm_overshoot(img_arr_for_usm, content_type=content_type)
    except Exception:
        usm_score, usm_diag = 0.0, {"n_edges": 0, "edges_with_overshoot": 0}
    usm_norm_target = _t1c.USM_NORMALIZATION.get(
        content_type, _t1c.USM_NORMALIZATION["unknown"]
    )
    # tene_clean = tene_norm * (1 - clamp(0, 1, usm / target))
    # When usm == 0 → tene_clean == tene_norm (full credit for legitimate sharpness).
    # When usm >= target → tene_clean == 0 (zero credit; fake sharpness neutralized).
    usm_damp = float(np.clip(usm_score / max(usm_norm_target, 1e-6), 0.0, 1.0))
    tene_clean = tene_norm * (1.0 - usm_damp)

    metadata: "Dict[str, Any]" = {
        "width": int(width),
        "height": int(height),
        "format": fmt,
        "size_bytes": int(len(blob)),
        "is_grayscale": bool(is_grayscale),
        "is_lossless": bool(is_lossless),
        "bpp": round(bpp, 4),
        "res_norm": round(res_norm, 4),
        "res_norm_target": int(res_target),
        "blockiness": round(blockiness, 4),
        "fft_hf_ratio": round(fft_hf, 4),
        "tenengrad": round(tene_raw, 4),
        "tenengrad_norm": round(tene_norm, 4),
        # v5.1 Phase 2 USM fields. tenengrad_clean is what feeds the T1
        # formula; tenengrad_norm is the pre-damping raw value (surfaced
        # for diagnosis). usm_overshoot_score is the raw mean overshoot.
        "tenengrad_clean": round(tene_clean, 4),
        "usm_overshoot_score": round(float(usm_score), 4),
        "usm_n_edges": int(usm_diag.get("n_edges", 0)),
        "usm_edges_with_overshoot": int(usm_diag.get("edges_with_overshoot", 0)),
        "content_type": content_type,  # v5.1 — surfaced for UI + cache
    }
    # Outlier flag: when USM is strong (>= 0.8 × normalization band), mark
    # as fake_sharpened. UI surfaces via the existing AlertTriangle pattern.
    if usm_score > 0.8 * usm_norm_target:
        metadata.setdefault("outlier", "fake_sharpened")

    if is_lossless:
        # Lossless branch: decode-quality substitute = 1.0 (no encoder loss).
        # Per-content-type weights via T1_WEIGHTS_LOSSLESS table.
        w = _t1c.T1_WEIGHTS_LOSSLESS.get(
            content_type, _t1c.T1_WEIGHTS_LOSSLESS["unknown"]
        )
        decode_substitute = 1.0
        t1 = (
            w["res"] * res_norm
            + w["lossless"] * decode_substitute
            + w["block"] * (1.0 - blockiness)
            + w["fft_hf"] * fft_hf
            + w["tene"] * tene_clean
        )
        metadata["jpeg_qf"] = None
        metadata["jpeg_qf_norm"] = None
        metadata["jpeg_nse"] = None
        metadata["decode_quality"] = round(decode_substitute, 4)
    elif fmt in ("JPEG", "JPG"):
        w = _t1c.T1_WEIGHTS_JPEG.get(
            content_type, _t1c.T1_WEIGHTS_JPEG["unknown"]
        )
        qf, nse = _estimate_jpeg_qf_lsm(blob, img)
        qf_norm: Optional[float] = None
        if qf is not None:
            qf_norm = float(np.clip(qf / 100.0, 0.0, 1.0))
            # NSE gating: when the QTs are non-standard (Adobe/Photoshop
            # produces NSE ~0.3-0.5), blend the LSM estimate toward a
            # middling 0.7 so we don't over-trust it. NSE < 0 indicates
            # the LSM fit is worse than the mean — basically a guess.
            if nse is not None and nse < 0.5:
                taper = max(0.0, nse / 0.5)
                qf_norm = taper * qf_norm + (1.0 - taper) * 0.7
        else:
            # Fall back to the legacy bucketed QT estimate.
            quant = getattr(img, "quantization", None)
            fallback_qf = _jpeg_quality_estimate(quant)
            qf_norm = float(np.clip(fallback_qf / 100.0, 0.0, 1.0))
            qf = fallback_qf
        t1 = (
            w["res"] * res_norm
            + w["qf"] * (qf_norm or 0.5)
            + w["block"] * (1.0 - blockiness)
            + w["fft_hf"] * fft_hf
            + w["tene"] * tene_clean
        )
        metadata["jpeg_qf"] = float(qf) if qf is not None else None
        metadata["jpeg_qf_norm"] = round(float(qf_norm), 4) if qf_norm is not None else None
        metadata["jpeg_nse"] = round(float(nse), 4) if nse is not None else None
        # Back-compat field for UI (SearchSourceCard.jsx tooltip): for JPEG
        # we mirror qf_norm into decode_quality.
        metadata["decode_quality"] = round(float(qf_norm), 4) if qf_norm is not None else None
    else:
        # Lossy WebP / AVIF / JXL / unknown — no JPEG QF available, reweight
        # via T1_WEIGHTS_WEBP table. AVIF additionally gets a small quality
        # premium (AVIF_QUALITY_PREMIUM = 1.05) because at matched bpp it
        # produces visually higher quality than WebP/JPEG.
        w = _t1c.T1_WEIGHTS_WEBP.get(
            content_type, _t1c.T1_WEIGHTS_WEBP["unknown"]
        )
        t1 = (
            w["res"] * res_norm
            + w["block"] * (1.0 - blockiness)
            + w["fft_hf"] * fft_hf
            + w["tene"] * tene_clean
        )
        # v5.1 Phase 5: AVIF quality premium. Small bonus for the most
        # encoder-efficient lossy format. JXL gets the same treatment if
        # we ever expand to track it separately (currently scored as WebP).
        if fmt in ("AVIF",):
            t1 = min(1.0, t1 * _t1c.AVIF_QUALITY_PREMIUM)
        metadata["jpeg_qf"] = None
        metadata["jpeg_qf_norm"] = None
        metadata["jpeg_nse"] = None
        # Synthesize a decode_quality proxy from sharpness + (1-blockiness).
        metadata["decode_quality"] = round((tene_norm + (1.0 - blockiness)) / 2.0, 4)

    # WebP-below-floor outlier flag preserved from v4.
    if fmt == "WEBP" and bpp < 0.05:
        metadata["outlier"] = "webp_below_floor"
        t1 = min(t1, 0.1)

    # v5.1 Phase 5: chroma subsampling penalty (color content + JPEG only).
    # B&W manga skips this since chroma is irrelevant. Penalty is small
    # (0.03) and stacks with watermark / USM penalties in _score_image_blob.
    img_arr_for_chroma = None
    if fmt in ("JPEG", "JPG"):
        try:
            img_arr_for_chroma = np.asarray(img.convert("RGB"), dtype=np.uint8)
        except Exception:
            img_arr_for_chroma = None
    if img_arr_for_chroma is not None:
        try:
            chroma_pen, chroma_diag = _compute_chroma_penalty(
                blob, img_arr_for_chroma, content_type,
            )
        except Exception:
            chroma_pen, chroma_diag = 0.0, {"chroma_subsampling": None,
                                             "chroma_complexity": None}
    else:
        chroma_pen, chroma_diag = 0.0, {"chroma_subsampling": None,
                                         "chroma_complexity": None}
    if chroma_pen > 0.0:
        t1 = max(0.0, t1 - chroma_pen)
        # Outlier flag for the UI: only set when no other outlier is
        # already in place (the existing watermark / webp_below_floor /
        # fake_sharpened flags take priority).
        metadata.setdefault("outlier", "low_chroma")
    metadata["chroma_penalty"] = round(float(chroma_pen), 4)
    metadata["chroma_subsampling"] = chroma_diag.get("chroma_subsampling")
    metadata["chroma_complexity"] = chroma_diag.get("chroma_complexity")

    t1 = float(max(0.0, min(1.0, t1)))
    metadata["t1_score"] = round(t1, 4)
    return t1, metadata


# --- v6 B&W manga T1 branch (2026-05-18) -----------------------------------
# Specialized T1 formula for content_type ∈ {bw_manga, bw_manga_with_color_inserts}.
# Drops the generic fft_hf / tenengrad-dominant T1 in favor of B&W-specific
# signals from sites/bw_signals.py:
#   - screentone band-energy fraction (the dominant B&W feature class)
#   - edge-conditioned ringing (sharpness − annulus variance at strokes)
#   - upscaler fingerprint penalty (half-Nyquist bump + asymmetric overshoot)
#   - background uniformity (Otsu high-mode MAD)
#   - bilevel-storage penalty (G4 / mode='1' / heavy histogram bimodality)
#   - chroma-subsampling penalty (4:2:0 forced on B&W = laundering signal)
#   - lossless bonus (PNG / lossless WebP genuinely better for B&W)
#   - resolution-decay shape that pushes obvious upscales back toward FLOOR
#
# Weights live in sites/t1_constants.py:T1_WEIGHTS_{JPEG,WEBP,LOSSLESS}_BW.
# Penalty constants in the same module.
#
# Cross-file: dispatched from _score_image_blob below; only fires when
# `_classify_series_content` (search_orchestrator.py:_classify_series_content,
# line ~2691) returns bw_manga or bw_manga_with_color_inserts. Legacy
# content types (color_manga, color_webtoon_*, unknown) stay on
# _compute_t1_score's per-format formulas.


def _compute_t1_score_bw(
    blob: bytes,
    img,
    fmt: str,
    width: int,
    height: int,
    is_grayscale: bool,
    is_lossless: bool,
    content_type: str,
) -> "Tuple[float, Dict]":
    """B&W-specialized T1 (v6). Returns (score, metadata) — same contract
    as _compute_t1_score so _score_image_blob's downstream T2/watermark
    pipeline plumbs through unchanged.

    metadata schema is a superset of the v5 schema: every v5 key is
    present (set to None when the B&W formula doesn't use it) plus a
    handful of bw_* keys carrying the new sub-scores. UI tooltips and
    cache audits don't need feature-detection.
    """
    import numpy as np
    from . import t1_constants as _t1c
    from . import bw_signals as _bw

    bpp = len(blob) / max(1, width * height)

    # 1. Resolution norm with v6 decay (pushes obvious upscales back).
    res_target = _t1c.RES_NORM_TARGETS.get(content_type, _t1c.RES_NORM_TARGETS["unknown"])
    area = max(1, int(width) * int(height))
    area_ratio = area / res_target
    if area_ratio <= 1.0:
        res_norm = float(max(0.0, min(1.0, area_ratio)))
    elif area_ratio <= _t1c.RES_NORM_DECAY_START:
        res_norm = 1.0  # safe zone — no decay
    elif area_ratio >= _t1c.RES_NORM_DECAY_END:
        res_norm = float(_t1c.RES_NORM_FLOOR)
    else:
        span = _t1c.RES_NORM_DECAY_END - _t1c.RES_NORM_DECAY_START
        progress = (area_ratio - _t1c.RES_NORM_DECAY_START) / span
        res_norm = float(1.0 - progress * (1.0 - _t1c.RES_NORM_FLOOR))

    # 2. JPEG QF estimation (only for JPEG; otherwise None).
    jpeg_qf: Optional[float] = None
    jpeg_qf_norm: Optional[float] = None
    jpeg_nse: Optional[float] = None
    if fmt in ("JPEG", "JPG"):
        try:
            qf, nse = _estimate_jpeg_qf_lsm(blob, img)
            if qf is not None:
                jpeg_qf = float(qf)
                jpeg_qf_norm = float(np.clip(qf / 100.0, 0.0, 1.0))
                if nse is not None and nse < 0.5:
                    # NSE blend (same logic as legacy _compute_t1_score):
                    # taper toward 0.7 when QT is non-standard.
                    taper = max(0.0, nse / 0.5)
                    jpeg_qf_norm = taper * jpeg_qf_norm + (1.0 - taper) * 0.7
                jpeg_nse = float(nse) if nse is not None else None
        except Exception:
            pass

    # 3. Blockiness (existing primitive — reused for B&W too).
    try:
        blockiness = _compute_blockiness_wang(img)
    except Exception:
        blockiness = 0.0

    # 4. Tenengrad with USM damping (existing primitive — reused).
    try:
        tene_raw, tene_norm = _compute_tenengrad(img)
    except Exception:
        tene_raw, tene_norm = 0.0, 0.0
    try:
        img_arr_for_usm = (
            np.asarray(img.convert("RGB"), dtype=np.uint8)
            if img.mode != "L"
            else np.asarray(img, dtype=np.uint8)
        )
        usm_score, usm_diag = _compute_usm_overshoot(img_arr_for_usm, content_type=content_type)
    except Exception:
        usm_score, usm_diag = 0.0, {"n_edges": 0, "edges_with_overshoot": 0}
    usm_norm_target = _t1c.USM_NORMALIZATION.get(content_type, _t1c.USM_NORMALIZATION["unknown"])
    usm_damp = float(np.clip(usm_score / max(usm_norm_target, 1e-6), 0.0, 1.0))
    tene_clean = tene_norm * (1.0 - usm_damp)

    # 5. New B&W primitives.
    screentone_score, screentone_meta = _bw.compute_screentone_integrity(img, content_type)
    line_score, line_meta = _bw.compute_line_quality(img)
    upscaler_score, upscaler_meta = _bw.compute_upscaler_score(img)
    storage = _bw.compute_bw_storage_signals(img, blob, fmt)

    # 6. Pick weights table.
    if is_lossless:
        weights_table = _t1c.T1_WEIGHTS_LOSSLESS_BW
    elif fmt in ("JPEG", "JPG"):
        weights_table = _t1c.T1_WEIGHTS_JPEG_BW
    else:
        weights_table = _t1c.T1_WEIGHTS_WEBP_BW
    w = weights_table.get(content_type, weights_table.get("bw_manga"))

    # 7. Weighted-sum composite. Common slots first, format-specific slot last.
    composite = (
        w["res"]    * res_norm
        + w["line"]   * line_score
        + w["screen"] * screentone_score
        + w["bg"]     * storage["bg_uniformity"]
        + w["block"]  * (1.0 - blockiness)
        + w["tene"]   * tene_clean
    )
    if "qf" in w:
        composite += w["qf"] * (jpeg_qf_norm if jpeg_qf_norm is not None else 0.5)
    elif "lossless" in w:
        composite += w["lossless"] * 1.0  # lossless = perfect decode quality

    # 8. Penalties (subtract) and bonuses (add). All bounded by t1_constants.
    upscaler_penalty = _t1c.UPSCALER_PENALTY_MAX * float(upscaler_score)
    bilevel_penalty = _t1c.BILEVEL_PENALTY_BW if storage["bilevel"] else 0.0
    chroma_penalty = (
        _t1c.CHROMA_PENALTY_BW
        if (fmt in ("JPEG", "JPG") and storage["chroma_subsampled"] == "4:2:0")
        else 0.0
    )
    lossless_bonus = _t1c.LOSSLESS_BONUS_BW if is_lossless else 0.0
    t1 = composite - upscaler_penalty - bilevel_penalty - chroma_penalty + lossless_bonus
    t1 = float(max(0.0, min(1.0, t1)))

    # 9. Build the v5-compatible metadata dict plus bw_* additions.
    metadata: "Dict[str, Any]" = {
        "width": int(width),
        "height": int(height),
        "format": fmt,
        "size_bytes": int(len(blob)),
        "is_grayscale": bool(is_grayscale),
        "is_lossless": bool(is_lossless),
        "bpp": round(bpp, 4),
        "res_norm": round(res_norm, 4),
        "res_norm_target": int(res_target),
        "blockiness": round(blockiness, 4),
        "fft_hf_ratio": None,  # subsumed by screentone integrity in B&W formula
        "tenengrad": round(tene_raw, 4),
        "tenengrad_norm": round(tene_norm, 4),
        "tenengrad_clean": round(tene_clean, 4),
        "usm_overshoot_score": round(float(usm_score), 4),
        "usm_n_edges": int(usm_diag.get("n_edges", 0)),
        "usm_edges_with_overshoot": int(usm_diag.get("edges_with_overshoot", 0)),
        "content_type": content_type,
        "jpeg_qf": jpeg_qf,
        "jpeg_qf_norm": round(jpeg_qf_norm, 4) if jpeg_qf_norm is not None else None,
        "jpeg_nse": round(jpeg_nse, 4) if jpeg_nse is not None else None,
        "decode_quality": round(jpeg_qf_norm, 4) if jpeg_qf_norm is not None else (1.0 if is_lossless else None),
        # B&W-specific sub-scores (new in v6):
        "bw_screentone_score": round(float(screentone_score), 4),
        "bw_screentone_meta": screentone_meta,
        "bw_line_quality": round(float(line_score), 4),
        "bw_line_meta": line_meta,
        "bw_upscaler_score": round(float(upscaler_score), 4),
        "bw_upscaler_meta": upscaler_meta,
        "bw_bilevel": bool(storage["bilevel"]),
        "bw_chroma_subsampled": storage["chroma_subsampled"],
        "bw_bg_uniformity": float(storage["bg_uniformity"]),
        "bw_gutter_shadow_score": float(storage["gutter_shadow_score"]),
        "bw_speckle_density": float(storage["speckle_density"]),
        "bw_upscaler_penalty": round(upscaler_penalty, 4),
        "bw_bilevel_penalty": round(bilevel_penalty, 4),
        "bw_chroma_penalty": round(chroma_penalty, 4),
        "bw_lossless_bonus": round(lossless_bonus, 4),
        "t1_score": round(t1, 4),
    }
    # v5.1 USM fake-sharpened outlier flag (preserved for UI consistency).
    if usm_score > 0.8 * usm_norm_target:
        metadata["outlier"] = "fake_sharpened"
    # Surface bilevel as an outlier so the UI badge can render.
    if storage["bilevel"]:
        metadata.setdefault("outlier", "bilevel_storage")
    if upscaler_score > 0.7:
        metadata.setdefault("outlier", "ai_upscale_suspected")

    return t1, metadata


def _score_image_blob(
    blob: bytes, content_type: str = "unknown",
    *,
    is_cover_probe: bool = False,
) -> Optional[Tuple[float, Dict]]:
    """Score an image blob 0..1; return (score, metadata) or None on failure.

    v5 (2026-05-17): rewritten to use the T1 composite formula via
    _compute_t1_score. The legacy 3-component formula
    `0.4*res + 0.3*format + 0.3*decode` is gone; per-format objective formula
    documented in _compute_t1_score's docstring takes over.

    v5.1 (2026-05-17): adds `content_type` parameter so per-content-type
    weights + area-based res_norm targets get used. Defaults to "unknown"
    (== bw_manga weights, 2M-px res_norm target) for backward compatibility
    with v5 callers. The probe orchestrator passes the classified
    content_type after Phase 4's `_classify_series_content`. Direct callers
    (calibration script, tests) can pass an explicit content_type to
    exercise the webtoon/manga branches.

    v7 (2026-05-18): adds keyword-only `is_cover_probe` flag. When True,
    the watermark detection step is skipped entirely (placeholders are
    written as if easyocr were unavailable). Cover artwork is designed
    with large title/logo text in the exact regions our detector crops,
    so running watermark detection on it categorically produces
    false-positive `heavy_watermark` flags. The chapter-page probe path
    (sites/base.py:_probe_chapter_aggregate) keeps the default False.
    Only the cover-probe fallback at the bottom of `_probe_one`
    (sites/search_orchestrator.py:~4310) passes True.

    Metadata returned includes every component of T1 plus T2/T3 placeholders
    (None) so the cache schema and the UI tooltip don't need to feature-
    detect — the contract is: every field is present, nulls are explicit.

    Returns None when PIL can't decode (corrupt, truncated, or unsupported
    format). Caller falls back to seed_quality in that case (per the
    comparator's `_quality_for(s) -> s.img_quality_score if ... is not None
    else s.seed_quality` contract).
    """
    if not blob or len(blob) < 256:
        return None
    try:
        from PIL import Image  # local import to avoid module-load cost
        import io
        img = Image.open(io.BytesIO(blob))
        img.load()
        # Normalize palette-with-tRNS to RGBA before any downstream
        # `.convert("RGB")` (in T2 tensor prep, chroma_var downsample, or
        # USM analysis). PIL raises a UserWarning at convert-time when
        # the source is P-mode with byte-encoded transparency suggesting
        # the caller "should" promote to RGBA first — so we do exactly
        # that here, once, at the canonical decode point.
        if img.mode == "P" and "transparency" in img.info:
            img = img.convert("RGBA")
    except Exception:
        return None

    try:
        width, height = img.size
        fmt = (img.format or "UNKNOWN").upper()
    except Exception:
        return None

    if width < 100 or height < 100:
        return None  # likely a placeholder/icon, not a real cover

    # Detect lossless + grayscale once; both feed _compute_t1_score's
    # per-format branch + metadata.
    lossless = _detect_lossless_blob(blob)
    is_grayscale = _is_grayscale_pil(img)
    is_lossless = (lossless is True)

    # v5.1: chroma variance is needed by _classify_series_content to
    # discriminate color webtoons from B&W manga. Compute once here; the
    # classifier reads it from per-page metadata in _probe_chapter_aggregate.
    # ~2 ms on a 1500×2000 page (Lanczos downscale dominates).
    try:
        chroma_var = _compute_chroma_var(img)
    except Exception:
        chroma_var = 0.0

    try:
        # v6 (2026-05-18): B&W content_types dispatch to the specialized
        # _compute_t1_score_bw branch (sites/bw_signals.py-backed). Color
        # manga / webtoons / unknown stay on the v5.1 _compute_t1_score
        # per-format formula. The dispatch happens here (not inside
        # _compute_t1_score) so external callers that target
        # _compute_t1_score directly still get the legacy behavior —
        # critical for back-compat with tests/calibration tooling.
        if content_type in ("bw_manga", "bw_manga_with_color_inserts"):
            t1, metadata = _compute_t1_score_bw(
                blob, img, fmt, width, height, is_grayscale, is_lossless,
                content_type,
            )
        else:
            t1, metadata = _compute_t1_score(
                blob, img, fmt, width, height, is_grayscale, is_lossless,
                content_type=content_type,
            )
    except Exception:
        # numpy / PIL hiccup on a malformed image — degrade to the legacy
        # bpp/format heuristic so the orchestrator still gets a number.
        # This keeps the probe-failure cache happy: we'd rather return a
        # weak signal than None (which forces seed-prior fallback).
        bpp_fb = len(blob) / max(1, width * height)
        format_bonus_fb = _FORMAT_BONUS.get(fmt, 0.4)
        if is_lossless:
            decode_fb = 1.0
        elif fmt in ("JPEG", "JPG"):
            decode_fb = max(0.0, min(1.0, _jpeg_quality_estimate(
                getattr(img, "quantization", None)) / 95.0))
        else:
            decode_fb = _bpp_decode_quality(width, height, len(blob), is_grayscale)
        res_fb = max(0.0, min(1.0, (width - 800) / 1600))
        t1 = max(0.0, min(1.0, 0.4 * res_fb + 0.3 * format_bonus_fb + 0.3 * decode_fb))
        metadata = {
            "width": int(width),
            "height": int(height),
            "format": fmt,
            "size_bytes": int(len(blob)),
            "is_grayscale": bool(is_grayscale),
            "is_lossless": bool(is_lossless),
            "bpp": round(bpp_fb, 4),
            "decode_quality": round(decode_fb, 4),
            "t1_score": round(t1, 4),
            "t1_fallback_used": True,
            # REQUIRED_V6_FIELDS placeholders. Without these, the cache
            # field-completeness gate at ~line 612 drops every fallback
            # entry on reload → next session re-fetches the same broken
            # blob, hits the same hiccup, falls back again, drops again
            # → infinite re-probe loop disguised as cache-miss. None is
            # acceptable because the gate uses `f in meta` (presence
            # check), not `is not None`. content_type is the value
            # passed to this function so cache-replay routing matches
            # the original probe's content-type dispatch.
            "res_norm": None,
            "blockiness": None,
            "fft_hf_ratio": None,
            "tenengrad_norm": None,
            "tenengrad_clean": None,
            "content_type": content_type,
        }

    # v5.1: stash chroma_var so _classify_series_content can read it from
    # the aggregated metadata without re-decoding pages.
    metadata["chroma_var"] = round(chroma_var, 4)

    # v5.1 Phase 1: watermark detection. Runs after T1 components but
    # before the T2 blend so the penalty applies to the T1 base. The
    # detector returns watermark_score=None when easyocr isn't available
    # or weights are still downloading — we skip the penalty in that case
    # (graceful degrade). A real 0.0 score means "checked, no watermark";
    # the distinction matters for cache audit.
    #
    # v7 (2026-05-18): skipped entirely when is_cover_probe=True. Covers
    # are designed with title/logo text in the exact regions we crop, so
    # detecting "watermarks" there is structurally wrong. The placeholders
    # below (watermark_score=None, watermark_regions=[]) match the
    # easyocr-unavailable degrade path so downstream consumers behave
    # identically.
    if not is_cover_probe:
        wm_metadata = _detect_watermarks(img, content_type=content_type)
        metadata.update(wm_metadata)
        wm_score = wm_metadata.get("watermark_score")
        if isinstance(wm_score, (int, float)) and wm_score > 0.0:
            t1 = max(0.0, t1 - float(wm_score))
            metadata["t1_score"] = round(t1, 4)  # update reported t1 post-penalty
            # Flag as outlier when multiple regions trigger; UI surfaces this
            # via the existing AlertTriangle pattern (Phase 7 mapping).
            if len(wm_metadata.get("watermark_regions") or []) >= 2:
                metadata["outlier"] = "heavy_watermark"

    # T2 (deep-model NR-IQA) blends in when pyiqa is available AND the
    # weight-prefetch daemon (warmup_t2_models) has confirmed weights are
    # cached. Both prerequisites mean the typical first-search case runs
    # T1-only while the warmup downloads, then subsequent searches in the
    # same process get full T1+T2 once the daemon completes. The
    # comparator does NOT distinguish T1-only from T1+T2 scores — they
    # share the same scalar contract — so this is a true graceful degrade.
    t2_composite, t2_metadata = _compute_t2_score(
        blob, img, is_grayscale, content_type=content_type,
    )
    metadata.update(t2_metadata)
    if t2_composite is not None:
        # Renormalized composite: 0.90 * T1 + 0.10 * T2. The 0.10 weight is
        # deliberately small because T2 barely discriminates within the
        # operational aggregator quality range (q>=70) per the calibration
        # in _compute_t2_score docstring. T2's value is catching weird
        # edge cases (non-JPEG distortions, scanner noise, color casts)
        # that T1's per-component formula doesn't model. Weights chosen to
        # sum to 1.0 so the composite stays in [0, 1] without clipping.
        composite = 0.90 * t1 + 0.10 * t2_composite
        composite = float(max(0.0, min(1.0, composite)))
        metadata["composite_pre_t2"] = round(t1, 4)
        t1 = composite
    # T2 (NIQE / ARNIQA / CLIP-IQA) field placeholders. _compute_t2_score
    # only populates `t2_available` when T2 is disabled; the other slots
    # must default to None so the cache schema and UI tooltip see explicit
    # nulls instead of missing keys. The contract: every metadata field
    # is present even when not measured.
    metadata.setdefault("t2_score", None)
    # v6: clip_iqa_score is the new scalar (sigmoid of antonym-pair cos
    # similarity from torchmetrics CLIP-IQA+). The legacy v5 keys
    # clip_iqa_scores / clip_iqa_mean stay for cache back-compat but are
    # always None (the legacy pyiqa-multi-prompt path was never reached
    # due to the Py3.13 / pkg_resources bug).
    metadata.setdefault("clip_iqa_score", None)
    metadata.setdefault("clip_iqa_scores", None)
    metadata.setdefault("clip_iqa_mean", None)
    metadata.setdefault("niqe_score", None)
    metadata.setdefault("niqe_norm", None)
    metadata.setdefault("arniqa_score", None)
    # v5.1 Phase 1: watermark detection field placeholders. _detect_watermarks
    # already populates these in the common path; setdefault covers the
    # T1-fallback path (the bpp/heuristic recovery when _compute_t1_score
    # itself raises) which doesn't go through _detect_watermarks.
    metadata.setdefault("watermark_score", None)
    metadata.setdefault("watermark_regions", [])
    metadata.setdefault("watermark_detector_used", None)
    # v5.1 Phase 5: chroma subsampling field placeholders for the
    # T1-fallback path (color/AVIF/JXL/non-JPEG won't have these populated
    # by _compute_t1_score either, so default to None).
    metadata.setdefault("chroma_penalty", 0.0)
    metadata.setdefault("chroma_subsampling", None)
    metadata.setdefault("chroma_complexity", None)
    # T3 (paired DISTS) is populated post-probe by the orchestrator's
    # `_run_paired_comparison` — placeholders here so the schema is
    # complete even before T3 runs.
    metadata.setdefault("paired_quality_adjustment", None)
    metadata.setdefault("paired_anchor_site", None)
    metadata.setdefault("paired_dists_median", None)
    metadata.setdefault("paired_pairs_compared", 0)

    return t1, metadata


# --- Title normalization + canonical key -------------------------------
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)
_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")


def _normalize_title(title: str) -> str:
    """Canonical key for cross-site dedupe.

    Aggressive: lowercase, drop parentheticals (often '(Doujinshi)' or season
    markers), strip non-alphanumerics, collapse to a token-bag string. False
    merges of distinct series are caught at the candidate level by chapter_count
    deltas in later phases (plan section L); for Phase 1a we accept some
    over-merging since the user picks the source URL anyway.
    """
    s = (title or "").strip().lower()
    s = _PARENS_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = " ".join(s.split())
    return s


# --- Title matching -----------------------------------------------------
def _best_title_match(query: str, hit: SearchHit) -> float:
    """Compute rapidfuzz weighted similarity against title + alt_titles.

    Uses WRatio (weighted blend of partial_ratio, token_sort_ratio, and ratio)
    because token_set_ratio alone gives a perfect score to any candidate that
    contains every query token — which means 'Frieren' matches a 60-char doujin
    title equally with the actual main series. WRatio penalizes extra cruft
    so 'Sousou no Frieren' scores 90 vs a doujin with parenthetical markers
    at 60. Returns max similarity across all candidate strings, normalized
    0..1 (rapidfuzz emits 0..100).

    Cross-file: this is the only place query↔hit similarity is computed; if
    we ever migrate scorers, change here and update DEFAULT_MIN_MATCH above
    in the same edit.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError as exc:
        raise RuntimeError(
            "rapidfuzz is required for cross-site search. "
            "Install with: pip install rapidfuzz"
        ) from exc

    candidates = [hit.title]
    candidates.extend(hit.alt_titles or [])
    if not candidates:
        return 0.0

    q = (query or "").strip()
    if not q:
        return 0.0

    best = 0.0
    for cand in candidates:
        if not cand:
            continue
        score = fuzz.WRatio(q, cand) / 100.0
        if score > best:
            best = score
    return best


# --- Public entrypoint --------------------------------------------------
def search_all(
    query: str,
    scraper_factory: Callable[[BaseSiteHandler], object],
    make_request: Callable,
    *,
    language: str = "en",
    parallelism: int = 6,
    per_site_timeout_s: float = DEFAULT_PER_SITE_TIMEOUT_S,
    min_match: float = DEFAULT_MIN_MATCH,
    top_per_site: int = 20,
    probe_failure_cache: Optional[ProbeFailureCache] = None,
    img_quality_cache: Optional[ImageQualityCache] = None,
    seed_hits: Optional[List[SearchHit]] = None,
    skip_probe_sites: Optional[Set[str]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    seeded_only: bool = False,
) -> List[SeriesCandidate]:
    """Fan out search across all search-capable handlers, dedupe, rank.

    Args:
      query: user-provided title text.
      scraper_factory: called once per handler — returns a configured scraper
        for that handler. Lets each handler set its own headers/cookies via
        configure_session without colliding on a shared session. Implemented
        by aio_search_cli.run_search_mode.
      make_request: aio-dl.py's make_request — passing this in (rather than
        importing) keeps the orchestrator a pure module.
      probe_failure_cache: shared cache of host→suppression. Pass None for
        ephemeral runs (used only by tests).
      seed_hits: optional list of SearchHits to inject before the parallel
        search runs. Used for URL-mode --search where the user supplied a
        specific MangaFire URL; the seed hit guarantees that source's
        participation even when its keyword search would have failed (e.g.,
        MangaFire's flaky typeahead). Seeded hits go through the same
        rapidfuzz scoring + union-find merge as parallel-search results, so
        they end up in the right candidate cluster.
      skip_probe_sites: optional set of site names whose candidates should
        be excluded from the image-quality probe loop. Reserved for the
        direct-URL --multi-source path (aio_search_cli.find_alternatives_
        for_direct_url, called from aio-dl.py main() at the multi-source
        alternatives-lookup site) where we're going to download from the
        URL's primary host regardless of what the probe says, so scoring
        it is pure waste. Search mode (--search) intentionally passes
        None here so the seed source IS probed — the JSON output is
        informational and the user expects a comparable score for every
        candidate including any URL they handed in. Skipped sources stay
        out of both the persistent-cache read and the probe-worker
        enqueue: img_quality_score stays None and the comparator falls
        back to seed_quality. Skipping is per-site (not per-URL) because
        once we've committed to a host we don't care about scoring any
        OTHER candidates from that host either — they wouldn't be used
        for the download.
      on_status: optional progress callback for human output (e.g., the
        "[*] Probing image quality across N sources..." line in Phase 2).

    Returns:
      List of SeriesCandidate, ranked best-first.
    """
    from . import iter_search_capable_handlers, get_handler_by_name

    cache = probe_failure_cache  # may be None
    img_cache = img_quality_cache  # may be None — probe phase skipped if None
    seed = _load_quality_seed()

    # Kick off T2 weight prefetch in a background daemon thread as soon as
    # we know a search is happening. NIQE's weights are 8KB so first-run
    # download is near-instant; ARNIQA (opt-in via AIO_T2_ARNIQA=1) is
    # 107MB. By starting the warmup here — before the parallel search
    # fan-out runs — workers don't serialize behind the download. The
    # _T2_READY event guards `_compute_t2_score`, so the very first
    # search (or one that started before warmup finishes) cleanly falls
    # back to T1-only scoring. Subsequent searches in the same process
    # always get full T1+T2.
    warmup_t2_models(background=True)

    handlers = [h for h in iter_search_capable_handlers()]
    if not handlers and not seed_hits:
        return []

    # Lowercase names of handlers flagged as publisher-owned platforms (vs
    # aggregators). Read off the class attribute once here so the per-source
    # loop below can populate SourceEntry.is_official with a cheap set lookup
    # instead of re-traversing the handler registry per candidate. Empty set
    # is the safe degenerate — falls back to the pre-fix behavior of quality-
    # only tiebreaking. See BaseSiteHandler.OFFICIAL_PUBLISHER for the flag.
    official_sites: set = {
        h.name.lower() for h in handlers
        if getattr(h, "OFFICIAL_PUBLISHER", False)
    }

    # Names of sites already represented by a seed hit (URL-mode --search).
    # Defined here (before the eligibility loop uses it) so seed_hits can
    # short-circuit re-querying those handlers.
    seeded_sites: set = {h.site for h in (seed_hits or [])}

    # Filter out handlers whose primary domain is currently blocked OR whose
    # site is already represented by a URL-mode seed hit. When a seed hit is
    # present we already have authoritative data for that site — no need to
    # also run the parallel search and risk its flakiness (this is the whole
    # reason URL-mode exists for MangaFire).
    #
    # When seeded_only is set (--seeded-only flag), drop any handler whose
    # name doesn't appear in quality_seed.json. This skips the long tail of
    # Madara/MangaThemesia extras that default to seed=0.50 — most are
    # foreign-language and contribute mostly noise to rankings. Cuts search
    # wall time roughly in half on popular queries since ~250 handlers stop
    # contributing parallel I/O. Cross-file: same name lookup as the
    # comparator's seed_quality assignment (`seed.get(site.lower(), 0.5)`),
    # so the filter is consistent with how scoring would have treated them.
    eligible: List[BaseSiteHandler] = []
    skipped_unseeded = 0
    for h in handlers:
        host = (h.domains[0] if getattr(h, "domains", None) else "") or ""
        if h.name in seeded_sites:
            # Skip silently — mangafire (or whichever site the URL maps to)
            # is already represented by a seed_hit that goes into
            # candidates. The seed_hit gets probed for image quality just
            # like any other source (the probe loop keys on src.site, not
            # on whether the source came from seed_hits vs. parallel
            # search), so this 'continue' only skips the redundant
            # typeahead re-query. Logging "skipping mangafire" used to
            # appear here, but it was misleading: users read it as
            # "mangafire is excluded from the run," when in fact mangafire
            # IS in the candidate list, IS probed, IS ranked, and IS the
            # primary download target. Removed 2026-05-27 in the cleanup
            # round after reverting the search-mode probe-skip experiment.
            continue
        if cache and host and cache.is_blocked(host):
            if on_status:
                on_status(f"  skipping {h.name} ({host} suppressed)")
            continue
        if seeded_only and h.name.lower() not in seed:
            skipped_unseeded += 1
            continue
        eligible.append(h)
    if seeded_only and skipped_unseeded and on_status:
        on_status(
            f"  --seeded-only: skipped {skipped_unseeded} handler(s) not in "
            f"quality_seed.json"
        )

    # Seed hits enter the scoring pipeline before the parallel search results.
    # If a seed hit's site is also returned by the parallel search, dedupe-
    # within-candidate (per_site_best) keeps whichever scored higher on
    # title-match — but seed hits typically already score 1.0 (title was
    # extracted from the URL's series page itself), so they normally win.
    #
    # Defined BEFORE the early-return below so the `not all_hits` check has
    # something to evaluate. Earlier revisions defined this after the early
    # return, which raised NameError when `eligible` was empty (all sites
    # blocked / --seeded-only filtered everything out / etc.).
    all_hits: List[SearchHit] = list(seed_hits) if seed_hits else []

    if not eligible and not all_hits:
        # No live handlers AND no seed hits — nothing to score.
        return []

    if on_status:
        if eligible:
            on_status(f"[*] Searching {len(eligible)} sites for '{query}'...")
        if seed_hits:
            on_status(f"[*] Including {len(seed_hits)} seeded source(s) from URL")

    def _run_one(handler: BaseSiteHandler) -> List[SearchHit]:
        host = (handler.domains[0] if getattr(handler, "domains", None) else "") or ""
        try:
            scraper = scraper_factory(handler)
        except Exception:
            if cache and host:
                cache.record_failure(host)
            return []
        try:
            hits = handler.search(
                query, scraper, make_request, language=language, limit=top_per_site
            )
            if not isinstance(hits, list):
                hits = []
            if cache and host:
                # Empty results don't count as failures (the site is up but
                # has no match for this query); only exceptions do.
                cache.record_success(host)
            return hits
        except Exception:
            if cache and host:
                cache.record_failure(host)
            return []

    workers = max(1, min(parallelism, len(eligible)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search") as pool:
        futures = {pool.submit(_run_one, h): h for h in eligible}
        for fut in as_completed(futures, timeout=None):
            try:
                hits = fut.result(timeout=per_site_timeout_s)
            except Exception:
                hits = []
            if hits:
                all_hits.extend(hits)

    if not all_hits:
        return []

    # Score every hit against the query.
    scored: List[Tuple[float, SearchHit]] = []
    for hit in all_hits:
        score = _best_title_match(query, hit)
        if score >= min_match:
            scored.append((score, hit))

    if not scored:
        return []

    # Group by canonical title key. Within a candidate, multiple sources may
    # appear; we rank them per-candidate and emit one candidate per group.
    #
    # Merge strategy: union-find over normalized title-keys.
    # Each hit contributes its primary key + every alt-title key as members of
    # one set. Any two hits whose keysets overlap end up in the same group.
    # This is symmetric — site A returning "Frieren ..." with "Sousou no
    # Frieren" as alt and site B returning "Sousou no Frieren" with no useful
    # alts will still merge, regardless of which arrived first. The earlier
    # one-directional approach (only fold new hit's alts into existing groups)
    # missed that case.
    parent: Dict[str, str] = {}

    def _find(k: str) -> str:
        path: List[str] = []
        while parent.get(k) and parent[k] != k:
            path.append(k)
            k = parent[k]
        for p in path:
            parent[p] = k
        parent.setdefault(k, k)
        return k

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    # Pass 1: build keysets per hit; union all keys within a hit; track which
    # hits ended up in which root.
    hit_keys: List[Tuple[float, SearchHit, List[str]]] = []
    for score, hit in scored:
        keys: List[str] = []
        primary = _normalize_title(hit.title)
        if primary:
            keys.append(primary)
        for alt in hit.alt_titles or []:
            ak = _normalize_title(alt)
            if ak and ak not in keys:
                keys.append(ak)
        if not keys:
            continue
        for k in keys:
            _find(k)  # ensure node exists
        for k in keys[1:]:
            _union(keys[0], k)
        hit_keys.append((score, hit, keys))

    # Pass 2: bucket by root.
    groups: Dict[str, List[Tuple[float, SearchHit]]] = {}
    for score, hit, keys in hit_keys:
        root = _find(keys[0])
        groups.setdefault(root, []).append((score, hit))

    candidates: List[SeriesCandidate] = []
    for key, members in groups.items():
        # canonical_title: most-common title across the bucket; shortest as
        # tiebreaker. Length-as-primary biases toward edition variants like
        # "One Piece - Digital Colored Comics" over the canonical "One Piece"
        # when 3 of 4 sources agree on the short name.
        from collections import Counter
        title_counts = Counter((h.title or "") for _, h in members if (h.title or ""))
        if title_counts:
            max_freq = max(title_counts.values())
            top_titles = [t for t, c in title_counts.items() if c == max_freq]
            canonical_title = min(top_titles, key=len)
        else:
            canonical_title = ""
        # Year: take the max non-None year (most recent edition).
        year = max(
            (h.year for _, h in members if isinstance(h.year, int)),
            default=None,
        )
        sources: List[SourceEntry] = []
        # Dedupe within the same site — keep the highest title-match per site.
        per_site_best: Dict[str, Tuple[float, SearchHit]] = {}
        for score, hit in members:
            cur = per_site_best.get(hit.site)
            if cur is None or score > cur[0]:
                per_site_best[hit.site] = (score, hit)
        for site, (score, hit) in per_site_best.items():
            seed_q = seed.get(site.lower(), 0.5)
            # is_official is the AND of site-level handler.OFFICIAL_PUBLISHER
            # (set by class attr) AND per-hit SearchHit.is_official (set by
            # the handler's search() method when a single handler legitimately
            # serves both official + non-official content — e.g. linewebtoon
            # for Originals (official) vs Canvas (user-uploaded, not
            # publisher-curated). hit.is_official=None means the handler
            # didn't differentiate per-hit, so site-level is the only signal.
            site_level = site.lower() in official_sites
            per_hit = hit.is_official
            if per_hit is None:
                src_is_official = site_level
            else:
                # AND ensures a rogue handler can't claim is_official without
                # opting in via the class attribute.
                src_is_official = bool(per_hit) and site_level
            sources.append(
                SourceEntry(
                    site=site,
                    url=hit.url,
                    title=hit.title,
                    cover=hit.cover,
                    title_match=score,
                    seed_quality=seed_q,
                    composite_score=score,  # Phase 1a: composite = title_match
                    chapter_count_hint=hit.chapter_count_hint,
                    actual_chapter_count=hit.actual_chapter_count,
                    dmca_likely=hit.dmca_likely,
                    raw_score=hit.raw_score,
                    is_official=src_is_official,
                )
            )

        # Cross-site chapter-count divergence detection. Two distinct
        # failure modes share the same "actual << peers' max" signal:
        #
        # (a) DMCA-affected source: source's OWN chapter_count_hint claims
        #     ~as many chapters as peers (so its metadata is intact) but
        #     actual_chapter_count is far below — the hosted chapters were
        #     hollowed by takedown. Originally caught:
        #       - Witch Hat Atelier (MangaDex hint=96, actual=1, MF=96)
        #       - One Piece (MangaDex hint=1181, actual=7, MF=1181)
        #       - Eleceed (MangaDex hint=400+, actual=102 at 25.5%)
        #     Flagged as dmca_likely → surfaced in to_json/UI.
        #
        # (b) Wrong-match / count outlier: source's OWN hint is missing or
        #     also low — the source genuinely has those few chapters; it
        #     just isn't the same series despite the union-find merge
        #     (typically caused by short generic normalized titles like
        #     "one piece" colliding with Canvas user uploads). Flagged as
        #     count_outlier → ranking penalty WITHOUT the DMCA claim in
        #     to_json (avoids users misreading a wrong-match for a
        #     takedown).
        #
        # Both end up sinking to the back of the source list via the _cmp
        # first check; the differentiator is purely semantic (which flag
        # is surfaced in the JSON output).
        #
        # Threshold: actual < 50% of max peer hint. Tuned for Eleceed
        # which slipped through at 25.5% under a 25% cap; 50% catches
        # substantially-incomplete without false-positiving series where
        # one source legitimately lags by a chapter or two.
        max_other_count = 0
        for s in sources:
            if isinstance(s.chapter_count_hint, int) and s.chapter_count_hint > max_other_count:
                max_other_count = s.chapter_count_hint
        if max_other_count >= 10:
            for s in sources:
                if s.dmca_likely or s.count_outlier:
                    continue
                actual = s.actual_chapter_count
                if not isinstance(actual, int) or actual >= max_other_count * 0.5:
                    continue
                own_hint = s.chapter_count_hint
                if isinstance(own_hint, int) and own_hint >= max_other_count * 0.5:
                    # Pattern (a): source claimed it has many, delivered few.
                    s.dmca_likely = True
                else:
                    # Pattern (b): source either claims few or didn't expose
                    # a hint at all — the union-find merge with the high-count
                    # peer is almost certainly a false-positive driven by a
                    # title-string collision (e.g. Canvas series titled
                    # "One Piece" with 20 real episodes union-find-merged
                    # with the actual 1100-chapter One Piece).
                    s.count_outlier = True
        # Sort sources within candidate. Order of decision:
        #   1. DMCA-likely sources go to the back. A source with 1/96 chapters
        #      accessible should never beat one with 96/96 regardless of
        #      quality. Fixes the canonical Witch Hat Atelier case where
        #      MangaDex's higher seed_quality (0.92) was beating MangaFire
        #      (0.85) even though MangaDex was DMCA-hollowed.
        #   2. Official publisher wins. When one source is the publisher's own
        #      platform (e.g. linewebtoon = webtoons.com, the literal LINE
        #      publisher) and the other is an aggregator re-hosting that
        #      same content (toonily, asura, etc.), the publisher wins
        #      regardless of title_match spread or measured image quality.
        #      Both sources have already been merged by union-find as the
        #      same series, so we trust the canonical bytes from the
        #      publisher. Fixes the webtoons.com vs toonily case where
        #      vertical-scroll PNG at 720-800px scored below toonily's
        #      upscaled JPEG on the probe's res_score formula (which
        #      treats 800-2400px as the credit band) despite the PNG
        #      being lossless and the JPEG being generation-loss.
        #   3. Title match within TIEBREAKER_WINDOW (0.10) → image-quality
        #      decides. Use img_quality_score when measured (Phase 2 cover
        #      probe); fall back to seed_quality when probe failed or hasn't
        #      run yet. Per the plan, ANY measurement replaces the seed —
        #      cover-bytes are deterministic, not noisy estimates.
        #   4. Otherwise title_match wins.
        # Pairwise comparator gives deterministic behavior at window boundaries.
        def _quality_for(s: SourceEntry) -> float:
            # `is not None` (not `> 0`): a measured 0.0 is meaningful — it's
            # the aggregate probe's "5/5 fetches failed" signal. We want it
            # used as the rank input even though it'll lose to anything seed-
            # based, because a broken CDN should rank LAST among its peers.
            return s.img_quality_score if s.img_quality_score is not None else s.seed_quality

        def _cmp(a: SourceEntry, b: SourceEntry) -> int:
            # Sink signal: dmca_likely OR count_outlier. Both push the source
            # to the back of the candidate's source list — they are the same
            # signal at the ranking layer, surfaced differently in to_json
            # (only dmca_likely; count_outlier is intentionally internal).
            # See the cross-site count check above for how each is set.
            a_sink = a.dmca_likely or a.count_outlier
            b_sink = b.dmca_likely or b.count_outlier
            if a_sink != b_sink:
                return 1 if a_sink else -1
            # Official-publisher tiebreaker — gated behind a title_match
            # floor + within-window check so a weak-match official hit can't
            # outrank a strong-match aggregator. See
            # IS_OFFICIAL_REQUIRES_TITLE_MATCH for the rationale; the gate
            # exists alongside per-hit is_official (which already filters
            # canvas) as a generic backstop for any future handler that
            # might surface low-confidence official hits.
            if a.is_official != b.is_official:
                strong_a = a.title_match >= IS_OFFICIAL_REQUIRES_TITLE_MATCH
                strong_b = b.title_match >= IS_OFFICIAL_REQUIRES_TITLE_MATCH
                within = abs(a.title_match - b.title_match) <= TIEBREAKER_WINDOW
                if strong_a and strong_b and within:
                    return -1 if a.is_official else 1
                # else fall through — title_match decides below.
            if abs(a.title_match - b.title_match) <= TIEBREAKER_WINDOW:
                qa, qb = _quality_for(a), _quality_for(b)
                if qa != qb:
                    return -1 if qa > qb else 1
                return 0
            return -1 if a.title_match > b.title_match else 1

        # Capture the comparator for re-use after the probe phase populates
        # img_quality_score; we sort once now (with seed fallback) so output
        # is deterministic even if the probe phase fails entirely.
        sources.sort(key=cmp_to_key(_cmp))
        candidates.append(
            SeriesCandidate(
                canonical_title=canonical_title or key,
                canonical_year=year,
                sources=sources,
            )
        )

    # ---- Image-quality probe phase (Phase 2) ----
    # For each unique (site, url, hit-with-cover) across all candidates'
    # sources, probe a representative image and score it. Cached results
    # serve subsequent searches for the same (site, series). Replaces the
    # seed_quality prior in the comparator below — the seed continues to
    # serve unfamiliar pairs and probe-failures.
    if img_cache is not None and candidates:
        # 2026-05-20: Pre-rank candidates BEFORE the probe phase so we know
        # which one is the top title-match. Only the top candidate's sources
        # get the full breadth-sampling chapter probe (8 chapters); every
        # other candidate's sources clamp to max_samples=1 (single middle-
        # chapter image). Rationale: sub-series like "Attack on Titan: Lost
        # Girls" / "Anthology" surface as separate candidates with weaker
        # title_match; users almost never want those over the main series,
        # so spending 7 extra chapter-probe HTTP calls per sub-series source
        # is wasted bandwidth — especially painful on MangaFire (Playwright
        # VRF per chapter, 3-5 s each). One-image probes still produce a
        # usable img_quality_score for ranking within the sub-series.
        # Same final ranking applies (line 5403); this just lightens the
        # probe cost for non-top candidates. Identical to the existing
        # EXPENSIVE_PROBE_QUICK_THRESHOLD clamp for low-title-match results
        # on expensive-probe handlers, but generalized to all handlers.
        candidates.sort(
            key=lambda c: max((s.title_match for s in c.sources), default=0.0),
            reverse=True,
        )
        top_candidate_urls: set = (
            {s.url for s in candidates[0].sources} if candidates else set()
        )

        # Index hit-by-url for quick lookup during probe (so we can pass the
        # full SearchHit to handler.probe_sample_image, not just the source).
        hit_by_url: Dict[str, SearchHit] = {h.url: h for _, h in scored}
        # Collect unique (site, url) source entries needing probe.
        # Same-(site,url) appearing in multiple candidates can share the same
        # cached result — but in practice each (site, url) lives in one
        # candidate after the union-find merge, so this is mostly redundant.
        seen_pairs: set = set()
        sources_to_probe: List[SourceEntry] = []
        for c in candidates:
            for src in c.sources:
                key = (src.site, src.url)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                sources_to_probe.append(src)

        # Cache hits short-circuit; only un-cached sources need a fetch.
        # Use get_with_metadata so cached entries restore img_quality_metadata
        # too — without this the UI tooltip loses bpp/is_grayscale/outlier on
        # every repeat search (cache hit path was returning score-only).
        cache_misses: List[SourceEntry] = []
        for src in sources_to_probe:
            # SKIP_QUALITY_PROBE handlers (currently only comix — see
            # sites/comix.py for the rationale) opt out of probing
            # entirely: their per-source probe cost is dominated by a
            # single-threaded browser bridge that would trip the 240 s
            # probe-phase deadline before any candidate completed. The
            # comparator `_quality_for` (~line 5329) falls back to
            # seed_quality when img_quality_score is None, so leaving
            # the score un-set IS how the calibrated seed becomes the
            # effective ranking signal. We also skip the persistent-
            # cache read so stale per-URL scores from earlier buggy
            # probes (which scored 0.0 because synthetic
            # `comix-page://` URLs aren't HTTP-fetchable) don't leak
            # back into ranking.
            handler = get_handler_by_name(src.site)
            if handler is not None and getattr(
                handler, "SKIP_QUALITY_PROBE", False,
            ):
                continue
            if skip_probe_sites and src.site in skip_probe_sites:
                # Caller marked this site as already-committed: we're
                # going to download from this URL regardless of any
                # quality score, so the score has no operational effect
                # on the download decision. Skip both the persistent-
                # cache read AND a fresh probe — feeding a cached score
                # in here would push the seed source through the same
                # sort as alternatives (e.g. a different site that
                # happens to have a higher cached score outranks the
                # seed and steals the primary slot in the candidate's
                # sources list, contradicting "this is the URL the
                # user picked"). Leave img_quality_score=None so the
                # comparator's `_quality_for` (~line 5329) falls back
                # to seed_quality — that's the per-site prior the user
                # implicitly trusts when they hand us a URL from that
                # site. Cross-file: callers populate this set from
                # aio_search_cli.run_search_mode (seed_hits[*].site)
                # and aio_search_cli.find_alternatives_for_direct_url
                # (primary_handler.name).
                continue
            cached = img_cache.get_with_metadata(src.site, src.url)
            if cached is not None:
                score, metadata = cached
                src.img_quality_score = score
                src.img_quality_metadata = metadata or None
            else:
                cache_misses.append(src)

        if cache_misses:
            if on_status:
                on_status(f"[*] Probing image quality across {len(cache_misses)} sources...")

            def _probe_one(src: SourceEntry) -> None:
                hit = hit_by_url.get(src.url)
                if hit is None:
                    return
                handler = get_handler_by_name(src.site)
                if handler is None:
                    return
                try:
                    scraper = scraper_factory(handler)
                except Exception:
                    return
                # Tiered probe selection. High-seed sites (those listed in
                # quality_seed.json) get the breadth-sampling aggregate probe
                # (v5: 1 page from each of 8 chapters spread across the
                # series + a throttle-probe tail of 3 sequential pages from
                # the highest-scoring chapter — catches CDN throttling AND
                # pixel quality AND per-chapter quality variance). Low-seed
                # sites stay on the cheap cover probe. See
                # CHAPTER_PROBE_MIN_SEED for the threshold rationale and
                # sites/base.py:_probe_chapter_aggregate for v5 details.
                if src.seed_quality >= CHAPTER_PROBE_MIN_SEED:
                    # 2026-05-20: clamp probe depth based on candidate rank.
                    # Only the top title-match candidate's sources run the
                    # full 8-chapter breadth probe; everyone else clamps to
                    # max_samples=1 (single mid-chapter image). Sub-series
                    # results ("Attack on Titan: Lost Girls", "Anthology"
                    # etc.) rarely win user attention vs the main series,
                    # so the extra 7 chapter fetches × N sources per
                    # sub-candidate is wasted bandwidth — especially on
                    # MangaFire (Playwright VRF per chapter ~3-5 s each).
                    # The existing EXPENSIVE_PROBE quick_probe clamp still
                    # fires on top of this for weak-title-match results
                    # within a candidate (mostly redundant now that
                    # non-top-candidate sources already clamp).
                    is_top_candidate = src.url in top_candidate_urls
                    quick_probe = (
                        getattr(handler, "EXPENSIVE_PROBE", False)
                        and src.title_match < EXPENSIVE_PROBE_QUICK_THRESHOLD
                    )
                    if not is_top_candidate:
                        max_samples = 1
                    elif quick_probe:
                        max_samples = 2
                    else:
                        max_samples = None  # full 8-chapter breadth
                    clamped = (max_samples is not None)
                    try:
                        aggregate = handler._probe_chapter_aggregate(
                            hit, scraper, make_request,
                            max_samples=max_samples,
                        )
                    except Exception:
                        aggregate = None
                    if aggregate is not None:
                        score, metadata = aggregate
                        src.img_quality_score = score
                        src.img_quality_metadata = metadata
                        # Skip caching clamped results: the next search may
                        # rank this source higher (different query → it
                        # could become the top candidate and want the full
                        # breadth probe). A cached 1-sample result would
                        # otherwise serve from cache and lock the source
                        # into the noisier score. Clamped probes are cheap
                        # enough to redo on demand. Full probes (top
                        # candidate, unclamped) DO cache.
                        if not clamped:
                            img_cache.set(src.site, src.url, score, metadata=metadata)
                        return
                    # Aggregate failed (no chapters / total CDN failure / etc.)
                    # — fall through to cover probe so we still get *some*
                    # signal for ranking.
                try:
                    blob = handler._probe_cover_image(hit, scraper, make_request)
                except Exception:
                    return
                if not blob:
                    return
                # v7 (2026-05-18): is_cover_probe=True suppresses watermark
                # detection. Cover artwork legitimately has title/logo text
                # in the corner/edge regions the detector crops; flagging
                # that as "heavy_watermark" is a false positive by design.
                result = _score_image_blob(blob, is_cover_probe=True)
                if result is None:
                    return
                score, metadata = result
                src.img_quality_score = score
                src.img_quality_metadata = metadata
                img_cache.set(src.site, src.url, score, metadata=metadata)

            # Daemon-thread worker pool with deadline. We can't use
            # ThreadPoolExecutor here because its `with` block calls
            # shutdown(wait=True), which waits for ALL submitted tasks —
            # including any hung probe (e.g., a cover URL on a slow CDN, a
            # site whose internals don't honor scraper.get's timeout, or a
            # Playwright VRF call stuck in a bad state). The previous code
            # could hang the orchestrator for minutes on a single bad site.
            #
            # Fix: spawn daemon worker threads pulling from a queue; bound
            # total wait time at PROBE_PHASE_DEADLINE_S. Daemons don't block
            # process exit; whatever didn't finish gets retried (uncached)
            # next search. Per-probe internal timeouts (scraper.get,
            # search_mr's per-attempt timeout) keep hung probes from leaking
            # bandwidth indefinitely; they finish on their own within ~30s
            # and cache results for next run if they manage to complete.
            import queue
            q: "queue.Queue" = queue.Queue()
            for s in cache_misses:
                q.put(s)
            probe_workers = max(1, min(parallelism, len(cache_misses)))

            def _worker_loop() -> None:
                while True:
                    try:
                        s = q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        _probe_one(s)
                    except Exception:
                        pass

            workers: List[threading.Thread] = []
            for _ in range(probe_workers):
                t = threading.Thread(
                    target=_worker_loop,
                    name="img-probe",
                    daemon=True,
                )
                t.start()
                workers.append(t)

            phase_deadline = time.monotonic() + PROBE_PHASE_DEADLINE_S
            for t in workers:
                remaining = phase_deadline - time.monotonic()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
            still_running = sum(1 for t in workers if t.is_alive())
            if still_running and on_status:
                on_status(
                    f"[!] Probe phase: {still_running}/{probe_workers} workers "
                    f"still running at {PROBE_PHASE_DEADLINE_S:.0f}s deadline; "
                    f"abandoning to avoid hang. Their results will populate the "
                    f"cache if the underlying calls return."
                )

        # T3 anchor-free pairwise ranking runs AFTER the worker pool joins
        # so it can compare T1 components against the now-populated
        # img_quality_score / metadata. No piq/cv2 dependency anymore —
        # the new path does scalar component comparisons rather than
        # DISTS+ST-LPIPS inference. Updates
        # candidate.sources[].img_quality_score in-place with
        # pairwise_adjustment, then re-sorts below. v5.1's anchor-based
        # path (_run_paired_comparison) remains in this module for
        # back-compat with tests/test_paired_comparison.py but is no
        # longer wired into search_all.
        if candidates:
            if on_status:
                on_status(
                    f"[*] T3 pairwise ranking across multi-source "
                    f"candidates (budget {PAIRWISE_BUDGET_S:.0f}s)..."
                )
            try:
                _run_pairwise_ranking(
                    candidates, scraper_factory, make_request, on_status,
                    probe_failure_cache=probe_failure_cache,
                )
            except Exception as _e:
                if on_status:
                    on_status(f"[!] T3 pairwise ranking failed: {_e}; T3 adjustments skipped")

        # Re-sort each candidate's sources now that img_quality_score is
        # populated. The comparator uses _quality_for which prefers measured
        # over seed when img_quality_score > 0. T3 adjustments (applied
        # in-place above) feed into this sort too — if a paired-comparison
        # boost pushed a source above its peers, it ranks higher here.
        for c in candidates:
            c.sources.sort(key=cmp_to_key(_cmp))

    # Rank candidates: best title_match across any of their sources.
    candidates.sort(
        key=lambda c: max((s.title_match for s in c.sources), default=0.0),
        reverse=True,
    )
    return candidates


__all__ = [
    "SearchHit",
    "SourceEntry",
    "SeriesCandidate",
    "ProbeFailureCache",
    "ImageQualityCache",
    "search_all",
    "DEFAULT_MIN_MATCH",
    "TIEBREAKER_WINDOW",
    "DEFAULT_PER_SITE_TIMEOUT_S",
    "IMG_QUALITY_TTL_S",
]
