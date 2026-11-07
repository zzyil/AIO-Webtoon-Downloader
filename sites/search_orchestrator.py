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
  - Image-quality rating is the matched-page probe in sites/match_session.py
    (pixel math in sites/page_ranker.py, numpy + Pillow only, no ML model).
    This module schedules it (grep _run_match_phase) and caches its records
    (ImageQualityCache).
  - The probe-failure cache hooks into make_request's _record_rate_limit so a
    timed-out host suppresses itself for 1h via the existing cooldown machinery
    (aio-dl.py:589, aio-dl.py:628). No parallel cooldown system.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import fuzzy_match
from .base import BaseSiteHandler, SearchHit, SiteBusyError


# --- Tunables -----------------------------------------------------------
# Title-match floor: hits below this rapidfuzz similarity are discarded.
# 0.55 keeps "Frieren" matching "Sousou no Frieren" via alt-titles but kicks
# out unrelated series that share a single word.
DEFAULT_MIN_MATCH = 0.55

# Seed alt-title matching (2026-08-20). In URL-seed mode aio_search_cli sets
# `query = seed.title`, so the whole cross-site search runs against ONE site's
# spelling — usually the English licensed title, while plenty of sites list
# only the romaji. Scoring every hit against the seed's alt_names too closes
# that gap; these two bound the widening. MIN_LEN drops fragments too short to
# discriminate ("K.", a bare volume marker) — WRatio's partial_ratio component
# would match those into anything. MAX caps the M*N score matrix so a series
# with 20 localized aliases stays cheap on Android's pure-Python rapidfuzz
# backend (see sites/fuzzy_match.py for why that backend is in play at all).
_SEED_VARIANT_MIN_LEN = 4
_SEED_VARIANT_MAX = 12

# Seed alt-title QUERIES (2026-11-08). Scoring against the alt names (above)
# only helps when a site's search RETURNED the series; a site whose index only
# knows the other name never does. Measured on the MangaFire "Monster Tale"
# URL (= Bakemonogatari): searched as "Monster Tale", mangahub and mangapill
# returned 20 unrelated hits each and weebcentral none, while all three list
# the series as "Bakemonogatari". So in _run_one a site whose primary search
# found no exact match is searched again under up to _SEED_QUERY_MAX of those
# names. ASCII-only (see _fanout_query_variants): Cyrillic/Arabic/Hangul
# aliases cost a request per site on sites that index English/romaji and
# return nothing. _SEED_QUERY_SATISFIED is "exact title" — WRatio returns 100
# only for identical strings, and a spin-off that merely contains the title
# ("X - Side Story") scores below it, so it doesn't suppress the re-query.
_SEED_QUERY_MAX = 2
_SEED_QUERY_SATISFIED = 1.0

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

# Image-quality cache TTL: the longest a served verdict or a pair edge lives in
# ImageQualityCache (shorter verdict TTLs come from match_session.verdict_ttl).
# Hosts re-encode rarely, so a (site, series) is re-matched about once a month.
IMG_QUALITY_TTL_S = 30 * 24 * 3600

# Only sources whose seed_quality is at or above this take part in the
# matched-page probe (grep _probe_eligible). Sites at >= 0.65 are the ones
# explicitly listed in sites/quality_seed.json — the candidates that actually
# compete for "best source"; the long tail of Madara/MangaThemesia extras
# (default seed 0.50) keeps its seed as its rating, which bounds the probe
# phase. The same threshold powers the --multi-source-quality-min default.
CHAPTER_PROBE_MIN_SEED = 0.65

# Gate for the official-publisher tiebreaker in source_rank_key. is_official wins
# over a non-official peer ONLY when both sources clear this title-match
# floor AND are within TIEBREAKER_WINDOW of each other; otherwise we
# fall through to title_match. Prevents a weak-match official hit (e.g.
# a Canvas series whose normalized title accidentally clustered with a
# real series via union-find) from outranking a strong-match aggregator.
# 0.85 is the line below which title_match is noisy: a clean main-series
# alt-title match lands >= 0.95, doujinshi carrying the literal English
# title in their slug ~0.85-0.95, spinoffs / unrelated series sharing a
# word ~0.55-0.75. Strong-match
# webtoons-vs-toonily case (both score ≈1.0 → within window, both
# strong → official wins) is preserved; weak-match canvas false-merge
# case (linewebtoon 1.0 + canvas 1.0 is still possible if the title
# literally matches, but Fix A + Fix C catch THAT failure path
# upstream — see SourceEntry.is_official assignment + count_outlier).
IS_OFFICIAL_REQUIRES_TITLE_MATCH = 0.85

# Hard cap on the whole matched-page probe phase, passed to the match session
# as its deadline (grep deadline_s=PROBE_PHASE_DEADLINE_S). After it the session
# stops scheduling and search_all moves on: its worker threads may keep running
# but are not waited for, and a source cut by the deadline is stored with the
# shorter truncated-probe TTL (match_session.verdict_ttl) so it is re-matched soon. Bounds
# worst-case latency from a hung handler (a chapter-list or page fetch blocking
# past its request timeout, a Playwright call stuck in a bad state). Each source
# also has its own budget (BaseSiteHandler.PROBE_SOURCE_BUDGET_S), so one slow
# site cannot use up the whole phase.
PROBE_PHASE_DEADLINE_S = 240.0

# --- Fan-out phase tunables (2026-07-12 rewrite) ------------------------
# The handler fan-out used to run on a ThreadPoolExecutor whose `with`-block
# join waited for the SLOWEST handler — the per-future result timeout was
# dead code because as_completed only yields already-finished futures. It now
# runs on daemon worker threads with a SOFT barrier: search_all stops WAITING
# at _FANOUT_DEADLINE_S and moves on, but the straggler threads keep running;
# any that complete before result finalization (after the probe phase) merge
# their hits with title-match + seed/cached quality only (they are never
# probed — user decision 2026-07-12). See search_all's fan-out block + the late-adoption
# block near the end of search_all.
#
# _FANOUT_MAX widens the fan-out beyond --search-parallelism (which also
# governs the probe phase, where every task hammers a single host and
# politeness matters). Fan-out tasks are 1-2 requests to DISTINCT hosts, so
# a wide pool is politeness-safe; 16 puts the ~31 --seeded-only handlers in
# 2 waves instead of 6. Env override for field tuning without a release.
_FANOUT_MAX = 16
try:
    _FANOUT_MAX = max(1, int(os.environ.get("AIO_SEARCH_FANOUT_MAX", "") or _FANOUT_MAX))
except (TypeError, ValueError):
    pass

# Soft barrier on the fan-out phase. NOT a kill switch: worker threads are
# daemons and keep running past it — the deadline only bounds how long the
# pipeline WAITS before proceeding with what's in hand. Env override exists
# for tuning + lets the offline regression test exercise the barrier without
# a real 60s wait (tools/_test_search_perf_opts.py patches the module attr).
_FANOUT_DEADLINE_S = 60.0
try:
    _FANOUT_DEADLINE_S = max(
        1.0, float(os.environ.get("AIO_SEARCH_FANOUT_DEADLINE", "") or _FANOUT_DEADLINE_S)
    )
except (TypeError, ValueError):
    pass

# --- Search-health "slow site" thresholds (2026-07-13) ------------------
# A participating site whose fan-out search or image-quality probe exceeds
# these is flagged "slow" in search_all's `diagnostics["site_health"]` payload,
# which aio_search_cli surfaces in --search-json so the UI can recommend
# disabling chronically slow/down sites (grep _classify_site_health /
# SlowSitesCallout in UI-source). ABSOLUTE, not relative-to-phase: fan-out is
# parallel (one site's share of wall-clock is meaningless) and the probe covers
# only the top 1-2 candidates' cache-miss sources (each probe is ~100% of the
# phase by construction). SLOW_FANOUT_S sits below the single-retry request
# cost (search_timeout×2 → a retry-then-succeed site legitimately takes ~20s)
# yet well above a healthy sub-3s search. SLOW_PROBE_S is DELIBERATELY not
# applied to constitutionally-slow handlers (SEARCH_COST_HINT=="slow" /
# EXPENSIVE_PROBE / OFFICIAL_PUBLISHER) whose deep browser/licensed probe is
# slow by design — see _classify_site_health; without that gate comix's healthy
# ~40-70s browser-driven probe would trip "slow" every run and get perpetually
# recommended for disabling. Env-overridable, same pattern as the deadlines.
SEARCH_SLOW_FANOUT_S = 10.0
try:
    SEARCH_SLOW_FANOUT_S = max(
        0.0, float(os.environ.get("AIO_SEARCH_SLOW_FANOUT_S", "") or SEARCH_SLOW_FANOUT_S)
    )
except (TypeError, ValueError):
    pass

SEARCH_SLOW_PROBE_S = 30.0
try:
    SEARCH_SLOW_PROBE_S = max(
        0.0, float(os.environ.get("AIO_SEARCH_SLOW_PROBE_S", "") or SEARCH_SLOW_PROBE_S)
    )
except (TypeError, ValueError):
    pass

# --- Search-health reachability probe (2026-07-13, fix 3) ---------------
# _classify_site_health below answers "how did this site's SEARCH behave this
# run" (raised? slow? never finished?) — NOT "is the site actually reachable".
# Those diverge in exactly the ways users hit: a site whose SEARCH endpoint
# times out ~40s then raises looks identical to a dead host (both 'down/error')
# even though its DOWNLOAD path is fine, while a site that swallows its own
# all-mirrors-dead failure to [] (grep zeroscans.search) or is merely slow in
# the image probe looks 'slow' though it's unreachable. To split those,
# _emit_site_health runs a bounded liveness GET against each FLAGGED (non-ok)
# eligible site at return time and _refine_with_reachability rewrites the
# verdict: reachable+errored -> a softer 'slow/search_error' (site up, its
# search flaked — no longer the alarming 'down' that auto-recommends disabling
# a download-healthy site), unreachable+anything -> 'down/unreachable' (the one
# true dead bucket). This DELIBERATELY reverses the feature's original "zero
# new network round-trips" constraint (plan
# ~/.claude/plans/certain-sites-that-we-refactored-elephant.md) — that
# constraint is precisely what made the labels mechanism-based-and-wrong.
# BOUNDED so it can't re-inflate the wall time the 2026-07-12 perf rework won
# back: fires ONLY when a site was already flagged (a healthy all-ok search
# probes NOTHING → +0s), skips constitutionally-slow/official/expensive
# handlers (comix — its scraper_factory would launch Patchright), runs parallel
# daemon workers under a hard phase deadline, single-attempts each host with a
# short timeout (NOT make_request's 6× retry chain). Kill-switch
# AIO_SEARCH_REACHABILITY=0 restores the pure-mechanism classification.
_REACHABILITY_ENABLED = (
    os.environ.get("AIO_SEARCH_REACHABILITY", "1").strip().lower()
    not in ("0", "false", "no", "off")
)
SEARCH_REACHABILITY_TIMEOUT_S = 5.0
try:
    SEARCH_REACHABILITY_TIMEOUT_S = max(
        0.5,
        float(os.environ.get("AIO_SEARCH_REACHABILITY_TIMEOUT_S", "") or SEARCH_REACHABILITY_TIMEOUT_S),
    )
except (TypeError, ValueError):
    pass
SEARCH_REACHABILITY_DEADLINE_S = 12.0
try:
    SEARCH_REACHABILITY_DEADLINE_S = max(
        1.0,
        float(os.environ.get("AIO_SEARCH_REACHABILITY_DEADLINE", "") or SEARCH_REACHABILITY_DEADLINE_S),
    )
except (TypeError, ValueError):
    pass
_REACHABILITY_MAX_URLS = 3     # candidate hosts probed per handler (cap the fan)
_REACHABILITY_MAX_SITES = 16   # flagged sites probed per run (whole-connection-down backstop)


# --- Data shapes --------------------------------------------------------
@dataclass
class SourceEntry:
    """One source's data for a particular SeriesCandidate.

    A candidate's `sources` are ordered by source_rank_key (sort_sources);
    composite_score is title_match and is not the sort key.

    `img_quality_score` (set by _apply_ratings from the matched-page probe,
    sites/match_session.py): None = not measured (rank and fallback use
    seed_quality); a rating relative to the best copy of the series (best
    0.95); 0.0 with basis "unreachable" = the source served no pages (sinks, is
    never a fallback). Timeouts never produce 0.0: they leave basis "pending".
    """
    site: str
    url: str
    title: str
    cover: Optional[str]
    title_match: float
    seed_quality: float
    img_quality_score: Optional[float] = None  # populated in Phase 2; None = un-measured
    # match_session.quality_metadata: basis, fetch counts, edition, tie /
    # match group, q and its sharp/res/blk components, sampled chapters - the
    # UI tooltip's "why this score?" data. None = un-measured.
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
    # source_rank_key as the top tiebreaker within a title band, above quality.
    is_official: bool = False
    # Wrong-match sink: True when this source's actual_chapter_count is
    # vastly below peer sources in the same candidate AND the source's
    # own chapter_count_hint doesn't claim a count comparable to peers
    # (i.e. it's not a DMCA-affected source claiming chapters it can't
    # deliver — it's a count-outlier whose union-find-merge with peers is
    # almost certainly a false-positive driven by title-string collision).
    # Treated by source_rank_key identically to dmca_likely (sink to back of the
    # candidate's source list) but DELIBERATELY not surfaced in to_json /
    # the UI — the UI's DMCA flag should only fire for actual DMCA-affected
    # sources to avoid users misreading wrong-match as a takedown. Set by
    # the same cross-site check at search_orchestrator.py:~5230 that
    # populates dmca_likely; the differentiator is whether own_hint
    # supports the high-count claim. See linewebtoon._populate_chapter_counts
    # for the data source on the webtoons side. NOT surfaced in to_json
    # intentionally — internal-only ranking signal.
    count_outlier: bool = False
    # Matched-page probe results (sites/match_session.py, applied in
    # _run_match_phase). basis None = not probed this run (`_quality_basis`
    # reports "seed"). edition: original | alternate | undecided; an alternate
    # edition (fan-colored etc.) ranks after originals and never becomes a
    # fallback (aio_search_cli._filter_and_rank_alt_sources). tie_group /
    # match_group order sources inside source_rank_key; coverage = readable
    # whole chapter numbers, the first key inside a tie group.
    basis: Optional[str] = None
    edition: Optional[str] = None
    tie_group: Optional[int] = None
    match_group: Optional[int] = None
    coverage: Optional[int] = None


def _source_sinks(s: "SourceEntry") -> bool:
    """True when source_rank_key must push this source to the back of its candidate.

    dmca_likely / count_outlier need peers or a site-claimed count to fire
    (the cross-site check in search_all). A VERIFIED count of zero needs
    neither: the site answered that it has no readable chapter, so it can't
    be the download source whatever its title or image rating says. Live
    case: MangaDex Goblin Slayer, all 203 rows publisher link-outs, which its
    search() now counts as 0 (includeExternalUrl=0, sites/mangadex.py).
    None (count not measured) never sinks. Internal like count_outlier:
    to_json shows the count, not a flag.
    """
    return bool(s.dmca_likely or s.count_outlier or s.actual_chapter_count == 0)


def _quality_basis(s: "SourceEntry") -> str:
    """Provenance of the rating the UI displays for this source.

      - "matched": compared on the same pages with the series' main group of
        sources; the rating is relative to the best copy found (~0.95).
      - "partial": compared, but only within a smaller group with no page in
        common with the main group; the rating is relative to that group.
      - "solo": the only source that served pages; nothing to compare with
        (img_quality_score None, the seed shows).
      - "unmatched": served pages, none matched a peer's (webtoon strips until
        P3, a different layout or edition); seed shows.
      - "unreachable": every non-timeout fetch failed, or no readable chapter;
        rated 0.0 and sinks.
      - "pending": timed out or cut by the deadline; seed shows, re-probed soon.
      - "seed": not probed (seed below CHAPTER_PROBE_MIN_SEED, outside the
        probe scope, SKIP_QUALITY_PROBE, skip_probe_sites, late fan-out).

    Cross-file: emitted as `quality_basis` in SeriesCandidate.to_json; read by
    UI-source/src/components/SearchSourceCard.jsx, UI-source/electron/sync/
    find-sources.js and Android SearchModels.kt, all through one rule: only
    "matched" / "partial" show a number, every other value a reason label
    (UI-source/src/lib/qualityDisplay.js, grep measuredRating).
    """
    return s.basis or "seed"


# Basis tiers inside one title band (plan rev 3 decision: unmatched sources rank
# after matched ones at equal title match). None / "seed" = 4.
_BASIS_TIER = {"matched": 0, "solo": 0, "partial": 1, "unmatched": 2, "pending": 3}


def best_title_of(sources: List["SourceEntry"]) -> float:
    """The title match the band in source_rank_key is measured from: the best
    among sources that do not sink (a sunk wrong-match or a source that served
    nothing must not set the band and push every live source out of it)."""
    pool = [s for s in sources if not _source_sinks(s) and s.basis != "unreachable"] or list(sources)
    return max((s.title_match for s in pool), default=0.0)


def source_rank_key(s: "SourceEntry", best_title: float) -> tuple:
    """The ONE source order. Used by search_all for every candidate's sources
    and by aio_search_cli._filter_and_rank_alt_sources for the fallback order
    (which also decides chapter_merger's anchor tie-break and the primary
    switch, so a coverage tie goes to the better copy - plan decision E). A
    plain key, not a pairwise comparator: the old `_cmp` window compared title
    matches pairwise, which is not transitive.

    1. sinks (DMCA, count outlier, verified zero chapters) and `unreachable` last;
    2. title band: within TIEBREAKER_WINDOW of `best_title` (best_title_of) is
       band 0; the rest follow by title match;
    3. in band 0, an official publisher (title >= IS_OFFICIAL_REQUIRES_TITLE_MATCH)
       first;
    4. alternate edition after original;
    5. basis tier (_BASIS_TIER): matched/solo, partial, unmatched, pending, seed;
    6. connected group (partial groups after the main one), tie group, then -
       outside tie groups only - the rating, seed when unmeasured. Inside a tie
       group the copies' difference is noise (same scan re-encoded), so coverage,
       seed and site order them deterministically instead of the rating."""
    sink = _source_sinks(s) or s.basis == "unreachable"
    in_band = best_title - s.title_match <= TIEBREAKER_WINDOW + 1e-9
    official = in_band and s.is_official and s.title_match >= IS_OFFICIAL_REQUIRES_TITLE_MATCH
    tier = _BASIS_TIER.get(s.basis or "seed", 4)
    if s.tie_group is not None:
        q = 0.0
    else:
        q = s.img_quality_score if s.img_quality_score is not None else s.seed_quality
    return (
        sink,
        0 if in_band else 1,
        0.0 if in_band else -s.title_match,
        0 if official else 1,
        1 if s.edition == "alternate" else 0,
        tier,
        s.match_group or 0,
        s.tie_group if s.tie_group is not None else 0,
        -q,
        -(s.coverage or 0),
        -s.seed_quality,
        s.site,
    )


def sort_sources(sources: List["SourceEntry"]) -> None:
    """Sort one candidate's sources in place by source_rank_key."""
    best = best_title_of(sources)
    sources.sort(key=lambda s: source_rank_key(s, best))


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
                    "quality_basis": _quality_basis(s),
                    "composite_score": round(s.composite_score, 4),
                    "chapter_count_hint": s.chapter_count_hint,
                    "actual_chapter_count": s.actual_chapter_count,
                    "dmca_likely": s.dmca_likely,
                    "is_official": s.is_official,
                    "edition": s.edition,
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
    """Persistent per-source record of the matched-page probe (schema 9).

    One entry per (site, series_url): the source's VERDICT (what it showed when
    probed: served pages / unreachable / pending, its edition call, depth,
    fetch counts) and its PAIR EDGES (median comparison with each peer it shared
    pages with). Ratings are NOT stored: search_all recomputes them at read time
    from the edges among the sources present in this run
    (match_session.rate_sources), so a new, missing, disabled or late source
    never invalidates the others' data. Read/written only by search_all's probe
    phase (grep _run_match_phase); records built by sites/match_session.py
    (verdict_to_json / edge_to_json).

    File ~/.aio-dl/cache/img_quality.json:
      { "<site>:<series_url>": {
          "schema_version": 9,
          "expires_at": epoch,       # per verdict: served 30 d (7 d when the
                                     # probe was cut by the deadline), unreachable
                                     # 24 h, pending 2 h (match_session.verdict_ttl)
          "verdict": {...},          # match_session.verdict_to_json
          "pairs": { "<peer key>": {"score", "n", "parts", "at", "same", "psnr"} },
        } }
    Edges are stored on BOTH endpoints' entries, each oriented in its owner's
    favour, and expire on their own clock (`at` + IMG_QUALITY_TTL_S).

    schema history: v1-v4 cover / single page / 5-page aggregate probes; v5-v8 the
    T1/T2/T3 single-image scorers (breadth sampling, content types, watermark and
    B&W rewrites). v9 (2026-10, plan sync-temp/plans/image-ranker-rework.md):
    matched pages compared pairwise; any other version is dropped at load.

    D7 (fixed in v9): v8's load rebuilt each entry from a fixed key set and
    dropped any entry missing scorer fields, which threw away every failure
    verdict, so a dead site was re-probed on every search. v9 passes the
    record through as stored and keeps failure verdicts for their short TTL.

    Writes are batched: put_records rewrites the file once per search.
    """

    SCHEMA_VERSION = 9

    def __init__(self, ttl_s: float = IMG_QUALITY_TTL_S) -> None:
        # ttl_s caps every entry's lifetime (tests pass a small one)
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
                    if not isinstance(v, dict) or v.get("schema_version") != self.SCHEMA_VERSION:
                        continue
                    try:
                        expires_at = float(v.get("expires_at") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if expires_at <= now or not isinstance(v.get("verdict"), dict):
                        continue
                    pairs = v.get("pairs")
                    cleaned[str(k)] = {
                        "schema_version": self.SCHEMA_VERSION,
                        "expires_at": expires_at,
                        "verdict": v["verdict"],
                        "pairs": pairs if isinstance(pairs, dict) else {},
                    }
                self._state = cleaned
        except (OSError, json.JSONDecodeError):
            pass
        self._run_one_shot_purges()

    # One-shot purges: (key prefix, marker file next to the snapshot). Each runs
    # once per machine — the first load that finds no marker drops the prefix's
    # entries, rewrites the snapshot, then writes the marker. A marker, not a
    # date cutoff, because stale entries are "written by code before the fix"
    # and a fixed date can't know when a given install upgraded.
    #   - mangadex: (2026-10-17) ratings probed before publisher link-out rows
    #     became placeholders. Redundant since v9 drops every v8 entry; delete
    #     with the mechanism once no other purge needs it.
    _ONE_SHOT_PURGES: Tuple[Tuple[str, str], ...] = (
        ("mangadex:", "img_quality.purged-mangadex-linkouts"),
    )

    def _run_one_shot_purges(self) -> None:
        cache_dir = os.path.dirname(self._snapshot_path)
        for prefix, marker_name in self._ONE_SHOT_PURGES:
            marker = os.path.join(cache_dir, marker_name)
            if os.path.exists(marker):
                continue
            stale = [k for k in self._state if k.startswith(prefix)]
            for k in stale:
                del self._state[k]
            if stale:
                self._save_snapshot()
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(f"purged {len(stale)} entries at {time.time():.0f}\n")
            except OSError:
                pass

    def _save_snapshot(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._snapshot_path), exist_ok=True)
            tmp = self._snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
            os.replace(tmp, self._snapshot_path)
        except OSError:
            pass

    def get_record(self, site: str, series_url: str):
        """(SourceVerdict, {peer key: PairEdge oriented from this source}) or
        None on a miss / expiry. Expired edges are left out."""
        from .match_session import verdict_from_json, edge_from_json
        if not site or not series_url:
            return None
        key = self._key(site, series_url)
        now = time.time()
        with self._lock:
            entry = self._state.get(key)
            if not entry or float(entry.get("expires_at") or 0.0) <= now:
                return None
            verdict = verdict_from_json(key, entry.get("verdict") or {})
            edges = {}
            for peer, d in (entry.get("pairs") or {}).items():
                if not isinstance(d, dict):
                    continue
                e = edge_from_json(key, peer, d)
                if e.at + IMG_QUALITY_TTL_S > now:
                    edges[peer] = e
            return verdict, edges

    def put_records(self, verdicts, edges) -> None:
        """Store one search's verdicts and pair edges, then write the file once.

        A verdict replaces the source's entry but keeps its pairs while that
        entry is unexpired (an edge to a peer not probed this time is still
        valid; an expired entry's pairs go with it). Exception: a `pending`
        verdict never replaces a fresh `served` one - a slow run says nothing
        about pages already measured (nor does a failed chapter list:
        match_session.no_page_evidence). A served verdict replacing a fresh served
        one keeps the deeper `depth`: the kept pairs still come from the deeper
        probe, and a source re-probed shallowly as another candidate's anchor
        (grep "the anchor" in _run_match_phase) must not be re-deepened next
        time its own candidate is the top one. Each edge lands on both
        endpoints' entries, when they exist; it does not extend their expiry."""
        from .match_session import verdict_to_json, verdict_ttl, edge_to_json, no_page_evidence
        now = time.time()
        with self._lock:
            for v in verdicts:
                cur = self._state.get(v.key)
                fresh = cur is not None and float(cur.get("expires_at") or 0.0) > now
                cur_v = (cur.get("verdict") or {}) if fresh else {}
                if fresh and no_page_evidence(v) and cur_v.get("status") == "served":
                    continue
                stored = verdict_to_json(v)
                if v.status == "served" and cur_v.get("status") == "served":
                    stored["depth"] = max(v.depth, int(cur_v.get("depth") or 0))
                self._state[v.key] = {
                    "schema_version": self.SCHEMA_VERSION,
                    "expires_at": now + min(self.ttl_s, verdict_ttl(v)),
                    "verdict": stored,
                    "pairs": (cur.get("pairs") or {}) if fresh else {},
                }
            for e in edges:
                for owner, edge in ((e.a, e), (e.b, e.flipped())):
                    entry = self._state.get(owner)
                    if entry is not None:
                        entry.setdefault("pairs", {})[edge.b] = edge_to_json(edge)
            self._save_snapshot()


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


def _title_merge_key(title: str) -> str:
    """_normalize_title with the spaces removed: the key the cross-site merge
    and anchored admission compare (grep `_title_merge_key(` in search_all).

    WHY spaces go: sites disagree on word breaks in romanized titles ("To Aru"
    vs "Toaru", "Chain Saw" vs "Chainsaw", "Onepunch-Man" vs "One Punch-Man"),
    and those are the same title. Measured 2026-10-17 over 16 live searches:
    over 647 candidate groups the stripped key joined exactly one more pair,
    and it was correct — so on its own it is nearly a no-op. It earns its keep
    in anchored admission, where an exact key is the only evidence a
    below-floor hit has.

    Only for COMPARING. Never shown to a user — fall back to a real title.
    """
    return _normalize_title(title).replace(" ", "")


# --- Title matching -----------------------------------------------------
def _best_title_match(
    query: str, hit: SearchHit, extra_queries: Sequence[str] = (),
) -> float:
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

    ``extra_queries`` are additional spellings of the SAME series the caller
    already knows (search_all passes the URL-seed's alt_names). The result is
    the max over every (query variant x hit candidate) pair, so a hit is
    admitted when it matches ANY known spelling. This is what lets a site that
    lists only the romaji survive an English-titled query: matching
    "The Disastrous Life of Saiki K." against comix's "Saiki Kusuo no Psi Nan"
    scores 0.466 and dies against the 0.55 floor, while three unrelated comix
    titles sharing one common word clear it — the gate was rejecting the right
    answer and admitting noise. Empty by default, so keyword searches (no seed)
    score exactly as they always did.

    Scores come from sites/fuzzy_match, which normalizes the three codepoints
    rapidfuzz's C++ and pure-Python backends disagree on. That matters most
    HERE: this compares a user query against titles scraped out of HTML, and a
    `&nbsp;` is a token separator to one backend and an ordinary character to
    the other. Android runs the pure-Python backend (no Chaquopy wheel exists),
    so without normalization the same query would rank sources differently on
    phone and desktop.
    """
    # RuntimeError, not the wrapper's ImportError: long-standing contract of
    # this function, and aio_search_cli reports it as a search-level failure.
    try:
        fuzzy_match.load_rapidfuzz()
    except ImportError as exc:
        raise RuntimeError(
            "rapidfuzz is required for cross-site search. "
            "Install with: pip install rapidfuzz"
        ) from exc

    candidates = [hit.title]
    candidates.extend(hit.alt_titles or [])
    if not candidates:
        return 0.0

    queries = [(query or "").strip()]
    queries.extend((e or "").strip() for e in extra_queries or ())
    queries = [q for q in queries if q]
    if not queries:
        return 0.0

    best = 0.0
    for q in queries:
        for cand in candidates:
            if not cand:
                continue
            score = fuzzy_match.wratio(q, cand) / 100.0
            if score > best:
                best = score
        # 1.0 is the ceiling — no later pair can raise it, and the variant
        # loop is the hot path on a URL-seed search (hits x variants x alts).
        if best >= 1.0:
            return best
    return best


def _collect_title_variants(
    query: str,
    seed_hits: Optional[Sequence[SearchHit]],
    title_variants: Optional[Sequence[str]],
) -> List[str]:
    """Distinct other spellings of the series being searched, in trust order.

    Seed hits first (their own title, then alt_titles — the URL the user
    pasted), then the caller's ``title_variants``. Deduped by
    _normalize_title against the query and each other, fragments shorter
    than _SEED_VARIANT_MIN_LEN dropped, capped at _SEED_VARIANT_MAX. The
    query itself is never in the result. Empty when neither source gives
    anything, which keeps keyword searches byte-identical to before.
    """
    raw: List[str] = []
    for sh in seed_hits or ():
        raw.append(sh.title)
        raw.extend(sh.alt_titles or [])
    raw.extend(title_variants or ())

    out: List[str] = []
    seen = {_normalize_title(query)}
    for alt in raw:
        if len(out) >= _SEED_VARIANT_MAX:
            break
        a = (alt or "").strip()
        if len(a) < _SEED_VARIANT_MIN_LEN:
            continue
        k = _normalize_title(a)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(a)
    return out


def _fanout_query_variants(variants: Sequence[str]) -> List[str]:
    """The subset of ``variants`` worth sending to other sites as a search query.

    Pure-ASCII names with at least one letter, in the given order, at most
    _SEED_QUERY_MAX. ASCII rather than "Latin script" on purpose: Vietnamese
    and other diacritic-heavy localizations are translations no English-indexed
    site lists, while the names that DO recover a series (romaji, the other
    English title) are plain ASCII in practice. Order is trust order from
    _collect_title_variants, so a site's first alt name — usually the main
    other title — goes first.
    """
    out: List[str] = []
    for v in variants:
        if len(out) >= _SEED_QUERY_MAX:
            break
        if v.isascii() and any(c.isalpha() for c in v):
            out.append(v)
    return out


def _src_key(src: "SourceEntry") -> str:
    """ImageQualityCache / match_session key of a source."""
    return f"{src.site}:{src.url}"


def _needs_probe(key: str, rec, depth: int, served_peers: List[str]) -> bool:
    """Whether a cached source must join this run's matched-page session.

    No record, or a served record shallower than this run wants (a source
    whose candidate became the top one is re-deepened), or a served record
    with no edge to any other cached served source present now (it could not
    be rated against them) - unless it was already found unmatched against
    every one of them. A failure verdict (unreachable / pending) is trusted for
    its short TTL instead of being re-probed (D7)."""
    if rec is None:
        return True
    verdict, edges = rec
    if verdict.status != "served":
        return False
    if verdict.depth < depth:
        return True
    peers = [p for p in served_peers if p != key]
    if not peers or any(p in edges for p in peers):
        return False
    return not set(peers) <= set(verdict.unmatched_against)


def _apply_ratings(sources: List["SourceEntry"], verdicts: Dict, edges) -> None:
    """Rate `sources` from verdicts + pair edges (match_session.rate_sources)
    and write the result onto each SourceEntry. Sources without a verdict are
    left untouched (seed)."""
    from .match_session import rate_sources, quality_metadata
    present = {k: v for k, v in verdicts.items()}
    ratings = rate_sources(present, list(edges))
    for src in sources:
        k = _src_key(src)
        v, r = present.get(k), ratings.get(k)
        if v is None or r is None:
            continue
        src.basis = r.basis
        src.edition = r.edition
        src.tie_group = r.tie_group
        src.match_group = r.group
        src.coverage = v.coverage or None
        src.img_quality_score = r.rating
        src.img_quality_metadata = quality_metadata(v, r)


def _edges_among(records: Dict, extra=()) -> List:
    """Pair edges among the given cached records (each stored on both ends;
    deduped and oriented a < b), then `extra` (this run's edges) on top."""
    out: Dict[Tuple[str, str], Any] = {}
    for key, (_, edges) in records.items():
        for peer, e in edges.items():
            if peer not in records:
                continue
            out[(min(key, peer), max(key, peer))] = e if key < peer else e.flipped()
    for e in extra:
        a, b = (e.a, e.b) if e.a < e.b else (e.b, e.a)
        out[(a, b)] = e if e.a == a else e.flipped()
    return list(out.values())


def _probe_eligible(src: "SourceEntry", skip_probe_sites: Optional[Set[str]]) -> bool:
    """Whether a source takes part in image-quality rating at all: seed >=
    CHAPTER_PROBE_MIN_SEED (the long tail of foreign-language extras stays on
    its seed, and the fallback filter drops it anyway), not in
    skip_probe_sites, a registered handler without SKIP_QUALITY_PROBE. Shared
    by _run_match_phase and _rate_candidate_from_cache so a late hit cannot
    change who is rated."""
    from . import get_handler_by_name
    if src.seed_quality < CHAPTER_PROBE_MIN_SEED:
        return False
    if skip_probe_sites and src.site in skip_probe_sites:
        return False
    handler = get_handler_by_name(src.site)
    return handler is not None and not getattr(handler, "SKIP_QUALITY_PROBE", False)


def _rate_candidate_from_cache(cand: "SeriesCandidate", img_cache: "ImageQualityCache",
                               skip_probe_sites: Optional[Set[str]] = None) -> None:
    """Re-rate a candidate entirely from the cache (which already holds this
    run's results): used after late fan-out adoption, which adds sources but
    runs no probe. Only probe-eligible sources are rated, as in the phase."""
    records = {}
    for src in cand.sources:
        if not _probe_eligible(src, skip_probe_sites):
            continue
        rec = img_cache.get_record(src.site, src.url)
        if rec is not None:
            records[_src_key(src)] = rec
    if records:
        _apply_ratings(cand.sources, {k: r[0] for k, r in records.items()}, _edges_among(records))


def _run_match_phase(
    probe_scope: List["SeriesCandidate"],
    *,
    hit_by_url: Dict[str, "SearchHit"],
    img_cache: "ImageQualityCache",
    scraper_factory: Callable,
    make_request: Callable,
    fetch_memo,
    parallelism: int,
    skip_probe_sites: Optional[Set[str]],
    on_status: Optional[Callable[[str], None]],
    probe_track_lock: threading.Lock,
    probe_inflight: Dict[Tuple[str, str], float],
    probe_times: List[Tuple[str, float]],
    fanout_elapsed: float,
) -> None:
    """search_all's image-quality phase: rate every probe-eligible source of the
    candidates in scope by comparing the SAME pages across sources
    (sites/match_session.py; plan sync-temp/plans/image-ranker-rework.md, P2).

    Eligible: _probe_eligible and a search hit exists. Cached
    sources are served from their verdicts + edges; only sources _needs_probe
    picks run, plus ONE cached anchor peer per candidate (the best-rated) so the
    new sources have a page in common with the rated ones. One session covers
    every candidate, so the probe pool and the phase deadline stay shared.
    Results go to the cache in one write."""
    from . import get_handler_by_name
    from .match_session import MatchSession, DEPTH_TOP, DEPTH_OTHER, no_page_evidence

    plans = []
    session_members = []
    n_cached = 0
    for rank, cand in enumerate(probe_scope):
        depth = DEPTH_TOP if rank == 0 else DEPTH_OTHER
        elig = []
        for src in cand.sources:
            hit = hit_by_url.get(src.url)
            if hit is None or not _probe_eligible(src, skip_probe_sites):
                continue
            elig.append((src, hit, get_handler_by_name(src.site)))
        cached = {}
        for src, _, _ in elig:
            rec = img_cache.get_record(src.site, src.url)
            if rec is not None:
                cached[_src_key(src)] = rec
        served = [k for k, (v, _) in cached.items() if v.status == "served"]
        need = [(src, hit, h) for src, hit, h in elig
                if _needs_probe(_src_key(src), cached.get(_src_key(src)), depth, served)]
        n_cached += len(elig) - len(need)
        members = list(need)
        if need:
            need_keys = {_src_key(s) for s, _, _ in need}
            linked = [k for k in served if k not in need_keys]
            if linked:
                # the anchor: the best cached copy by the cached ratings
                pre = {k: cached[k][0] for k in linked}
                from .match_session import rate_sources
                rated = rate_sources(pre, _edges_among({k: cached[k] for k in linked}))
                by_key = {_src_key(s): (s, hit, h) for s, hit, h in elig}
                anchor = min(linked, key=lambda k: (
                    _BASIS_TIER.get(rated[k].basis, 4), -(rated[k].rating or 0.0),
                    -by_key[k][0].seed_quality, k))
                members.append(by_key[anchor])
        plans.append((cand, depth, elig, cached))
        if members:
            session_members.append((rank, depth, [
                (s, hit, h, _source_sinks(s)) for s, hit, h in members]))

    session = None
    if session_members:
        n_probe = sum(len(m) for _, _, m in session_members)
        if on_status:
            extra = f", {n_cached} from cache" if n_cached else ""
            on_status(
                f"[*] Matching pages across {n_probe} sources{extra}... "
                f"(fan-out took {fanout_elapsed:.1f}s)"
            )
        scrapers: Dict[str, Any] = {}
        scraper_lock = threading.Lock()

        def get_scraper(entry, handler):
            if fetch_memo is not None:
                # Per-run memo (sites/fetch_memo.py): the winner chapter fetch
                # reuses this scraper (CF clearance included).
                return fetch_memo.get_scraper(entry.site, entry.url, lambda: scraper_factory(handler))
            k = _src_key(entry)
            with scraper_lock:
                if k not in scrapers:
                    scrapers[k] = scraper_factory(handler)
                return scrapers[k]

        # A source runs several tasks at once (one per chapter window), so the
        # in-flight entry lives until its LAST running task ends; keyed per
        # source because readers (the deadline line, site health `stuck`) are.
        running_n: Dict[Tuple[str, str], int] = {}

        def on_task(site, url, started):
            k = (site, url)
            with probe_track_lock:
                if started is not None:
                    running_n[k] = running_n.get(k, 0) + 1
                    probe_inflight.setdefault(k, started)
                else:
                    running_n[k] = running_n.get(k, 1) - 1
                    if running_n[k] <= 0:
                        running_n.pop(k, None)
                        probe_inflight.pop(k, None)

        session = MatchSession(
            session_members, get_scraper=get_scraper, make_request=make_request,
            fetch_memo=fetch_memo, parallelism=parallelism,
            deadline_s=PROBE_PHASE_DEADLINE_S, on_status=on_status, on_task=on_task,
        )
        session.run()
        # Editions are voted only on pages >= 3 copy classes share, so a
        # source re-probed in a small session (the anchor, a re-deepened
        # source) comes back "undecided"; keep the cached call instead, or a
        # colored copy would requalify as a fallback for B&W downloads.
        cached_all = {k: rec for _, _, _, cached in plans for k, rec in cached.items()}
        for k, sv in session.verdicts.items():
            cv = cached_all.get(k)
            if sv.edition == "undecided" and cv is not None and cv[0].edition != "undecided":
                sv.edition = cv[0].edition
        with probe_track_lock:
            for s in session.by_key.values():
                # time the source's own tasks took, not its wait in the queue:
                # probe_times feeds the "slow site" health signal
                probe_times.append((s.site, s.busy_s))
        img_cache.put_records(list(session.verdicts.values()), session.edges)
        if on_status:
            served_n = sum(1 for v in session.verdicts.values() if v.status == "served")
            on_status(
                f"[*] matched-page probe: {served_n}/{len(session.verdicts)} sources served "
                f"pages; {session.compares} comparisons in {session.compare_s:.1f}s"
                + session.strip_note()
            )
            with probe_track_lock:
                slow = sorted(probe_times, key=lambda kv: -kv[1])[:5]
                stuck = sorted({site for (site, _u) in probe_inflight})
            if slow:
                on_status("[*] probe slowest: " + " · ".join(f"{n} {t:.1f}s" for n, t in slow))
            cut = sorted({s.site for s in session.by_key.values() if s.over_budget})
            if cut:
                on_status(f"[*] probe budget ran out for: {', '.join(cut)} "
                          f"(their unfinished pages are re-probed soon)")
            if session._truncated:
                on_status(
                    f"[!] Probe phase stopped at the {PROBE_PHASE_DEADLINE_S:.0f}s deadline "
                    f"(in-flight: {', '.join(stuck) or 'none'}); unfinished sources keep "
                    f"their seed and are re-probed soon."
                )

    for cand, depth, elig, cached in plans:
        verdicts = {k: rec[0] for k, rec in cached.items()}
        run_edges = []
        if session is not None:
            for src, _, _ in elig:
                k = _src_key(src)
                sv = session.verdicts.get(k)
                if sv is None:
                    continue
                cv = verdicts.get(k)
                # a slow run or a failed list says nothing about pages
                # measured before (match_session.no_page_evidence)
                if no_page_evidence(sv) and cv is not None and cv.status == "served":
                    continue
                verdicts[k] = sv
            keys = set(verdicts)
            run_edges = [e for e in session.edges if e.a in keys and e.b in keys]
        _apply_ratings(cand.sources, verdicts, _edges_among(cached, run_edges))


# --- Search-health classification --------------------------------------
def _classify_site_health(
    fanout_s: Optional[float],
    probe_s: Optional[float],
    *,
    errored: bool,
    blocked: bool,
    never_finished: bool,
    stuck: bool,
    constitutionally_slow: bool,
) -> Tuple[str, Optional[str]]:
    """Map a site's per-run search signals to a (status, reason) health verdict.

    status is "down" | "slow" | "ok". Hard-down signals always win over slow.
    `never_finished` = still pending at the fan-out soft barrier AND never
    adopted late (search_all's return-time `late_pending`); `stuck` = still
    in-flight when the probe phase abandoned at its deadline. `constitutionally_
    slow` handlers (SEARCH_COST_HINT=="slow" / EXPENSIVE_PROBE / OFFICIAL_
    PUBLISHER — comix's browser probe, official-publisher deep probes) are
    EXEMPT from the slow_probe signal only (their deep probe is slow by design);
    they still catch every hard-down signal. Pure function so
    tools/_test_site_health.py can exercise the truth table offline.
    """
    if errored:
        return "down", "error"
    if blocked:
        return "down", "blocked"
    if never_finished:
        return "down", "late_fanout"
    if stuck:
        return "down", "probe_stuck"
    if fanout_s is not None and fanout_s >= SEARCH_SLOW_FANOUT_S:
        return "slow", "slow_fanout"
    if (
        not constitutionally_slow
        and probe_s is not None
        and probe_s >= SEARCH_SLOW_PROBE_S
    ):
        return "slow", "slow_probe"
    return "ok", None


# --- Search-health reachability probe (2026-07-13, fix 3) ---------------
def _reachability_urls(handler) -> List[str]:
    """Base URLs to liveness-check for a handler, best-host first, deduped and
    capped at _REACHABILITY_MAX_URLS. Prefers a failover handler's own
    _candidate_domains() (so a moved-domain site like zeroscans → zscans.com is
    checked at its CURRENT mirrors, not just domains[0]), then any explicit
    base_url / _BASE_URL, then the universal `domains` tuple (apex form, www-
    deduped). Cross-file: consumed only by _probe_site_reachable."""
    urls: List[str] = []
    seen: set = set()

    def _add(u: Optional[str]) -> None:
        if isinstance(u, str) and u:
            key = u.rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                urls.append(u)

    cand = getattr(handler, "_candidate_domains", None)
    if callable(cand):
        try:
            for d in cand():
                if d:
                    _add(f"https://{d}/")
        except Exception:
            pass
    for attr in ("base_url", "_BASE_URL"):
        b = getattr(handler, attr, None)
        if isinstance(b, str) and b:
            _add(b if b.startswith("http") else f"https://{b}/")
    for d in (getattr(handler, "domains", None) or ()):
        if isinstance(d, str) and d:
            apex = d[4:] if d.startswith("www.") else d
            _add(f"https://{apex}/")
    return urls[:_REACHABILITY_MAX_URLS]


def _probe_site_reachable(handler, scraper_factory, *, timeout_s: float) -> Optional[bool]:
    """One bounded liveness GET per candidate host. Returns:
      True  — a host returned ANY HTTP status (even 4xx/5xx / a Cloudflare
              challenge: the server ANSWERED, so the site is reachable and any
              search-side failure is endpoint-specific, not a dead host).
      False — every candidate failed at the socket layer (DNS / conn refused /
              SSL / timeout) within the budget → genuinely unreachable.
      None  — couldn't attempt (no domains, unusable scraper, kwarg-signature
              mismatch) → caller keeps the mechanism verdict.
    Single attempt per host with a short timeout — deliberately NOT make_request
    (its 6× retry + backoff would re-eat ~40s and defeat a liveness check) and
    NOT its cross-process cooldown machinery (a raw .get must not trip the
    rate-limit coordinator). Handler-agnostic: uses the scraper's .get; a
    browser/exotic scraper without a compatible .get yields None (callers
    already skip constitutionally-slow/browser handlers — this is a backstop).
    Module-level (not a closure) so tools/_test_site_health.py can monkeypatch
    it for deterministic offline coverage."""
    urls = _reachability_urls(handler)
    if not urls:
        return None
    try:
        scraper = scraper_factory(handler)
    except Exception:
        return None
    getter = getattr(scraper, "get", None)
    if not callable(getter):
        return None
    for url in urls:
        try:
            r = getter(url, timeout=timeout_s, allow_redirects=True)
        except TypeError:
            # scraper.get doesn't accept our kwargs — don't risk an unbounded
            # bare call; report 'unknown' and keep the mechanism verdict.
            return None
        except Exception:
            continue  # socket-layer failure on THIS host — try the next mirror
        if getattr(r, "status_code", None) is not None:
            return True
    return False


def _refine_with_reachability(
    status: str, reason: Optional[str], reachable: Optional[bool]
) -> Tuple[str, Optional[str]]:
    """Second-stage correction of a mechanism verdict using an emit-time
    liveness result. `reachable` is True / False / None (not probed → keep as
    is). Pure function — offline truth-table in tools/_test_site_health.py.

    - `blocked` is untouched: the ProbeFailureCache is the authority there and
      the host was NOT re-probed.
    - unreachable (False) → down/unreachable for every non-blocked reason. This
      is the ONE true 'the site is dead' bucket (zeroscans' swallowed all-
      mirrors failure; omegascans timing out).
    - reachable (True) → the host is UP, so a search-side problem is never
      'down': an errored search softens to slow/search_error (mangakatana — its
      search flaked but downloads are fine), and any other hard-down verdict
      (late_fanout / probe_stuck) softens to 'slow' keeping its reason. An
      already-'slow' verdict is unchanged.
    """
    if reason == "blocked":
        return status, reason
    if reachable is False:
        return "down", "unreachable"
    if reachable is True:
        if reason == "error":
            return "slow", "search_error"
        if status == "down":
            return "slow", reason
        return status, reason
    return status, reason


def _run_reachability_probe(
    pending: List[dict], scraper_factory
) -> Dict[str, Optional[bool]]:
    """Liveness-probe a list of flagged-site pending dicts in parallel daemon
    workers under a hard phase deadline. Returns {name -> True/False/None};
    names unresolved when the deadline fires are omitted (the caller treats a
    missing name as None → keeps the mechanism verdict). Mirrors the fan-out /
    image-probe daemon+deadline pattern so a wedged host can't hang the run."""
    results: Dict[str, Optional[bool]] = {}
    rlock = threading.Lock()
    rq: "queue.Queue" = queue.Queue()
    for p in pending:
        rq.put(p)

    def _rworker() -> None:
        while True:
            try:
                p = rq.get_nowait()
            except queue.Empty:
                return
            verdict: Optional[bool] = None
            try:
                verdict = _probe_site_reachable(
                    p["handler"], scraper_factory,
                    timeout_s=SEARCH_REACHABILITY_TIMEOUT_S,
                )
            except Exception:
                verdict = None
            with rlock:
                results[p["name"]] = verdict

    n_workers = max(1, min(len(pending), _FANOUT_MAX))
    threads = [
        threading.Thread(target=_rworker, name="search-reach", daemon=True)
        for _ in range(n_workers)
    ]
    for t in threads:
        t.start()
    deadline = time.monotonic() + SEARCH_REACHABILITY_DEADLINE_S
    for t in threads:
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        t.join(timeout=rem)
    with rlock:
        return dict(results)


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
    probe_candidate_limit: Optional[int] = None,
    fetch_memo=None,
    exclude_sites: Optional[Set[str]] = None,
    diagnostics: Optional[dict] = None,
    title_variants: Optional[Sequence[str]] = None,
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
      skip_probe_sites: optional set of site names left out of the
        image-quality phase (no cache read, no probe; img_quality_score stays
        None -> seed). No caller passes it since the matched-page ranker: the
        direct-URL --multi-source path used to skip its committed primary, but
        the primary's pages are now what its alternatives are compared with and
        its edition decides which alternatives qualify.
      on_status: optional progress callback for human output (e.g., the
        "[*] Matching pages across N sources..." line).
      probe_candidate_limit: cap how many top candidates get their sources
        image-quality probed at ALL (2026-07-12). None = every candidate
        (today's behavior; api.py + tests). Callers that only ever read the
        top candidate(s) pass a small int: aio_search_cli passes 1 for
        --auto-pick and direct-URL --multi-source (only candidates[0] is
        consumed) and 2 for UI searches (user decision: "titles can be
        slightly different", keep the runner-up comparable). Safe for
        ranking because candidate ORDER is pure title-match — probes only
        reorder sources WITHIN a candidate. Skipped sources keep
        img_quality_score=None → seed fallback, same as SKIP_QUALITY_PROBE.
      fetch_memo: optional sites.fetch_memo.FetchMemo shared across this
        run's phases. The probe phase populates it (scrapers + contexts +
        chapter lists); the caller's winner chapter fetch then reuses
        those instead of re-fetching identical data a 2nd/3rd time.
      exclude_sites: optional set of handler names (lowercase) to drop from the
        fan-out + probe entirely — the user's disabled-sites list (--disable-
        sites). Filtered in the eligibility loop before the blocked/seeded
        checks. A URL seed_hit for an excluded site still rides (explicit user
        intent overrides the block); excluded sites also get NO site_health
        entry (below) so a disabled site isn't re-flagged as down.
      diagnostics: optional mutable dict the caller passes to receive per-run
        observability WITHOUT changing the return type (same collaborator
        pattern as probe_failure_cache / img_quality_cache / fetch_memo). When
        provided, search_all fills diagnostics["site_health"] (a list of
        {site, display_name, host, status, reason, fanout_s, probe_s} for every
        slow/down participating site, plus status "busy" for a site whose
        search raised SiteBusyError — a notice, not a verdict),
        ["eligible_count"], ["tested_sites"] and ["phase_times"].
        Assembled at return under the fan-out/probe locks (stragglers may still
        be mutating the timing containers). See _classify_site_health.
      title_variants: other names of the SAME series that the caller already
        trusts, beyond what seed_hits carry — the direct-URL --multi-source
        path passes the primary page's alt_names (it has no seed hit). Merged
        with the seed hits' title + alt_titles into one variant list that is
        (a) scored against every hit (_best_title_match extra_queries) and
        (b) sent as extra search queries to sites whose primary search found
        no exact match (_fanout_query_variants, _run_one). Never pass
        user-unverified names here (e.g. AniList synonyms off a library entry
        — see external_metadata's poisoned-synonym note): they widen
        admission. None/empty with no seed_hits = keyword search, scored and
        queried exactly as before.

    Returns:
      List of SeriesCandidate, ranked best-first.
    """
    from . import iter_search_capable_handlers

    # Phase-timing instrumentation (2026-07-12): monotonic checkpoints at
    # entry / post-fan-out / post-probe-join, surfaced via
    # on_status as one summary line before return. Pure observability — the
    # numbers are what the live benchmarks in the perf plan compare.
    _phase_t0 = time.monotonic()
    _fanout_elapsed = 0.0
    _probe_elapsed = 0.0

    # Per-site timing + failure attribution for the search-health signal
    # (2026-07-13). Populated by the fan-out and probe phases below (each under
    # its own lock — fanout_lock / probe_track_lock) and snapshotted at return
    # time to build diagnostics["site_health"]. HOISTED to function scope (out
    # of the `if eligible:` fan-out block and the nested probe block) so the
    # return-time assembly is valid even when a phase never runs — no eligible
    # handlers, img_cache None, or zero cache-misses. errored_names is written
    # by _run_one's except branches UNDER fanout_lock so the return snapshot
    # can't tear; blocked_skipped is captured single-threaded in the
    # eligibility loop.
    fanout_times: Dict[str, float] = {}
    probe_times: List[Tuple[str, float]] = []
    probe_inflight: Dict[Tuple[str, str], float] = {}
    probe_track_lock = threading.Lock()
    errored_names: Set[str] = set()
    # name -> reason for sites whose search raised SiteBusyError: THIS machine
    # kept them from answering (mangafire's browser profile held by another AIO
    # process). Neither errored nor tested — see _emit_site_health.
    busy_names: Dict[str, str] = {}
    blocked_skipped: List[Tuple[str, str, str]] = []  # (name, display_name, host)

    cache = probe_failure_cache  # may be None
    img_cache = img_quality_cache  # may be None — probe phase skipped if None
    seed = _load_quality_seed()

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
    disabled_skipped = 0
    _exclude = {s.lower() for s in exclude_sites} if exclude_sites else set()
    for h in handlers:
        host = (h.domains[0] if getattr(h, "domains", None) else "") or ""
        # User-disabled sites (--disable-sites) drop out FIRST — before the
        # blocked/seeded checks — so the disable is authoritative. A matching
        # URL-seed hit (if any) still rides in all_hits: an explicit URL
        # overrides the block, same as the primary anchor in a download.
        if _exclude and h.name.lower() in _exclude:
            disabled_skipped += 1
            continue
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
            # Blocked = 2+ recent failures (down/unreachable). Surface it in the
            # health signal even though it never enters `eligible`.
            blocked_skipped.append((h.name, getattr(h, "display_name", h.name), host))
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
    if disabled_skipped and on_status:
        on_status(f"  --disable-sites: skipped {disabled_skipped} handler(s)")

    # Slow-first enqueue order (2026-07-12): handlers whose search() is
    # expensive (SEARCH_COST_HINT="slow" — comix's browser-driven typeahead
    # is the only one today) start at t=0 so their multi-second search
    # overlaps everyone else's, instead of landing in a late wave and ADDING
    # its full duration to the fan-out phase. Stable sort keeps registry
    # order within each cost class. Cross-file: BaseSiteHandler.
    # SEARCH_COST_HINT (sites/base.py) + ComixSiteHandler's override.
    eligible.sort(
        key=lambda h: 0 if getattr(h, "SEARCH_COST_HINT", "normal") == "slow" else 1
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

    # Other known names of the series (seed page's title + alt_names, then the
    # caller's title_variants). URL-seed mode searches every other site with
    # the seed site's title (aio_search_cli: `query = seed.title`), so without
    # these a site listing the series under another name (a) scores against a
    # title it was never going to match, and (b) is never even ASKED for that
    # name. They feed both: _best_title_match extra_queries for (a),
    # seed_query_variants in _run_one for (b). Computed BEFORE the fan-out
    # because (b) runs inside it. Keyword searches leave both empty.
    seed_title_variants: List[str] = _collect_title_variants(
        query, seed_hits, title_variants,
    )
    seed_query_variants: List[str] = _fanout_query_variants(seed_title_variants)
    # Sites whose extra-name search ran / added a hit — status line only.
    variant_queried: Set[str] = set()
    variant_recovered: Set[str] = set()
    if seed_query_variants and eligible and on_status:
        on_status(
            "[*] Sites without an exact match are also searched as: "
            + ", ".join(repr(v) for v in seed_query_variants)
        )

    def _search_other_names(
        handler: BaseSiteHandler, scraper: Any, hits: List[SearchHit],
        primary_s: float,
    ) -> List[SearchHit]:
        """Re-search ``handler`` under seed_query_variants when its primary
        search found no exact match; returns ``hits`` plus any new ones.

        Skipped when the primary already holds an exact title (the series is
        found; another query only adds noise) or when the primary took at least
        per_site_timeout_s — at that point the host is timing out, and a second
        query would wait the same timeout again and push the site past the
        fan-out barrier. A variant query that raises is dropped WITHOUT a
        ProbeFailureCache strike or errored_names entry: the primary already
        proved the host reachable, so the failure is about the query string.
        Stops at the first variant that yields an exact match. New hits dedupe
        by URL against what the site already returned.
        """
        if not seed_query_variants or primary_s >= per_site_timeout_s:
            return hits

        def _has_exact(hs: List[SearchHit]) -> bool:
            return any(
                _best_title_match(query, h, seed_title_variants)
                >= _SEED_QUERY_SATISFIED
                for h in hs
            )

        if _has_exact(hits):
            return hits
        merged = list(hits)
        seen_urls = {h.url for h in merged}
        with fanout_lock:
            variant_queried.add(handler.name)
        for variant in seed_query_variants:
            try:
                extra = handler.search(
                    variant, scraper, make_request,
                    language=language, limit=top_per_site,
                )
            except Exception:
                # SiteBusyError included: our own contention, and the primary
                # already answered — nothing to record. Try the next name.
                continue
            if not isinstance(extra, list):
                continue
            new = [h for h in extra if h.url not in seen_urls]
            seen_urls.update(h.url for h in new)
            merged.extend(new)
            if _has_exact(new):
                with fanout_lock:
                    variant_recovered.add(handler.name)
                break
        return merged

    def _run_one(handler: BaseSiteHandler) -> List[SearchHit]:
        host = (handler.domains[0] if getattr(handler, "domains", None) else "") or ""
        try:
            scraper = scraper_factory(handler)
        except Exception:
            # Attribute this run's failure to the site for the health signal —
            # under fanout_lock so the return-time set(errored_names) snapshot
            # can't tear against a straggler still erroring past the barrier.
            with fanout_lock:
                errored_names.add(handler.name)
            if cache and host:
                cache.record_failure(host)
            return []
        try:
            _primary_t0 = time.monotonic()
            hits = handler.search(
                query, scraper, make_request, language=language, limit=top_per_site
            )
            if not isinstance(hits, list):
                hits = []
            hits = _search_other_names(
                handler, scraper, hits, time.monotonic() - _primary_t0,
            )
            if cache and host and hits:
                # Record success ONLY on a NON-EMPTY result — that's the sole
                # proof we actually reached AND parsed the site. An empty list
                # is ambiguous: a genuine no-match, OR a handler that swallowed
                # its own network failure to [] (grep zeroscans.search's all-
                # mirrors-down path). The old UNCONDITIONAL record_success
                # marked a fully-unreachable host 'healthy', hiding it from both
                # the ProbeFailureCache block path AND the search-health signal
                # (2026-07-13, fix 1). Empty now records NOTHING — neither
                # success nor failure; the emit-time reachability probe
                # (_probe_site_reachable) is what splits down-from-slow for a
                # zero-hit site. Exceptions still record_failure below.
                cache.record_success(host)
            return hits
        except SiteBusyError as e:
            # Not the site's fault, so no ProbeFailureCache strike and no
            # errored_names entry — either would push the site toward a block
            # or a "disable this site" recommendation for our own contention.
            with fanout_lock:
                busy_names[handler.name] = getattr(e, "reason", None) or "busy"
            if on_status:
                on_status(f"[!] {handler.name}: {e}")
            return []
        except Exception:
            # Attribute this run's failure to the site for the health signal —
            # under fanout_lock so the return-time set(errored_names) snapshot
            # can't tear against a straggler still erroring past the barrier.
            with fanout_lock:
                errored_names.add(handler.name)
            if cache and host:
                cache.record_failure(host)
            return []

    # --- Fan-out (2026-07-12 rewrite): daemon workers + soft barrier -----
    # WHY not ThreadPoolExecutor: (a) its `with`-block join waited for the
    # SLOWEST handler — the old `fut.result(timeout=per_site_timeout_s)` was
    # dead code because as_completed only yields already-finished futures,
    # so the phase was unbounded in practice; (b) late adoption requires NOT
    # joining: a straggler past the barrier must keep running so its hits
    # can merge at finalization, and non-daemon executor threads would also
    # block interpreter exit on a wedged handler. Same daemon-thread pattern
    # as the probe phase below. Workers record success/failure into the
    # ProbeFailureCache themselves (inside _run_one, which is lock-guarded)
    # — only COMPLETED calls record, so a slow-but-alive straggler is never
    # punished as a dead host.
    fanout_results: Dict[str, List[SearchHit]] = {}
    fanout_lock = threading.Lock()
    fanout_pending: Set[str] = {h.name for h in eligible}
    fanout_done = threading.Event()
    late_pending: Set[str] = set()
    if not fanout_pending:
        fanout_done.set()

    _fanout_started = time.monotonic()
    if eligible:
        fan_q: "queue.Queue" = queue.Queue()
        for h in eligible:
            fan_q.put(h)

        # fanout_times (per-site search durations for the "slowest" diagnostic
        # line — the mangakatana class of problem is invisible without per-site
        # attribution) is hoisted to function scope; populated here under
        # fanout_lock so the return-time health assembly can snapshot it.

        def _fanout_worker() -> None:
            while True:
                try:
                    h = fan_q.get_nowait()
                except queue.Empty:
                    return
                _t = time.monotonic()
                hits = _run_one(h)  # never raises; swallows + records per-host
                with fanout_lock:
                    fanout_times[h.name] = time.monotonic() - _t
                    fanout_results[h.name] = hits or []
                    fanout_pending.discard(h.name)
                    if not fanout_pending:
                        fanout_done.set()

        # Wider than the probe phase's `parallelism`: fan-out tasks are 1-2
        # requests to DISTINCT hosts (politeness-safe), and at width 6 the
        # ~31 seeded handlers ran in 6 sequential waves. max() keeps a
        # user-raised --search-parallelism authoritative above _FANOUT_MAX.
        fan_workers = max(1, min(len(eligible), max(parallelism, _FANOUT_MAX)))
        for _ in range(fan_workers):
            threading.Thread(
                target=_fanout_worker, name="search-fan", daemon=True,
            ).start()

        fanout_done.wait(timeout=_FANOUT_DEADLINE_S)
        with fanout_lock:
            late_pending = set(fanout_pending)
            fanout_slowest = sorted(fanout_times.items(), key=lambda kv: -kv[1])[:5]
            for _name, hits in fanout_results.items():
                if hits:
                    all_hits.extend(hits)
        if on_status and fanout_slowest:
            on_status(
                "[*] fan-out slowest: "
                + " · ".join(f"{n} {t:.1f}s" for n, t in fanout_slowest)
            )
        if variant_queried and on_status:
            with fanout_lock:
                _vq, _vr = len(variant_queried), sorted(variant_recovered)
            on_status(
                f"[*] other-name search: {_vq} site(s) re-searched, exact match "
                f"found on {len(_vr)}" + (f" ({', '.join(_vr)})" if _vr else "")
            )
        if late_pending and on_status:
            on_status(
                f"[!] fan-out: {len(late_pending)} site(s) still searching at "
                f"{_FANOUT_DEADLINE_S:.0f}s ({', '.join(sorted(late_pending))}) "
                f"— continuing; late results merge before finalization"
            )
    _fanout_elapsed = time.monotonic() - _fanout_started

    # ---- Search-health snapshot (2026-07-13) ----
    # Reuse the timing the phases ALREADY measured (zero added network work) to
    # tell the caller which participating sites were slow or down. Defined here
    # (after the fan-out barrier) and called before EVERY return path — the
    # no-hits / no-match early returns below INCLUDED, because "every site is
    # down → no hits → early return" is exactly when the signal matters most.
    # Snapshots under the existing locks: daemon fan-out stragglers past the
    # barrier and probe workers past the deadline can still be mutating these
    # containers, so a bare read risks "dict/list changed size during iteration".
    # `late_pending` holds sites still pending at the barrier; by the FINAL
    # return the adoption block below has discarded any straggler that finished
    # late (those keep a real fanout_times entry and classify "slow" instead),
    # leaving only genuinely never-finished sites (down/late_fanout). Excluded
    # (user-disabled) sites get no entry so a disabled site isn't re-flagged as
    # down. Consumed by aio_search_cli's --search-json payload → the UI's
    # SlowSitesCallout. Closure over the phase locals (read at call time).
    def _emit_site_health() -> None:
        if diagnostics is None:
            return
        with fanout_lock:
            _ft = dict(fanout_times)
            _errored = set(errored_names)
            _never = set(late_pending)
            _blocked = list(blocked_skipped)
            _busy = dict(busy_names)
        with probe_track_lock:
            _stuck = {site for (site, _u) in probe_inflight}
            _probe_by_site: Dict[str, float] = {}
            for _site, _secs in probe_times:
                # A site can be probed once per source across the top 1-2
                # candidates; keep its WORST single probe.
                if _secs > _probe_by_site.get(_site, 0.0):
                    _probe_by_site[_site] = _secs

        # Stage 1 — mechanism verdict per eligible site (pure, no network). Each
        # _pending entry carries what the reachability refinement + payload
        # assembly below need. Blocked-skipped sites are appended afterward as
        # down/blocked and are NOT reachability-probed (the cache is authority).
        _pending: List[dict] = []
        for h in eligible:
            name = h.name
            if _exclude and name.lower() in _exclude:
                continue
            if name in _busy:
                continue  # reported as "busy" below, never classified
            host = (h.domains[0] if getattr(h, "domains", None) else "") or ""
            fo = _ft.get(name)
            pr = _probe_by_site.get(name)
            constitutionally_slow = (
                getattr(h, "SEARCH_COST_HINT", "normal") == "slow"
                or getattr(h, "EXPENSIVE_PROBE", False)
                or getattr(h, "OFFICIAL_PUBLISHER", False)
            )
            status, reason = _classify_site_health(
                fo, pr,
                errored=(name in _errored),
                blocked=bool(cache and host and cache.is_blocked(host)),
                never_finished=(name in _never),
                stuck=(name in _stuck),
                constitutionally_slow=constitutionally_slow,
            )
            _pending.append({
                "handler": h, "name": name, "host": host,
                "fo": fo, "pr": pr, "status": status, "reason": reason,
                "constitutionally_slow": constitutionally_slow,
            })

        # Stage 2 — bounded emit-time reachability probe (2026-07-13, fix 3).
        # ONLY sites already flagged non-ok are probed, and only plain-HTTP
        # handlers: constitutionally-slow / official / expensive handlers are
        # slow-or-special by design (probing comix's scraper_factory would
        # launch Patchright), and a blocked-verdict eligible site (a host that
        # crossed the cache threshold mid-run) is left to the cache. A healthy
        # all-ok search reaches here with an empty _to_probe → zero added
        # latency. See the reachability-constant header for the full rationale.
        _reach: Dict[str, Optional[bool]] = {}
        if _REACHABILITY_ENABLED:
            _to_probe = [
                p for p in _pending
                if p["status"] != "ok"
                and p["reason"] != "blocked"
                and not p["constitutionally_slow"]
            ][:_REACHABILITY_MAX_SITES]
            if _to_probe:
                _reach = _run_reachability_probe(_to_probe, scraper_factory)

        # Stage 3 — refine each mechanism verdict with the liveness result, feed
        # a confirmed-unreachable host into the cache, and assemble the payload.
        _health: List[dict] = []
        _unreachable_ct = 0
        for p in _pending:
            if p["status"] == "ok":
                continue  # healthy → no entry (keep the payload tiny)
            reachable = _reach.get(p["name"])
            status, reason = _refine_with_reachability(p["status"], p["reason"], reachable)
            if status == "ok":  # defensive — refine never returns ok, but guard
                continue
            # A newly-confirmed-unreachable site that did NOT already error this
            # run (its _run_one never recorded a failure — a swallow-to-[]
            # handler, or a slow-but-alive host that died before the emit probe)
            # feeds the cache so a persistently-dead host trends toward a block +
            # auto-skip on later runs. Errored sites already recorded in
            # _run_one's except — don't double-count (that would block a host
            # after a single run instead of the intended 2).
            if reachable is False:
                _unreachable_ct += 1
                if p["reason"] != "error" and cache and p["host"]:
                    try:
                        cache.record_failure(p["host"])
                    except Exception:
                        pass
            _health.append({
                "site": p["name"],
                "display_name": getattr(p["handler"], "display_name", p["name"]),
                "host": p["host"],
                "status": status,
                "reason": reason,
                "fanout_s": round(p["fo"], 1) if p["fo"] is not None else None,
                "probe_s": round(p["pr"], 1) if p["pr"] is not None else None,
            })
        # ProbeFailureCache-blocked sites never entered `eligible` — surface
        # them as down/blocked (unless the user already disabled them).
        for name, display_name, host in _blocked:
            if _exclude and name.lower() in _exclude:
                continue
            _health.append({
                "site": name,
                "display_name": display_name,
                "host": host,
                "status": "down",
                "reason": "blocked",
                "fanout_s": None,
                "probe_s": None,
            })
        # Busy sites: status "busy" is a NOTICE, not a verdict. The UI shows it
        # but adds no strike (useDownloader.js, grep `"busy"`), and they are
        # left out of tested_sites below so a busy run doesn't decay a strike
        # either — the site simply wasn't measured this time.
        for h in eligible:
            if h.name not in _busy or (_exclude and h.name.lower() in _exclude):
                continue
            fo = _ft.get(h.name)
            _health.append({
                "site": h.name,
                "display_name": getattr(h, "display_name", h.name),
                "host": (h.domains[0] if getattr(h, "domains", None) else "") or "",
                "status": "busy",
                "reason": _busy[h.name],
                "fanout_s": round(fo, 1) if fo is not None else None,
                "probe_s": None,
            })
        if on_status and _reach:
            on_status(
                f"[*] reachability: probed {len(_reach)} flagged site(s), "
                f"{_unreachable_ct} unreachable"
            )
        diagnostics["site_health"] = _health
        diagnostics["eligible_count"] = len(eligible)
        # The names of the sites actually fanned out this run (disabled /
        # seed-hit / blocked / unseeded sites never enter `eligible`). The UI
        # folds site_health as STRIKES and uses this as the DECAY set: a site
        # in tested_sites but absent from site_health came back healthy this
        # run → decay its strike; a site in NEITHER wasn't measured → leave it
        # untouched. Without this the UI can't tell "tested & ok" from "not
        # tested", so recovered sites would never drop off the recommend list.
        # Cross-file: useDownloader.js setSearchSiteHealth fold (grep tested_sites).
        diagnostics["tested_sites"] = [h.name for h in eligible if h.name not in _busy]
        diagnostics["phase_times"] = {
            "fanout": round(_fanout_elapsed, 1),
            "probe": round(_probe_elapsed, 1),
        }

    if not all_hits:
        # Nothing matched by the barrier and no seeds. Stragglers (if any)
        # are abandoned here — an empty result at ~the barrier matches the
        # user's "stop waiting" semantics; a handler that alone needs >60s
        # for the only hits is the degenerate case we accept.
        _emit_site_health()
        return []

    if seed_title_variants and on_status:
        _shown = ", ".join(seed_title_variants[:3])
        _more = f" +{len(seed_title_variants) - 3} more" if len(seed_title_variants) > 3 else ""
        on_status(
            f"[*] Also matching against {len(seed_title_variants)} seed "
            f"alt-title(s): {_shown}{_more}"
        )

    # Score every hit against the query.
    scored: List[Tuple[float, SearchHit]] = []
    below_floor: List[SearchHit] = []
    for hit in all_hits:
        score = _best_title_match(query, hit, seed_title_variants)
        if score >= min_match:
            scored.append((score, hit))
        else:
            below_floor.append(hit)

    if not scored:
        _emit_site_health()
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

    def _hit_merge_keys(hit: SearchHit) -> List[str]:
        keys: List[str] = []
        for t in [hit.title, *(hit.alt_titles or [])]:
            k = _title_merge_key(t)
            if k and k not in keys:
                keys.append(k)
        return keys

    # Pass 1: build keysets per hit; union all keys within a hit; track which
    # hits ended up in which root.
    hit_keys: List[Tuple[float, SearchHit, List[str]]] = []
    # merge key -> best title-match of an ADMITTED hit carrying it. The
    # evidence anchored admission (below) and the late-adoption block read.
    anchor_score: Dict[str, float] = {}
    for score, hit in scored:
        keys = _hit_merge_keys(hit)
        if not keys:
            continue
        for k in keys:
            _find(k)  # ensure node exists
            if score > anchor_score.get(k, 0.0):
                anchor_score[k] = score
        for k in keys[1:]:
            _union(keys[0], k)
        hit_keys.append((score, hit, keys))

    # ANCHORED ADMISSION (2026-10-17). A hit below the title-match floor is
    # admitted when one of its titles has exactly the merge key of a title an
    # ADMITTED hit carries — the other site already vouched for that title.
    # WHY: the floor compares the query with each site's own spelling, and
    # loses the right series whenever a site lists it differently from the
    # query: mangahub's only title for Railgun is the romaji "To Aru Kagaku no
    # Railgun" (0.50 vs the English query), and the floor's scorer is
    # case-sensitive, so linewebtoon's "CHAINSAW MAN" scores 0.25 and
    # mangafire's "SPY×FAMILY" 0.18. Each has a correctly-spelled twin on
    # another site. Measured over 16 live searches: 38 hits admitted, about
    # 32 correct; the wrong ones all come from _normalize_title dropping
    # parentheticals ("(Novel)", "(Guide Book)"), an over-merge admitted hits
    # already get.
    # WHY not fix the scorer instead: making it case-insensitive admits 44-67
    # hits of mostly NOISE, because WRatio gives 0.855 to any title sharing a
    # single word with the query (memory search-title-gate-measurements).
    # Three rules keep this from growing candidates out of noise:
    #   * Anchors are only hits the FLOOR admitted — an anchored hit never
    #     anchors another (no chaining).
    #   * The hit attaches by its ONE best-scoring matching key and adds no
    #     union, so it can join a group but never weld two groups together.
    #   * It inherits that anchor's title-match: the key match is the
    #     evidence, so it ranks as the title it matched (its own score is a
    #     measure of the spelling, not of the series). Never above the
    #     group's best, so candidate ORDER cannot change.
    anchored_sites: List[str] = []
    for hit in below_floor:
        best_key, best = None, 0.0
        for k in _hit_merge_keys(hit):
            sc = anchor_score.get(k, 0.0)
            if sc > best:
                best_key, best = k, sc
        if best_key is None:
            continue
        hit_keys.append((best, hit, [best_key]))
        anchored_sites.append(hit.site)
    if anchored_sites and on_status:
        on_status(
            f"[*] title anchoring: admitted {len(anchored_sites)} hit(s) below "
            f"the match floor by exact title ({', '.join(sorted(set(anchored_sites)))})"
        )

    # Pass 2: bucket by root.
    groups: Dict[str, List[Tuple[float, SearchHit]]] = {}
    for score, hit, keys in hit_keys:
        root = _find(keys[0])
        groups.setdefault(root, []).append((score, hit))

    # Source order: source_rank_key (module level), shared with
    # aio_search_cli's fallback ranking. Applied at build time below, after the
    # image-quality phase, and after late adoption.
    from collections import Counter
    candidates: List[SeriesCandidate] = []
    # root-of-union-find → candidate object, for the late-adoption merge
    # (a straggler's hit attaches to an existing candidate when any of its
    # normalized title-keys _find()s to one of these roots).
    candidate_by_root: Dict[str, SeriesCandidate] = {}
    for key, members in groups.items():
        # canonical_title: most-common title across the bucket; shortest as
        # tiebreaker. Length-as-primary biases toward edition variants like
        # "One Piece - Digital Colored Comics" over the canonical "One Piece"
        # when 3 of 4 sources agree on the short name.
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
        # Both end up sinking to the back of the source list via
        # source_rank_key's first element; the differentiator is purely semantic (which flag
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
        # Sort now so output is deterministic even if the probe phase fails
        # entirely; the same key re-sorts after it rates the sources.
        sort_sources(sources)
        cand_obj = SeriesCandidate(
            canonical_title=canonical_title or key,
            canonical_year=year,
            sources=sources,
        )
        candidates.append(cand_obj)
        candidate_by_root[key] = cand_obj

    # ---- Image-quality phase: matched-page probe ----
    # Every probe-eligible source of the candidates in scope is rated by
    # comparing the SAME pages across sources (_run_match_phase,
    # sites/match_session.py). Cached sources are served from their verdicts +
    # pair edges; the rest are probed in one session that shares the pool and
    # the PROBE_PHASE_DEADLINE_S deadline. Replaces the per-source chapter
    # aggregate + cover probe + T3 pairwise pass (plan
    # sync-temp/plans/image-ranker-rework.md, P2).
    probe_scope: List[SeriesCandidate] = []   # read by late adoption below
    if img_cache is not None and candidates:
        # Candidate order is pure title match (the final sort below is the
        # same); ranking first tells the phase which candidate is the top one
        # (3 target chapters; others 1) and lets probe_candidate_limit cut the
        # scope.
        candidates.sort(
            key=lambda c: max((s.title_match for s in c.sources), default=0.0),
            reverse=True,
        )
        _probe_started = time.monotonic()
        hit_by_url: Dict[str, SearchHit] = {h.url: h for _, h in scored}
        # probe_candidate_limit (2026-07-12): candidates outside the scope keep
        # img_quality_score=None -> seed, exactly like SKIP_QUALITY_PROBE
        # sources; candidate RANK can't change because it's pure title match.
        if probe_candidate_limit is not None:
            probe_scope = candidates[: max(0, int(probe_candidate_limit))]
        else:
            probe_scope = candidates
        try:
            _run_match_phase(
                probe_scope,
                hit_by_url=hit_by_url,
                img_cache=img_cache,
                scraper_factory=scraper_factory,
                make_request=make_request,
                fetch_memo=fetch_memo,
                parallelism=parallelism,
                skip_probe_sites=skip_probe_sites,
                on_status=on_status,
                probe_track_lock=probe_track_lock,
                probe_inflight=probe_inflight,
                probe_times=probe_times,
                fanout_elapsed=_fanout_elapsed,
            )
        except Exception as _e:
            # ranking must survive a probe bug: sources keep their seed
            if on_status:
                on_status(f"[!] Image-quality phase failed: {_e!r}; ranking on seed")
        _probe_elapsed = time.monotonic() - _probe_started
        for c in candidates:
            sort_sources(c.sources)

    # ---- Late fan-out adoption (2026-07-12) ----
    # Stragglers that completed AFTER the soft barrier merge here — post-probe,
    # pre-final-rank — ranked by title match with cached ratings only: no fresh
    # probe (user decision 2026-07-12: a late site costs no visible time). The
    # candidates they join are re-rated from the cache, which already holds this
    # run's results (_rate_candidate_from_cache).
    # Documented edge, accepted by the same decision: a late candidate with
    # the best title-match can become candidates[0] with seed/cache-only
    # quality, so --auto-pick then picks on title-match + seed. Late entries
    # also skip the cross-site DMCA/count-outlier analysis (it ran at build
    # time); hit-level dmca_likely (e.g. mangadex sets it per-hit) still
    # rides in and still sinks in source_rank_key.
    if late_pending:
        with fanout_lock:
            late_now: Dict[str, List[SearchHit]] = {
                name: fanout_results[name]
                for name in list(late_pending)
                if name in fanout_results
            }
            for name in late_now:
                late_pending.discard(name)
        # Attach to an existing candidate when any normalized title-key
        # resolves through the union-find to a known root (READ-ONLY lookups
        # — parent is not mutated here, so pre-built candidates keep their
        # roots); late hits that match no existing candidate cluster among
        # THEMSELVES via late_key_map so two stragglers returning the same
        # series form one new candidate, not two.
        late_key_map: Dict[str, SeriesCandidate] = {}
        adopted_hits = 0
        adopted_into: List[SeriesCandidate] = []
        adopted_sites: set = set()
        for _name, hits in late_now.items():
            for hit in hits or []:
                score = _best_title_match(query, hit, seed_title_variants)
                keys = _hit_merge_keys(hit)
                if not keys:
                    continue
                anchored = score < min_match
                if anchored:
                    # Anchored admission, same rules as the build pass: only
                    # pre-barrier admitted hits anchor, one key, no new group.
                    best_key, best = None, 0.0
                    for k in keys:
                        sc = anchor_score.get(k, 0.0)
                        if sc > best:
                            best_key, best = k, sc
                    if best_key is None:
                        continue
                    score, keys = best, [best_key]
                target: Optional[SeriesCandidate] = None
                for k in keys:
                    if k in parent:
                        target = candidate_by_root.get(_find(k))
                        if target is not None:
                            break
                if target is None and anchored:
                    # Its anchor's candidate was filtered out after the build;
                    # an anchored hit never founds a candidate of its own.
                    continue
                if target is None:
                    for k in keys:
                        target = late_key_map.get(k)
                        if target is not None:
                            break
                # Same is_official derivation as the build loop: site-level
                # class attr ANDed with the per-hit flag when present.
                site_level = hit.site.lower() in official_sites
                per_hit = hit.is_official
                src_is_official = (
                    site_level if per_hit is None
                    else (bool(per_hit) and site_level)
                )
                entry = SourceEntry(
                    site=hit.site,
                    url=hit.url,
                    title=hit.title,
                    cover=hit.cover,
                    title_match=score,
                    seed_quality=seed.get(hit.site.lower(), 0.5),
                    composite_score=score,
                    chapter_count_hint=hit.chapter_count_hint,
                    actual_chapter_count=hit.actual_chapter_count,
                    dmca_likely=hit.dmca_likely,
                    raw_score=hit.raw_score,
                    is_official=src_is_official,
                )
                if target is None:
                    target = SeriesCandidate(
                        # keys are space-stripped comparison keys — never a
                        # display title (see _title_merge_key).
                        canonical_title=hit.title or next(
                            (a for a in (hit.alt_titles or []) if a), ""
                        ),
                        canonical_year=hit.year if isinstance(hit.year, int) else None,
                        sources=[],
                    )
                    candidates.append(target)
                for k in keys:
                    late_key_map.setdefault(k, target)
                # Per-site dedupe within the candidate — same rule as the
                # build loop's per_site_best (highest title_match wins). A
                # same-site entry can only pre-exist from THIS late batch:
                # the handler was late, so none of its hits were in the
                # pre-barrier build, and seeded sites never enter `eligible`.
                existing = next(
                    (s for s in target.sources if s.site == entry.site), None,
                )
                if existing is not None:
                    if entry.title_match <= existing.title_match:
                        continue
                    target.sources[target.sources.index(existing)] = entry
                else:
                    target.sources.append(entry)
                adopted_hits += 1
                if not any(t is target for t in adopted_into):
                    adopted_into.append(target)
                adopted_sites.add(hit.site)
        if adopted_hits:
            # Re-rate the candidates that gained a source (cached verdicts and
            # edges only) and re-sort them: adopted entries landed after the
            # post-probe sort. In-memory apart from the cache lookups.
            for c in adopted_into:
                # candidates outside probe_candidate_limit stay on seed, as in
                # the phase (a late hit must not change who is rated)
                if img_cache is not None and any(c is p for p in probe_scope):
                    _rate_candidate_from_cache(c, img_cache, skip_probe_sites)
                sort_sources(c.sources)
            if on_status:
                on_status(
                    f"[*] fan-out: merged {adopted_hits} late hit(s) from "
                    f"{', '.join(sorted(adopted_sites))} (seed/cached rating "
                    f"only — no probe)"
                )

    # Rank candidates: best title_match across any of their sources.
    candidates.sort(
        key=lambda c: max((s.title_match for s in c.sources), default=0.0),
        reverse=True,
    )
    if on_status:
        on_status(
            f"[*] Search phases: fan-out {_fanout_elapsed:.1f}s · probe "
            f"{_probe_elapsed:.1f}s · total "
            f"{time.monotonic() - _phase_t0:.1f}s"
        )

    # Emit the search-health snapshot on the success path too (defined after
    # the fan-out barrier above; also called before the no-hits/no-match early
    # returns). By this FINAL call the late-adoption block has run, so
    # late_pending holds only never-finished sites and the probe phase's timing
    # is included.
    _emit_site_health()

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
    "source_rank_key",
    "sort_sources",
    "best_title_of",
]
