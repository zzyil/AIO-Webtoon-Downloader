from __future__ import annotations

import datetime as dt
import math
import os
import queue
import re
import socket
import statistics
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from ._image_io import finalize_pending_image, looks_like_real_image
from .group_quality import MTL_CONFIRMED, MTL_NONE, classify_mtl, mtl_rank

# Preferred BeautifulSoup parser, detected once at import. lxml is faster and
# more lenient than the stdlib parser; handlers used to copy this probe into
# every __init__ + a _make_soup override (grep _make_soup). BaseSiteHandler
# ._make_soup centralizes both.
try:
    import lxml as _lxml  # noqa: F401
    _DEFAULT_SOUP_PARSER = "lxml"
except Exception:
    _DEFAULT_SOUP_PARSER = "html.parser"

# curl_cffi powers the fast image-download path used by handlers that opt into
# SUPPORTS_FAST_DOWNLOAD. HTTP/2 multiplex over a single keep-alive
# AsyncSession + Chrome-impersonate TLS fingerprint. Pinned to >=0.7.0 in
# requirements.txt for the AsyncSession API. ImportError fallback flips the
# module-level capability flag to False so opt-in handlers degrade to
# SUPPORTS_FAST_DOWNLOAD=False and the chapter loop reverts to its existing
# ThreadPoolExecutor + cloudscraper path. Cross-file: re-exported from
# sites/mangafire.py for back-compat with anything that grepped for the
# symbol there before this refactor.
try:
    from curl_cffi.requests import AsyncSession as _CurlCffiAsyncSession
    _CURL_CFFI_AVAILABLE = True
except Exception:  # ImportError or any sub-dep failure
    _CurlCffiAsyncSession = None  # type: ignore[assignment]
    _CURL_CFFI_AVAILABLE = False

# Exceptions from a probe fetch that mean "we couldn't MEASURE this page in time"
# (slowness), NOT "the page/CDN is definitively broken". The image-quality probe
# (_probe_chapter_aggregate) EXCLUDES these from the quality aggregate instead of
# scoring them 0.0, so a merely-slow-to-probe site (e.g. atsumaru: WebP-native,
# fast to download but slow to breadth-probe 8 full-res color chapters) isn't
# mislabeled as broken. requests.exceptions.Timeout already covers BOTH
# ConnectTimeout and ReadTimeout. Base requests.exceptions.ConnectionError
# (refused / reset / DNS) is DELIBERATELY NOT here — that's a genuine
# reachability failure and must stay a scored 0.0 (it's what keeps rizzcomic's
# connection-poison detection working). Grep _PROBE_TIMEOUT_EXCEPTIONS:
# _fetch_probe_item_bytes_ex + _probe_one_pick.
try:
    import requests as _requests
    _PROBE_TIMEOUT_EXCEPTIONS = (_requests.exceptions.Timeout, socket.timeout)
except Exception:
    _PROBE_TIMEOUT_EXCEPTIONS = (socket.timeout,)


class IncompleteChapterError(Exception):
    """Raised by handlers when a chapter cannot be fully fetched after the
    handler's own retry logic (e.g. MangaDex's MD@H node-swap loop has
    exhausted re-fetches but pages are still missing).

    The chapter download loop in aio-dl.py:_process_chapter_impl catches
    this around the get_chapter_images call and converts it to
    ChapterSkippedError, so the strict-wrapper retry / multi-source
    fallback / inline-retry machinery treats it the same as a Phase-2
    download failure. Without this signaling path, a handler that returns
    a truncated binary_image list would look "complete" to the validation
    block (since pages_total is computed AFTER the entries are classified).

    Cross-file: see aio-dl.py's existing ChapterSkippedError for the full
    retry contract; this exception only carries the diagnostic fields the
    wrapper needs to re-raise as ChapterSkippedError.
    """

    def __init__(
        self,
        pages_ok: int,
        pages_total: int,
        host: str = "",
        reason: str = "",
    ) -> None:
        self.pages_ok = int(pages_ok)
        self.pages_total = int(pages_total)
        self.host = host or ""
        self.reason = reason or "handler_incomplete"
        super().__init__(
            f"chapter incomplete: {self.pages_ok}/{self.pages_total} from "
            f"{self.host or '?'} ({self.reason})"
        )
        # INFRA-6: an identical second `class IncompleteChapterError` definition
        # followed here (a merge artifact that shadowed this one, byte-for-byte).
        # Removed — a single definition is authoritative.


@dataclass
class SiteComicContext:
    comic: Dict
    title: str
    identifier: str
    soup: Optional[BeautifulSoup] = None


@dataclass
class SearchHit:
    """Cross-site search result from a handler.search() call.

    Returned by handlers that implement search; consumed by
    sites/search_orchestrator.py to dedupe across sites and rank candidates.

    Field semantics:
      - site:    handler.name (e.g. 'mangadex'). Lets the orchestrator look up
                 quality_seed priors and route a chosen candidate back to
                 fetch_comic_context via the right handler.
      - url:     canonical comic URL that get_handler_for_url(url) will resolve
                 to this handler. Feeds straight into the existing single-URL
                 download flow when --auto-pick fires.
      - raw_score: site-internal relevance position, normalized 0..1. NOT used
                 directly for cross-site ranking — that's title_match (computed
                 by the orchestrator via rapidfuzz). raw_score is just a stable
                 fallback when the orchestrator can't compute its own score for
                 some reason.
      - alt_titles: used by rapidfuzz to match 'Frieren' against 'Sousou no Frieren'
                 etc. Empty list is fine; orchestrator just falls back to title.
      - chapter_count_hint: site's metadata claim about how many chapters exist
                 (e.g. MangaDex's attributes.lastChapter; MangaFire's "Chap N" badge).
                 Per-site definition — not normalized.
      - actual_chapter_count: how many chapters are actually fetchable in the
                 user's language. Set ONLY when handler bothered to verify (e.g.
                 MangaDex queries /chapter?manga=&limit=1 to read the total field).
                 None = not verified, not "unknown=zero". Used by the orchestrator
                 to detect DMCA-affected MangaDex entries: when chapter_count_hint
                 (metadata) >> actual_chapter_count, the series was likely
                 hollowed out by takedowns and the source is degraded.
      - dmca_likely: per-handler heuristic flag. True when chapter_count_hint
                 substantially exceeds actual_chapter_count (e.g. MD says 96
                 but only 1 EN chapter accessible). The orchestrator surfaces
                 this in JSON so users can see why a source is suspect.
      - is_official: per-hit override for handler.OFFICIAL_PUBLISHER.
                 None (default) = "defer to handler.OFFICIAL_PUBLISHER".
                 False = "this specific hit is NOT publisher-canonical
                 content" — e.g. linewebtoon Canvas user uploads:
                 webtoons.com hosts the file but LINE Webtoon doesn't
                 publish it, so canvas hits must not claim the official-
                 publisher tiebreaker that originals legitimately do.
                 Consumed by sites/search_orchestrator.py at the
                 SourceEntry.is_official assignment (AND'd with site-level
                 so a rogue handler can't claim official without the class
                 attribute set).
    """
    site: str
    title: str
    url: str
    cover: Optional[str] = None
    alt_titles: List[str] = field(default_factory=list)
    year: Optional[int] = None
    language: Optional[str] = None
    chapter_count_hint: Optional[int] = None
    actual_chapter_count: Optional[int] = None
    dmca_likely: bool = False
    raw_score: float = 0.0
    is_official: Optional[bool] = None


@dataclass
class AssetSpec:
    """A non-page auxiliary asset a handler discovered while scraping a
    chapter — audio to download, a motion-toon manifest/timeline to archive,
    or an external audio reference (SoundCloud) to record without downloading.

    WHY this exists (faithful-archival feature, local branch — see
    ~/.claude/plans/i-want-to-add-rustling-penguin.md): webtoons.com motion
    toons and tapas.io episodes carry `.mp3` clips, a proprietary motion
    timeline, and SoundCloud embeds that the static-image pipeline throws
    away. A handler stashes a list of these on the MUTABLE chapter dict as
    `chapter["_aux_assets"]` during get_chapter_images; the packaging loop in
    aio-dl.py fetches them into in-memory members embedded INSIDE the chapter
    CBZ under the reserved `_aio/` prefix (audio bytes + motion manifest).
    `_aio/` members are renumber-EXEMPT: build_cbz_from_content skips + preserves
    them (else the combined-archive renumber would turn an in-CBZ .m4a into a
    bogus {idx:04d}.m4a page). The user's own reader reads them out of the CBZ.

    Cross-file coupling (grep targets):
      - Producers: sites/tapas.py (audio_reference), sites/linewebtoon.py
        (_extract_motion_toon_pages: motion_manifest + audio_download +
        motion_layer; get_chapters has_bgm -> _resolve_bgm_specs audio_download).
      - Consumer: aio-dl.py `_materialize_chapter_aux` (fetches bytes), embedded
        into the per-chapter CBZ at build time (grep 'cached_cbz_path' /
        '_aux_members'). The record flows to the per-chapter ComicInfo.xml
        <AioChapterResources> (grep 'aux_records') + a series has_audio/
        has_motion rollup in details.json (grep '_scan_chapter_cbz_aux').

    Field semantics:
      - type:       one of "audio_download" | "motion_manifest" |
                    "motion_layer" | "audio_reference". The consumer switches
                    on this. Unknown types are ignored (forward-compatible).
      - source_url: the URL to download (audio_download) OR the reference URL
                    to record without downloading (audio_reference). None for
                    inline-data specs (motion_manifest carries `data`).
      - data:       inline bytes the handler already fetched (motion_manifest
                    holds the raw manifest JSON — no second HTTP round trip).
      - filename:   preferred on-disk name; the consumer sanitizes + de-dupes.
      - mime:       "audio/mpeg" | "application/json" | "text/uri-list" etc.
                    Advisory only.
      - meta:       free-form dict — motion layer->page map, timeline cues,
                    provider ("soundcloud"), has_bgm flags, etc. Serialized
                    verbatim into the per-chapter ComicInfo <AioChapterResources>
                    blob (embedded in the CBZ).
    """
    type: str
    source_url: Optional[str] = None
    data: Optional[bytes] = None
    filename: Optional[str] = None
    mime: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GroupInfo:
    """One scanlation group credited on a chapter version.

    THE canonical group representation. Handlers stash a list of these on the
    chapter dict as ``chapter["_groups"]``; everything that reasons about
    groups reads them back through ``BaseSiteHandler.get_group_infos``.

    WHY a structured list instead of the old per-site string: every handler
    invented its own key (``group_name`` / ``group`` / ``scanlator``) and its
    own near-identical ``get_group_name`` override, and a single string cannot
    express (a) a chapter co-released by two groups — mangadex and kagane both
    used to keep only the FIRST of a multi-group list, dynasty comma-joined
    them into one unmatchable blob — or (b) the trust signals the ranker needs
    (official / verified / inactive / description).

    ``chapter["group_name"]`` SURVIVES as the human-readable ``", ".join(names)``
    because five call sites read it as a plain string: aio_search_cli's
    per-source JSON, sites/chapter_merger.py, the ComicInfo ``<Translator>``
    writer, and the missed-chapter replay (grep ``grp_name`` in aio-dl.py).

    Field semantics:
      - name:        display name, RAW from the site. The MTL classifier's
                     case-sensitive "AI" guard depends on original casing.
      - group_id:    site-native stable id (MangaDex group UUID, atsumaru
                     scanlationMangaId). Preferred over name for catalog
                     lookups — names drift across rebrands, ids don't.
      - is_official: licensed publisher or site-operated release. Feeds the
                     ranker's official tier AND `--group official`.
      - verified:    the site's own verified badge (MangaDex only today).
      - inactive:    the site says the group is dead. Demotes, never excludes.
      - description: group self-description, where exposed (MangaDex only).
                     Second input to sites/group_quality.classify_mtl.
    """
    name: Optional[str] = None
    group_id: Optional[str] = None
    is_official: bool = False
    verified: bool = False
    inactive: bool = False
    description: Optional[str] = None


@dataclass(frozen=True)
class GroupSelectionPolicy:
    """Run-level inputs to select_best_chapter_version that aren't per-chapter.

    Built ONCE per series in aio-dl.py (grep ``build_group_census``) and passed
    into every per-chapter selector call. Deliberately NOT stashed on chapter
    dicts (they get copied by _annotate_selection and again by the
    collapse-splits merge, so a stashed field would have three lifetimes) and
    NOT cached on the handler (a module global keyed by series leaks across the
    --jobs orchestrator's in-process handler reuse and across --update-all).

      - census:        group match_key -> number of DISTINCT chapter numbers
                       that group supplies for THIS series. The strongest
                       zero-cost "is this the real TL or a filler dump" signal.
      - census_total:  distinct chapter numbers in the series, the denominator.
      - mtl:           "avoid" (default: rank last, still use when it's the only
                       version) | "allow" (ignore MTL entirely) | "exclude"
                       (skip chapters whose every version is CONFIRMED MTL).
      - excluded_keys: match_keys from --exclude-group.
    """
    census: Optional[Dict[str, int]] = None
    census_total: int = 0
    mtl: str = "avoid"
    excluded_keys: FrozenSet[str] = frozenset()


# `--group <one of these>` selects by the GroupInfo.is_official FLAG rather than
# by name. These are match keys (get_group_match_key output: casefolded,
# non-alphanumerics stripped), so "Official Release" arrives as
# "officialrelease". Grep target: group_matches_filter.
_OFFICIAL_ALIAS_KEYS = frozenset({
    "official", "officialrelease", "licensed", "publisher",
})

# --- Chapter-version ranking tuning ----------------------------------------
# Page count is a PLACEHOLDER detector, never a "more pages is better" signal:
# webtoon groups slice the same complete chapter differently (Solo Leveling
# ch.1 on atsumaru is Alpha 22p / Asura 22p / Flame 19p / Dusk 14p, all four
# complete). The band below flags only genuine stubs.
#   _PAGE_BAND_MIN_MEDIAN — below this a median can't distinguish a stub from a
#     legitimately short chapter (4-koma, omake), so the tier goes inert.
#   _PAGE_BAND_RATIO — 0.35 not 0.5, because re-slicing at 2x tile height
#     legitimately halves a count; 0.35 still catches the 3-of-20 class.
#   _PAGE_BAND_ABS_FLOOR — stops the ratio firing on tiny chapters at all
#     (0.35 * 6 = 2.1 would bless a 2-page entry).
_PAGE_BAND_MIN_MEDIAN = 8
_PAGE_BAND_RATIO = 0.35
_PAGE_BAND_ABS_FLOOR = 3

# Per-series track record, as a fraction of the series' distinct chapter count.
# Banded rather than raw so 198-vs-201 is a tie (noise) while 201-vs-12 is not.
_CENSUS_BANDS = ((0.60, 3), (0.25, 2), (0.05, 1))

# Index-aligned with the tuple built by BaseSiteHandler._rank_version. Used to
# name the tier that actually decided a pick, for the selection log.
_RANK_TIER_NAMES = (
    "exclusion", "downloadability", "MTL", "page-count", "official",
    "track-record", "verified", "upvotes", "recency", "pages", "source-order",
)


@lru_cache(maxsize=4096)
def _classify_group_mtl(
    name: Optional[str], description: Optional[str]
) -> Tuple[str, str]:
    """Memoized classify_mtl. A long series re-classifies the same handful of
    groups once per chapter otherwise (1000 chapters x 4 versions)."""
    return classify_mtl(name, description=description)


def _census_band(count: int, total: int) -> int:
    """Map a group's chapter count for THIS series onto a track-record tier."""
    if not total or count <= 0:
        return 0
    frac = count / total
    for threshold, band in _CENSUS_BANDS:
        if frac >= threshold:
            return band
    return 0


def _coerce_epoch(value: Any) -> Optional[int]:
    """Epoch seconds from a chapter's `uploaded`, or None.

    Strings are REJECTED on purpose: mangago stores a display string ("2 days
    ago"), and a wrong parse would silently reorder selections with no error.
    None keeps the recency tier inert for that handler instead.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _coerce_pages(version: Dict) -> Optional[int]:
    """Page count from a chapter dict, or None when the site doesn't report it.

    `_pages` is the established internal key (kagane, mangadex, atsumaru);
    `pages` is accepted for anything that surfaces it raw.
    """
    for key in ("_pages", "pages"):
        value = version.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
    return None


def build_group_census(
    handler: "BaseSiteHandler", chapters_by_num: Dict[str, List[Dict]]
) -> Tuple[Dict[str, int], int]:
    """Count how many DISTINCT chapter numbers each group supplies for a series.

    Distinct NUMBERS, not rows: a group that re-uploaded ch.5 three times must
    not earn 3x credit. Returns (match_key -> count, distinct chapter total).

    This is the ranker's strongest zero-cost signal — an MTL/filler group dumps
    a handful of chapters into a gap while the real TL has the long run — and it
    is the one fact a per-chapter selector cannot see for itself. Built once per
    series in aio-dl.py and passed down via GroupSelectionPolicy.

    Runtime-only: never persisted, never an argparse dest, never in
    gating_hash, so it cannot desync a resume.
    """
    census: Dict[str, int] = {}
    for versions in chapters_by_num.values():
        seen_here: set = set()
        for version in versions:
            for info in handler.get_group_infos(version):
                key = handler.get_group_match_key(info.name)
                if key:
                    seen_here.add(key)
        for key in seen_here:
            census[key] = census.get(key, 0) + 1
    return census, len(chapters_by_num)


# --- Search image-quality probe: breadth-sample concurrency -----------------
# How many of a source's breadth-sample chapters BaseSiteHandler.
# _probe_chapter_aggregate probes CONCURRENTLY. The breadth pass fetches 1 page
# from each of up to 8 chapters; those fetches used to run SERIALLY, so an
# HTML-scraped site whose page-image list lives in the chapter HTML (mangakatana:
# every sample = a full Cloudflare-fronted origin GET) cost ~40s cold-cache for
# a probe an actual chapter download does in ~4s. A small bounded daemon pool
# collapses that to ceil(picks / N) waves.
#
# BYTE-IDENTICAL to serial: WHICH chapter+page each sample fetches is fixed
# before any I/O by two pure pickers (_pick_representative_chapters /
# _pick_random_middle_page_index) and the aggregation (median/mean, Counter
# votes) is order-independent — concurrency only reorders the fetches. Default 4
# keeps the per-host origin burst modest (get_chapter_images routes through
# make_request -> _respect_rate_limit, which backs off on 429/503). Env override
# tunes the fleet default without a release (mirrors search_orchestrator.
# _FANOUT_MAX); a subclass sets PROBE_BREADTH_CONCURRENCY=1 to force serial
# (e.g. a future browser handler avoiding concurrent tabs). comix is unaffected
# (whole-method override). Grep target: PROBE_BREADTH_CONCURRENCY.
_PROBE_BREADTH_CONCURRENCY = 4
try:
    _PROBE_BREADTH_CONCURRENCY = max(
        1,
        int(os.environ.get("AIO_PROBE_BREADTH_CONCURRENCY", "") or _PROBE_BREADTH_CONCURRENCY),
    )
except (TypeError, ValueError):
    pass


@dataclass(frozen=True)
class _ProbeSample:
    """One breadth-sample chapter's probe result, written by a worker into a
    preallocated slot in _probe_chapter_aggregate's concurrent breadth pass.

    ``metadata is not None`` is the SINGLE "full success" discriminator — it
    drives the compacted ``per_chapter_metas`` rebuild. A FAILED sample carries
    ``metadata=None`` but may still carry ``image_items`` / ``picked_page_idx`` /
    ``blob`` (whatever it had computed before failing), exactly mirroring the
    per-branch field population of the former serial loop so the re-score pass
    and throttle tail see identical state.

    ``is_timeout`` marks a sample that could not be MEASURED in time (a network
    timeout in get_chapter_images / the image fetch, or a budget-miss slot swept
    after PROBE_SOURCE_BUDGET_S expired) — as opposed to a genuine content/CDN
    failure. Timeouts are EXCLUDED from the score aggregate (a slow site is not a
    broken one); genuine failures keep their 0.0. Default False so the existing
    positional constructors (successes + genuine failures) are unchanged.
    """
    score: float
    metadata: Optional[Dict]
    image_items: Optional[List]
    picked_page_idx: Optional[int]
    blob: Optional[bytes]
    is_timeout: bool = False


class BaseSiteHandler:
    """Base class for site-specific handlers."""

    name: str = "base"
    domains: tuple[str, ...] = ()
    # Preferred BeautifulSoup parser for _make_soup. None -> module default
    # (_DEFAULT_SOUP_PARSER, lxml if importable). Subclasses needing a specific
    # parser set this; most handlers just inherit _make_soup.
    _parser: Optional[str] = None
    # Shared chapter-number extractor for _chapter_number_from_text (grep it).
    # ~11 handlers inlined this exact `(\d+(?:\.\d+)?)` group; now one regex.
    _CHAPTER_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
    # When True, the orchestrator's image-quality probe phase clamps to a
    # SINGLE sample for low-title-match results (below
    # EXPENSIVE_PROBE_QUICK_THRESHOLD in search_orchestrator.py). Default
    # False — pure-HTTP handlers don't pay much for 5 samples, and we want
    # the full aggregate signal for them. Override to True on handlers
    # whose per-chapter fetch is expensive (e.g. browser-driven capture).
    # No handler sets this today (MangaFire's 2026 REST-API rewrite dropped
    # its VRF cost); browser-based handlers can opt in.
    EXPENSIVE_PROBE: bool = False

    # Relative cost class of this handler's search() for the search
    # orchestrator's fan-out scheduler. "slow" handlers are enqueued FIRST
    # (stable slow-first sort) so a multi-second search overlaps the cheap
    # HTTP handlers instead of starting in a late wave and adding its full
    # duration to the fan-out phase. comix (browser-driven typeahead: cold
    # Patchright launch + 28s inner budget) is the only "slow" today.
    # Values: "normal" | "slow". Cross-file consumer:
    # sites/search_orchestrator.py:search_all sorts `eligible` on this
    # before enqueue — grep SEARCH_COST_HINT.
    SEARCH_COST_HINT: str = "normal"

    # ADVISORY lower bound on the chapter numbers this run actually wants, set
    # per-INSTANCE by aio-dl.py just before get_chapters (grep
    # chapter_floor_hint there). Purely an optimization: a handler may ignore
    # it, and every handler that does still returns the full list, so
    # correctness never depends on it. The caller applies the real --chapters
    # filter afterwards either way.
    #
    # Exists for handlers whose chapter listing is paginated and expensive.
    # comix is the motivating case: its list is a browser DOM scrape over a
    # pager whose rows are per (chapter x group), so a long multi-group series
    # runs to ~360 page loads — while a routine update run only needs the
    # newest one or two pages. comix sorts newest-first, so it stops as soon as
    # a whole page falls below this value.
    #
    # None = no floor (full download); handlers must treat it as "walk
    # everything". Not a class-level policy knob — it changes per run, so it is
    # assigned on the instance, and it is NOT part of _RESUME_GATING_DESTS
    # (it can't change which bytes land on disk, only how much listing work we
    # do to find them).
    chapter_floor_hint: Optional[float] = None

    # When True, the search orchestrator treats this handler as the canonical
    # source for any series it returns — winning the per-candidate tiebreaker
    # over non-official aggregators WITHIN the same SeriesCandidate (post
    # union-find merge) regardless of measured image quality or title-match
    # spread. Set on handlers operating the publisher's own platform where
    # the canonical bytes originate; do NOT set on aggregators that re-host
    # other publishers' content (toonily, asura, mangafire, etc.).
    #
    # Why this matters: the image-quality probe scores resolution + format
    # + decode quality. Webtoons.com serves vertical-scroll PNGs at the
    # intentional 720–800px width — below the probe's res_score 800px floor,
    # so res_score contributes 0/1.0 to the composite. Aggregators that
    # upscale to 1500–2000px JPEG get a HIGHER res_score even though their
    # content is generation-loss from the official PNGs. Without this flag
    # the probe was confidently choosing toonily over webtoons.com despite
    # the latter being the literal publisher. Cross-file:
    # sites/search_orchestrator.py:SourceEntry.is_official + _cmp consume
    # this flag; grep OFFICIAL_PUBLISHER for the consumer side.
    #
    # Current opt-ins (lowercase handler names): linewebtoon.
    OFFICIAL_PUBLISHER: bool = False

    # When True, the chapter loop and the inter-chapter image prefetch route
    # this handler's image fetches through fast_download_images (curl_cffi
    # async + HTTP/2 multiplex over one keep-alive TLS session) instead of
    # the legacy dl_image + cloudscraper ThreadPoolExecutor path. Bench
    # (MangaFire, 83-page chapter, 2026-05-09): cloudscraper 3-thread =
    # 10.20s; curl_cffi async @ conc=8 = 6.04s. The win is HTTP/2 multiplex
    # eliminating per-page TLS handshake. Auto-disabled when curl_cffi
    # failed to import (falls back to the cloudscraper path).
    #
    # Opt in by setting True (typically `_CURL_CFFI_AVAILABLE`); subclasses
    # also override the FAST_DL_* attributes below if they need a custom
    # Referer / UA / TLS impersonate profile / extra headers. The base
    # fast_download_images method handles the rest.
    #
    # Don't opt in handlers whose image CDN requires a non-Chrome UA (e.g.
    # MangaDex's API ToS-mandated UA at sites/mangadex.py:configure_session) —
    # the curl_cffi `impersonate=` parameter overrides UA at the JA3/JA4
    # level too, and the API may reject the impersonated traffic outright.
    SUPPORTS_FAST_DOWNLOAD: bool = False

    # curl_cffi TLS-impersonate profile passed to AsyncSession(impersonate=).
    # "chrome120" gives a Chrome 120-equivalent JA3/JA4 + h2 settings frame.
    # Override only if the CDN requires a different fingerprint (very rare —
    # Cloudflare-fronted CDNs largely accept any modern Chrome profile).
    FAST_DL_IMPERSONATE: str = "chrome120"

    # User-Agent header sent with every fast-download request. None = let
    # curl_cffi fill the UA from the impersonate profile (Chrome's default
    # for that version). Override when consistency with the cloudscraper
    # session matters (MangaFire pins Chrome/122 because Cloudflare may
    # cookie-validate against the UA fingerprint of the cf_clearance cookie
    # captured by Patchright; mismatched UA invalidates the cookie).
    FAST_DL_USER_AGENT: Optional[str] = None

    # Static Referer URL (typically the site's homepage with trailing slash).
    # Empty string = no Referer header. Most aggregators serve images from
    # a separate CDN host and check Referer for anti-hotlink protection;
    # send the site's homepage URL to satisfy that check. Per-URL Referer
    # logic is rare; subclasses needing it override _fast_dl_build_headers
    # rather than this attribute.
    FAST_DL_REFERER_FROM: str = ""

    # Extra headers to send on every fast-download request (e.g.
    # X-Requested-With, custom auth tokens, locale hints). Built into the
    # request before Referer/User-Agent so those two attributes can override
    # entries here if both are set.
    FAST_DL_EXTRA_HEADERS: Dict[str, str] = {}

    def _make_soup(self, html: str) -> BeautifulSoup:
        """Parse HTML with the preferred parser (``self._parser`` or the module
        default — lxml if importable, else html.parser), falling back to
        html.parser if the chosen parser isn't installed. Handlers call this
        instead of ``BeautifulSoup(...)`` directly; it replaces the per-handler
        lxml-probe ``__init__`` + ``_make_soup`` copy ~11 handlers carried."""
        parser = self._parser or _DEFAULT_SOUP_PARSER
        try:
            return BeautifulSoup(html or "", parser)
        except FeatureNotFound:
            return BeautifulSoup(html or "", "html.parser")

    def _chapter_number_from_text(
        self, text: Optional[str], *, decimal_comma: bool = False
    ) -> Optional[str]:
        """Extract the first ``N`` / ``N.M`` run from a chapter label as a string.

        Centralizes the ``_CHAPTER_NUM_RE`` search that ~11 handlers inlined
        (madara, mangakatana, weebcentral, mangareader, mangapill, artlapsa,
        asmotoon, tcbscans, atsumaru, manhuaus, manhuaplus; grep
        _chapter_number_from_text). ``decimal_comma=True`` first maps ``','`` ->
        ``'.'`` for sites that write decimal chapters as ``"12,5"`` (asmotoon,
        artlapsa). Returns ``None`` for empty/None/no-digit input; callers that
        want the raw label on a miss do ``... or title``.
        """
        if not text:
            return None
        if decimal_comma:
            text = text.replace(",", ".")
        match = self._CHAPTER_NUM_RE.search(text)
        return match.group(1) if match else None

    def _meta_content(
        self,
        soup: BeautifulSoup,
        name: Optional[str] = None,
        property_name: Optional[str] = None,
    ) -> Optional[str]:
        """Return a stripped ``<meta>`` content value by ``name`` or ``property``.

        Tries ``name`` first, then ``property_name`` (either may be omitted).
        Consolidates the byte-identical copies asmotoon (formerly ``_meta``),
        artlapsa, and assortedscans each carried; grep _meta_content.
        """
        if name:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        if property_name:
            tag = soup.find("meta", attrs={"property": property_name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    @staticmethod
    def _parse_iso_z_timestamp(value: Optional[str]) -> Optional[int]:
        """Parse an ISO-8601 ``...Z`` timestamp (with or without fractional
        seconds) to a Unix-epoch ``int``; ``None`` if falsy/unparseable.

        Shared by weebcentral (``_extract_datetime``) and atsumaru
        (``_parse_chapter_entry``), which both inlined this exact two-format
        ladder. NOTE: strptime yields a NAIVE datetime, so ``.timestamp()``
        interprets it in the HOST's local timezone — preserved verbatim from
        both original call sites. sites/dynasty.py deliberately does NOT use
        this: it parses a date-only ``%Y-%m-%d`` anchored to UTC with a broad
        ``except`` (a genuinely different contract, left standalone).
        """
        if not value:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return int(dt.datetime.strptime(value, fmt).timestamp())
            except ValueError:
                continue
        return None

    def _rank_client_filter_hits(
        self,
        query: str,
        candidates: Iterable[Tuple[str, Any]],
        *,
        limit: int = 20,
    ) -> List[Tuple[float, Any]]:
        """Rank a locally-scraped catalog against ``query`` for sites with no
        server-side search (asmotoon, assortedscans, flamecomics, zeroscans,
        tcbscans all fetch their whole catalog page and filter in-process).

        ``candidates`` yields ``(title, payload)`` where ``payload`` is whatever
        the caller needs to emit a :class:`SearchHit` (an href, a source dict,
        an ``(href, cover)`` tuple...). Scoring mirrors what those five handlers
        each inlined: a full-query substring match scores 1.0, every query token
        present scores 0.7, anything else is dropped. Matches sort best-first and
        each surviving hit's ``raw_score`` decays with rank position as
        ``max(0.05, relevance * (1 - idx/n))`` where ``n`` is the total matched
        count BEFORE truncation to ``limit`` (so trimming the tail can't inflate
        the kept scores). Returns ``[(raw_score, payload), ...]``; the caller
        keeps ownership of SearchHit construction — including any post-rank skip
        (e.g. a missing id) applied in its own emit loop, which leaves the
        raw_score sequence identical to the pre-refactor inline code.
        """
        clean = (query or "").strip()
        if not clean:
            return []
        ql = clean.lower()
        query_tokens = {t for t in ql.split() if t}
        scored: List[Tuple[float, Any]] = []
        for title, payload in candidates:
            tl = (title or "").lower()
            if ql in tl:
                relevance = 1.0
            elif query_tokens and all(tok in tl for tok in query_tokens):
                relevance = 0.7
            else:
                continue
            scored.append((relevance, payload))
        scored.sort(key=lambda item: -item[0])
        total = len(scored)
        ranked: List[Tuple[float, Any]] = []
        for idx, (relevance, payload) in enumerate(scored[:limit]):
            raw_score = max(0.05, relevance * (1.0 - (idx / max(1, total))))
            ranked.append((raw_score, payload))
        return ranked

    def _fast_dl_build_headers(self, host: str) -> Dict[str, str]:
        """Build the headers dict sent with every fast-download request.

        Default implementation reads the FAST_DL_* class attributes. The
        `host` argument is provided so subclasses can override and inject
        per-host headers (rare); the default ignores it and emits a static
        dict driven entirely by the class config.

        Order: extra headers first, then Referer, then User-Agent — the last
        two override extras if a key collision happens (unlikely; called out
        for predictability).
        """
        headers: Dict[str, str] = dict(self.FAST_DL_EXTRA_HEADERS)
        if self.FAST_DL_REFERER_FROM:
            headers["Referer"] = self.FAST_DL_REFERER_FROM
        if self.FAST_DL_USER_AGENT:
            headers["User-Agent"] = self.FAST_DL_USER_AGENT
        return headers

    def fast_download_images(
        self,
        download_tasks: List[Tuple[int, str, str, str]],
        *,
        concurrency: int = 8,
        timeout: float = 30.0,
        is_cancelled: Optional[Callable[[], bool]] = None,
        record_host_failure: Optional[Callable[..., None]] = None,
        scraper: Any = None,
        pending_suffix: str = "",
    ) -> List[Tuple[int, Optional[str]]]:
        """Bulk-download chapter images via curl_cffi async + HTTP/2.

        Lifted from the original sites/mangafire.py implementation
        (2026-05-13 generalization) with three substitutions to make it
        handler-agnostic: headers come from _fast_dl_build_headers,
        impersonate comes from FAST_DL_IMPERSONATE, and an optional
        `scraper` kwarg lets callers forward cookies from the cloudscraper
        session to the curl_cffi session (handler-relevant for sites that
        gate their image CDN on session cookies).

        Args:
          download_tasks: list of (page_index, url, folder, filename) tuples,
                          same shape aio-dl.py constructs in Phase 1. The
                          filename is a base placeholder like "5_0001.jpg";
                          finalize_pending_image rewrites the extension based
                          on actual bytes.
          concurrency:    asyncio.Semaphore bound. 8 is the bench-stable
                          default. Past ~12 is diminishing returns on most
                          home networks (network-bandwidth-limited).
          timeout:        Per-request socket timeout. 30s matches aio-dl.py's
                          default _HTTP_TIMEOUT.
          is_cancelled:   Optional callback. When True, every in-flight fetch
                          checks before sending the next request and bails.
          record_host_failure: Optional callback fired when a URL hard-fails.
                          Updates aio-dl.py's _HOST_FAIL_COUNT so the chapter
                          watchdog can poison-detect a flaky CDN. Forward-
                          compatible kwarg signature: callers may pass
                          (host, url) or (host, url, status=..., body_size=...)
                          — the latter feeds the ghost-chapter signature
                          accumulator. Older overrides that pass only
                          (host, url) keep working; new code should forward
                          status + body_size when the response object is in
                          scope.
          scraper:        Optional cloudscraper session. When supplied, the
                          curl_cffi AsyncSession is constructed with cookies
                          forwarded from the scraper that match the host of
                          the first download URL (single-host-per-chapter
                          assumption — true in 99%+ cases). Lets handlers
                          like LineWebtoon ride along their .webtoons.com
                          age-gate cookies even though the curl_cffi session
                          is a separate TLS session from cloudscraper's.
          pending_suffix: Appended to the ".pending_<base>" tempfile name (NOT
                          the final page name — finalize_pending_image renames by
                          the explicit `base`). aio-dl.py's image-prefetch worker
                          passes ".bgprefetch" so a background page download can't
                          collide with the FOREGROUND writing the same page into
                          the same tdir after it adopts the chapter (S5-2 write
                          race); both finalize to the same page atomically.

        Returns: list of (page_index, path_or_None), ordered by page_index.
        path_or_None matches dl_image's contract — None signals failure.

        Subclass override pattern: most handlers won't need to override this
        method — set the FAST_DL_* class attributes instead. Subclasses
        that DO override should mirror the cancellation + record_host_failure
        callback shape so the existing aio-dl.py wiring continues to work.
        """
        if not _CURL_CFFI_AVAILABLE:
            raise RuntimeError(
                "fast_download_images called without curl_cffi installed. "
                "Caller should check SUPPORTS_FAST_DOWNLOAD before invoking."
            )
        if not download_tasks:
            return []

        import asyncio

        # Build cookies dict for the URL host. Filter scraper cookies to only
        # include those whose domain matches the target host (or which have
        # no domain at all — those ride along on every same-host request).
        # When scraper is None or has no relevant cookies, dict ends up empty.
        cookies: Optional[Dict[str, str]] = None
        if scraper is not None:
            try:
                first_host = urlparse(download_tasks[0][1]).netloc
                relevant: Dict[str, str] = {}
                for c in scraper.cookies:
                    cookie_domain = (c.domain or "").lstrip(".")
                    # No domain set → ride along on same-host. Domain set →
                    # match if the request host endswith the cookie domain.
                    if not cookie_domain or first_host.endswith(cookie_domain):
                        relevant[c.name] = c.value
                if relevant:
                    cookies = relevant
            except Exception:
                # Cookie extraction is best-effort; swallow and continue
                # with no cookies rather than failing the whole download.
                cookies = None

        # Headers built once per chapter — host parameter is for subclass
        # hooks; default implementation ignores it.
        first_host_for_headers = urlparse(download_tasks[0][1]).netloc
        headers = self._fast_dl_build_headers(first_host_for_headers)

        async def _fetch_one(
            session, sema, page_idx: int, url: str, folder: str, filename: str
        ) -> Tuple[int, Optional[str]]:
            base, _ = os.path.splitext(filename)
            if not base:
                base = filename
            pending_path = os.path.join(folder, f".pending_{base}{pending_suffix}")
            host = urlparse(url).netloc

            # Two attempts: original + one retry on transient failure. No
            # variant cascade — alternates rarely exist on image CDNs and
            # subclasses can override fast_download_images entirely if they
            # need one. (MangaFire confirmed: alternative path segments
            # /o/, /full/, /orig/ and extensions .png, .webp all 404.)
            for attempt in range(2):
                if is_cancelled is not None and is_cancelled():
                    return page_idx, None
                async with sema:
                    # Re-check after sema acquire — coroutines that were
                    # queued before cancel was set should still bail here
                    # rather than firing a GET they were already cancelled
                    # for. (Without this, large queues + late cancel = the
                    # remaining tail still issues HTTP requests.)
                    if is_cancelled is not None and is_cancelled():
                        return page_idx, None
                    try:
                        r = await session.get(url, headers=headers, timeout=timeout)
                    except Exception as exc:
                        # Per-attempt visibility: shows up in aio-dl's stderr
                        # which the UI's LogPanel surfaces. Ported from the
                        # 2026-05-13-deleted mangafire-specific
                        # fast_download_images for log-verbosity parity.
                        # Subclasses that don't want this can override
                        # fast_download_images and skip the print.
                        import sys
                        print(f"[-] curl_cffi exception: {exc} for URL: {url}", file=sys.stderr)
                        if attempt < 1:
                            await asyncio.sleep(1.0)
                            continue
                        if record_host_failure is not None:
                            try:
                                # No response object (request itself raised),
                                # so no status/body_size to forward. The
                                # callback's kwargs default to None and the
                                # ghost-detector treats absent signatures
                                # as zero-bucket — exceptions that all share
                                # the same cls also register as uniform if
                                # they all hit this path identically.
                                record_host_failure(host, url)
                            except Exception:
                                pass
                        return page_idx, None
                # Accept any HTTP-200 body that is a real image — INCLUDING a
                # legitimately tiny one (an 800x40 webtoon divider compresses to
                # ~128B). looks_like_real_image() rejects only sub-256B junk
                # (HTML/JSON stubs, 1x1 tracking pixels, truncated bodies); the
                # old blanket len<256 gate false-positived on real dividers and
                # aborted whole runs (bench/webtoonCanvasShelterLogs.md).
                if r.status_code != 200 or not looks_like_real_image(r.content):
                    import sys
                    body_len = len(r.content) if r.content else 0
                    print(
                        f"[-] curl_cffi status={r.status_code} "
                        f"size={body_len} for URL: {url}",
                        file=sys.stderr,
                    )
                    if attempt < 1:
                        await asyncio.sleep(1.0)
                        continue
                    if record_host_failure is not None:
                        try:
                            # Forward status + body_size so aio-dl.py's
                            # _record_failure can feed the ghost-chapter
                            # signature accumulator. Uniform (status,
                            # body_bucket) across every page of a chapter
                            # is the signal that distinguishes a fake/
                            # placeholder chapter from a transient CDN
                            # issue. See aio-dl.py:_is_ghost_chapter_signature.
                            record_host_failure(
                                host, url,
                                status=r.status_code,
                                body_size=body_len,
                            )
                        except Exception:
                            pass
                    return page_idx, None
                # Bytes look real — write pending file then atomic-rename.
                # finalize_pending_image runs sync; safe inside the coroutine
                # because file I/O is the same cost either way.
                try:
                    os.makedirs(folder, exist_ok=True)
                    with open(pending_path, "wb") as fh:
                        fh.write(r.content)
                except OSError:
                    return page_idx, None
                content_type = ""
                try:
                    content_type = r.headers.get("Content-Type", "") or ""
                except Exception:
                    content_type = ""
                try:
                    final = finalize_pending_image(
                        pending_path, folder, base, content_type
                    )
                except Exception:
                    # INFRA-1: an AV/indexer briefly locking the .pending_ file
                    # can make the atomic rename raise (Windows PermissionError).
                    # Drop just THIS page (the caller retries it) instead of
                    # letting the exception bubble out of gather and fail the
                    # whole chapter, discarding every sibling page that succeeded.
                    return page_idx, None
                return page_idx, final
            return page_idx, None

        async def _run() -> List[Tuple[int, Optional[str]]]:
            sema = asyncio.Semaphore(max(1, int(concurrency)))
            # Single AsyncSession across all pages of this chapter so HTTP/2
            # multiplex + connection keepalive amortize TLS handshake cost.
            # impersonate sets the JA3/JA4 + h2 settings frame to match a
            # real browser — should not strictly be needed for cookieless
            # edge-cached image CDNs, but defensive (and free).
            session_kwargs: Dict[str, Any] = {"impersonate": self.FAST_DL_IMPERSONATE}
            if cookies:
                session_kwargs["cookies"] = cookies
            async with _CurlCffiAsyncSession(**session_kwargs) as s:
                tasks = [
                    _fetch_one(s, sema, p_idx, url, folder, name)
                    for p_idx, url, folder, name in download_tasks
                ]
                # return_exceptions=True (INFRA-1): a single page coroutine that
                # raises unexpectedly must NOT abort gather and fail the WHOLE
                # chapter. Map any raised exception back to that page's
                # (idx, None) miss so the caller retries just that page; gather
                # preserves task order, so zip with download_tasks recovers the
                # page_idx the dead coroutine couldn't return.
                gathered = await asyncio.gather(*tasks, return_exceptions=True)
                out: List[Tuple[int, Optional[str]]] = []
                for (p_idx, _u, _f, _n), res in zip(download_tasks, gathered):
                    out.append(res if not isinstance(res, BaseException)
                               else (p_idx, None))
                return out

        # Run in this thread's own event loop. asyncio.run constructs a fresh
        # loop, so works whether called from main thread or from a daemon
        # prefetch thread (each has no running loop).
        results = asyncio.run(_run())
        # Preserve original submission order (page_idx ascending). gather()
        # already returns in input order, but sorting is cheap insurance.
        results.sort(key=lambda t: t[0])
        return results

    def matches(self, url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(domain in netloc for domain in self.domains)

    # --- Session lifecycle -------------------------------------------------
    def configure_session(self, scraper, args) -> None:
        """Give the handler a chance to tweak the HTTP session."""
        return None

    # Handlers needing main-thread setup before their get_chapters can run in
    # worker threads (e.g. comix's Patchright-based chapter-listing token
    # capture, which can't drive its sync API without a main-thread asyncio
    # loop) set this True AND override prepare_chapter_fetch. Read by
    # aio_search_cli._fetch_chapters_for_winner to skip the scraper-build cost
    # on the common path.
    NEEDS_MAIN_THREAD_PREFETCH: bool = False

    def prepare_chapter_fetch(
        self, url: str, scraper, args, make_request
    ) -> None:
        """Optional main-thread pre-warm hook invoked by
        aio_search_cli._fetch_chapters_for_winner BEFORE it dispatches the
        per-source ThreadPoolExecutor that runs fetch_comic_context +
        get_chapters across the candidate's sources.

        Handlers requiring main-thread initialization override this to warm
        any per-title cache so the worker-thread get_chapters call hits the
        cache instead of failing to capture. Default no-op; called only when
        NEEDS_MAIN_THREAD_PREFETCH is True on the subclass.

        Cross-file: aio_search_cli.py:_fetch_chapters_for_winner.
        """
        return None

    # --- Initial comic retrieval ------------------------------------------
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        """Return the key comic data for downstream processing."""
        raise NotImplementedError

    def extract_additional_metadata(
        self, context: SiteComicContext
    ) -> Dict[str, List[str]]:
        """Optional metadata enrichment hook."""
        return {}

    # --- Chapter helpers ---------------------------------------------------
    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        raise NotImplementedError

    def get_volumes(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        return []

    def get_group_infos(self, chapter_version: Dict) -> List[GroupInfo]:
        """Read a chapter's scanlation groups. THE canonical accessor.

        Precedence:
          1. ``chapter["_groups"]`` — a list of GroupInfo. The modern path;
             the only one that can express multi-group releases and trust
             signals.
          2. Legacy string keys, FIRST hit wins: group_name, group, scanlator,
             publisher. Wrapped as a single unadorned GroupInfo.

        The legacy string is deliberately NOT comma-split: a real group name
        can contain a comma, and multi-group releases are what `_groups` is
        for. (dynasty used to comma-join its scanlators here, which made
        `--group "X"` unable to match a chapter tagged "X, Y" — it now emits
        `_groups` instead.)

        Handlers should NOT override this. Ten of them used to override
        get_group_name with the same three-line "read my key, return it" body;
        the fallback chain above subsumes every one of them.
        """
        groups = chapter_version.get("_groups")
        if isinstance(groups, (list, tuple)):
            resolved = [g for g in groups if isinstance(g, GroupInfo)]
            if resolved:
                return resolved
        for key in ("group_name", "group", "scanlator", "publisher"):
            value = chapter_version.get(key)
            if isinstance(value, str) and value.strip():
                return [GroupInfo(name=value.strip())]
        return []

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        """Human-readable group credit, or None. Multi-group chapters join with
        ", " — this is display/metadata only (ComicInfo <Translator>, logs, the
        search JSON). Matching and ranking go through get_group_infos."""
        names = [g.name.strip() for g in self.get_group_infos(chapter_version)
                 if isinstance(g.name, str) and g.name.strip()]
        return ", ".join(names) if names else None

    def normalize_group_name(self, group_name: Optional[str]) -> Optional[str]:
        """Casefold + collapse separators. PURE text normalization.

        It used to also rewrite any name matching \\b(official|webtoons?|naver)\\b
        to the literal "official". That was destructive: it merged LINE Webtoon
        Originals with Canvas (defeating the distinction sites/linewebtoon.py
        builds on purpose), merged unrelated real groups ("Webtoon Scans",
        "Naver fan TL") into one bucket, and made all of them matchable by
        `--group official`. The official-alias capability now lives in
        group_matches_filter, keyed on the GroupInfo.is_official FLAG rather
        than on a substring of the name.
        """
        if not isinstance(group_name, str):
            return None
        cleaned = group_name.strip().casefold()
        if not cleaned:
            return None
        cleaned = re.sub(r"[_./-]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or None

    def get_group_match_key(self, group_name: Optional[str]) -> Optional[str]:
        normalized = self.normalize_group_name(group_name)
        if not normalized:
            return None
        squashed = re.sub(r"[^0-9a-z]+", "", normalized)
        return squashed or normalized

    def group_matches_filter(
        self, infos: List[GroupInfo], filter_key: Optional[str]
    ) -> bool:
        """Does any of a chapter's groups satisfy one --group / --exclude-group
        entry (already reduced to a match key)?

        `--group official` (and its aliases) matches on the is_official FLAG,
        not on the name — so it catches MangaPlus, Viz, LINE Originals, and
        mangago's /br_chapter- releases, while leaving a fan group merely
        NAMED "Webtoon Scans" alone. See normalize_group_name for why this
        moved out of the normalizer.
        """
        if not filter_key:
            return False
        if filter_key in _OFFICIAL_ALIAS_KEYS:
            return any(info.is_official for info in infos)
        return any(
            self.get_group_match_key(info.name) == filter_key for info in infos
        )

    # --- Chapter-version ranking -------------------------------------------
    def _version_signals(
        self, version: Dict, policy: GroupSelectionPolicy
    ) -> Dict[str, Any]:
        """Per-version facts the rank tuple needs, folded across the version's
        groups. Multi-group folding rules, and why each is what it is:

          - mtl: take the BEST verdict across groups. A chapter co-credited to
            a human group and an MTL shop had a human in the loop; taking the
            worst would demote a legitimate joint release and, on a site where
            the main TL sometimes co-credits, would fight the census tier.
          - is_official / verified: any group qualifies the chapter.
          - inactive: only when EVERY credited group is flagged inactive.
          - census band: max — a co-release inherits the main group's standing.
          - excluded: any — `--exclude-group X` means "not this group's work",
            and a co-credit is still that group's work.
        """
        infos = self.get_group_infos(version)
        mtl_verdict = MTL_NONE
        best_mtl = -1
        for info in infos:
            verdict, reason = _classify_group_mtl(info.name, info.description)
            rank = mtl_rank(verdict)
            if rank > best_mtl:
                best_mtl, mtl_verdict = rank, verdict
        if best_mtl < 0:
            best_mtl = mtl_rank(MTL_NONE)

        census = policy.census or {}
        band = 0
        for info in infos:
            key = self.get_group_match_key(info.name)
            if not key:
                continue
            band = max(band, _census_band(census.get(key, 0), policy.census_total))

        excluded = any(
            self.group_matches_filter([info], key)
            for info in infos
            for key in policy.excluded_keys
        ) if policy.excluded_keys else False

        return {
            "infos": infos,
            "mtl_verdict": mtl_verdict,
            "mtl_rank": best_mtl,
            "is_official": any(i.is_official for i in infos),
            "verified": any(i.verified for i in infos),
            "all_inactive": bool(infos) and all(i.inactive for i in infos),
            "census_band": band,
            "excluded": excluded,
        }

    def _build_rank_context(
        self, versions: List[Dict], policy: GroupSelectionPolicy
    ) -> Dict[str, Any]:
        """Cross-version facts computed ONCE, so a tier can go inert.

        THE core rule: a MISSING signal must never read as a LOSING signal. The
        old ranker's `v.get("up_count", 0)` treated "this site doesn't report
        votes" as "zero votes", which is why 8 of 11 group-bearing handlers
        collapsed to `max()`-returns-the-first-element, i.e. arbitrary API
        order. Every tier below is enabled only when it can actually
        discriminate, and returns a constant for every version otherwise.
        """
        upvote_values = [
            v.get("up_count") for v in versions
            if isinstance(v.get("up_count"), int) and not isinstance(v.get("up_count"), bool)
        ]
        # Needs >= 2 reporters AND a non-zero max: mangago hardcodes 0 for
        # every chapter, which is data-shaped but carries no information.
        upvotes_live = len(upvote_values) >= 2 and max(upvote_values) > 0

        epochs = [_coerce_epoch(v.get("uploaded")) for v in versions]
        recency_live = len(versions) > 1 and all(e is not None for e in epochs)

        page_counts = [_coerce_pages(v) for v in versions]
        known_pages = [p for p in page_counts if p is not None]
        pages_live = len(known_pages) == len(versions) and len(versions) > 1

        # Placeholder floor. Only meaningful with >=2 reported counts and a
        # median big enough to tell a stub from a genuinely short chapter.
        page_floor: Optional[float] = None
        if len(known_pages) >= 2:
            median_pages = statistics.median(known_pages)
            if median_pages >= _PAGE_BAND_MIN_MEDIAN:
                page_floor = max(
                    _PAGE_BAND_ABS_FLOOR, median_pages * _PAGE_BAND_RATIO
                )

        return {
            "policy": policy,
            "upvotes_live": upvotes_live,
            "recency_live": recency_live,
            "pages_live": pages_live,
            "page_floor": page_floor,
            # `--mtl allow` means "treat a machine translation like any other
            # version", so the tier must go inert rather than merely stop
            # excluding — otherwise allow and avoid rank identically.
            "mtl_live": policy.mtl != "allow",
            "signals": [self._version_signals(v, policy) for v in versions],
        }

    def _rank_version(self, index: int, version: Dict, ctx: Dict[str, Any]) -> Tuple:
        """The composite rank key. Higher wins; consumed by max().

        Tiers, most significant first. Each line states WHY it sits where it
        does — mirrors the documented tuple in
        sites/external_metadata.py:_pick_best_candidate.

         0 not_excluded    A hard user veto (--exclude-group / --mtl exclude)
                           dominates everything. Kept IN the tuple rather than
                           pre-filtered so an excluded-but-only version still
                           obeys the allow_group_fallback contract instead of
                           silently vanishing.
         1 downloadable    A MangaDex external chapter (pages:0 + externalUrl)
                           is metadata, not a chapter — ranking it first trades
                           a working download for a guaranteed abort. This tier
                           is what makes the official tier safe at all. Set
                           ONLY from an explicit handler flag, never inferred
                           from a missing field.
         2 mtl_rank        The headline fix. Above official because an
                           official-looking MTL doesn't exist, but a
                           high-upvote MTL demonstrably does.
         3 not_placeholder A 3-page stub beside 22-page siblings is a broken
                           upload, not a translation choice.
         4 official_rank   Licensed beats fan TL when both are downloadable.
                           Mirrors chapter_merger.py's cross-source
                           official-first row sort, so within-source and
                           cross-source selection finally agree.
         5 census_band     The group at 201/201 is the series' TL; the one that
                           dropped 12 chapters into a gap is filler. Banded so
                           198-vs-201 ties instead of shadowing everything
                           below.
         6 verified        The site's own curation badge. Free where it exists
                           (MangaDex only), absent everywhere else, so it can't
                           sit higher.
         7 upvote_band     Demoted from being THE ENTIRE metric to a weak
                           tiebreak. This is the fix for "an MTL with more
                           likes beats a real TL". log2 banding makes 412-vs-408
                           a tie while 1000-vs-5 still decides.
         8 recency         A later upload of the same chapter is usually a
                           redo/fix. Below upvotes because necro-uploads of bad
                           rips are common.
         9 pages           Usually more complete, but webtoon slicing makes it
                           near-noise (Solo Leveling ch.1 is 22p and 14p from
                           two groups, both complete), so it only breaks ties
                           nothing else could.
        10 -index          Deterministic last resort. Replaces the old
                           ACCIDENTAL versions[0] with a documented one.
        """
        sig = ctx["signals"][index]
        floor = ctx["page_floor"]
        pages = _coerce_pages(version)

        not_placeholder = 1
        if floor is not None and pages is not None:
            not_placeholder = int(pages >= floor)

        official_rank = 2 if sig["is_official"] else (0 if sig["all_inactive"] else 1)

        upvote_band = 0
        if ctx["upvotes_live"]:
            count = version.get("up_count")
            if isinstance(count, int) and count > 0:
                upvote_band = int(math.log2(count + 1))

        recency = (_coerce_epoch(version.get("uploaded")) or 0) if ctx["recency_live"] else 0
        pages_tier = pages if (ctx["pages_live"] and pages is not None) else 0

        return (
            0 if sig["excluded"] else 1,
            0 if version.get("_undownloadable") is True else 1,
            sig["mtl_rank"] if ctx["mtl_live"] else 2,
            not_placeholder,
            official_rank,
            sig["census_band"],
            1 if sig["verified"] else 0,
            upvote_band,
            recency,
            pages_tier,
            -index,
        )

    def select_best_chapter_version(
        self,
        versions: List[Dict],
        preferred_groups: List[str],
        mix_by_upvote: bool,
        allow_group_fallback: bool = True,
        log_debug_fn=None,
        *,
        selection_policy: Optional[GroupSelectionPolicy] = None,
    ) -> Optional[Dict]:
        """Collapse every version of ONE chapter number down to the best one.

        Called once per chapter-number bucket from aio-dl.py (grep
        `select_best_chapter_version`). NO handler overrides this — per-site
        behavior comes from what the handler puts in `_groups`, not from
        reimplementing the policy.

        Control flow is unchanged from the original: no --group → rank
        everything; --group + --mix-by-upvote → rank within the union of
        preferred groups; --group alone → first preferred group that has any
        version wins, ranked within it; nothing matched → fall back (or skip
        under --no-group-fallback). Only the METRIC changed, from
        `max(key=up_count)` to the composite `_rank_version` tuple.

        `selection_policy` carries the per-series census + MTL/exclude policy.
        Keyword-only with a default so the positional signature is unchanged.
        """
        if not versions:
            return None

        policy = selection_policy or GroupSelectionPolicy()

        def _debug(msg):
            if log_debug_fn:
                log_debug_fn(msg)

        # --mtl exclude: drop CONFIRMED machine translations outright. Never
        # `suspect` — hard-dropping real chapters on a heuristic is not a
        # trade the flag is allowed to make.
        pool = versions
        excluded_mtl = 0
        if policy.mtl == "exclude":
            probe_ctx = self._build_rank_context(versions, policy)
            keep = [
                v for i, v in enumerate(versions)
                if probe_ctx["signals"][i]["mtl_verdict"] != MTL_CONFIRMED
            ]
            excluded_mtl = len(versions) - len(keep)
            if not keep:
                _debug(
                    f"    Ch {versions[0].get('chap', '?')}: every version is "
                    f"machine-translated and --mtl exclude is set. Skipping."
                )
                return None
            pool = keep

        ctx = self._build_rank_context(pool, policy)
        chap_label = pool[0].get("chap", "?")
        # IDENTITY-keyed, not pool.index(): list.index compares by equality, so
        # two byte-identical duplicate rows would both resolve to the first
        # one's slot and corrupt the -index stable tiebreak.
        slot_of = {id(v): i for i, v in enumerate(pool)}

        def _available_groups() -> str:
            groups: List[str] = []
            for version in pool:
                group_name = self.get_group_name(version)
                if not isinstance(group_name, str):
                    continue
                cleaned = group_name.strip()
                if cleaned and cleaned not in groups:
                    groups.append(cleaned)
            return ", ".join(groups) if groups else "none"

        def _describe(version: Dict) -> str:
            """'Alpha' [201/201 ch, MTL] — the log's evidence line."""
            name = self.get_group_name(version) or "No Group"
            bits: List[str] = []
            sig = ctx["signals"][slot_of[id(version)]]
            census = policy.census or {}
            if census:
                best = 0
                for info in sig["infos"]:
                    key = self.get_group_match_key(info.name)
                    if key:
                        best = max(best, census.get(key, 0))
                if best:
                    bits.append(f"{best}/{policy.census_total} ch")
            if sig["mtl_verdict"] != MTL_NONE:
                bits.append(sig["mtl_verdict"] + " MTL")
            if sig["is_official"]:
                bits.append("official")
            if version.get("_undownloadable") is True:
                bits.append("no pages")
            return f"'{name}'" + (f" [{', '.join(bits)}]" if bits else "")

        def _pick(candidates: List[Dict]) -> Tuple[Dict, Optional[str]]:
            """Best candidate + the name of the tier that actually decided it."""
            ranked = sorted(
                ((self._rank_version(slot_of[id(v)], v, ctx), v) for v in candidates),
                key=lambda pair: pair[0],
                reverse=True,
            )
            winner_key, winner = ranked[0]
            why = None
            if len(ranked) > 1:
                runner_key = ranked[1][0]
                for tier, (a, b) in enumerate(zip(winner_key, runner_key)):
                    if a != b:
                        why = _RANK_TIER_NAMES[tier]
                        break
            return winner, why

        def _annotate_selection(
            version: Dict,
            *,
            selection_kind: str,
            requested_group: Optional[str] = None,
            why: Optional[str] = None,
        ) -> Dict:
            annotated = dict(version)
            # ONE reserved key replacing the three write-only ones
            # (_selection_kind / _requested_group / _available_groups) that
            # nothing ever read. Consumed by aio-dl.py's per-chapter log and
            # the --list-chapters JSON.
            annotated["_group_selection"] = {
                "kind": selection_kind,
                "requested": requested_group,
                "available": _available_groups(),
                "winner": self.get_group_name(version),
                "why": why,
                "excluded_mtl": excluded_mtl,
            }
            return annotated

        def _log_pick(prefix: str, winner: Dict, why: Optional[str]) -> None:
            losers = [v for v in pool if v is not winner]
            tail = ""
            if losers:
                shown = ", ".join(_describe(v) for v in losers[:3])
                more = f" +{len(losers) - 3} more" if len(losers) > 3 else ""
                tail = f" over {shown}{more}"
            reason = f" [on {why}]" if why else ""
            _debug(f"    Ch {chap_label}: {prefix}{_describe(winner)}{tail}{reason}.")

        if not preferred_groups:
            winner, why = _pick(pool)
            _log_pick("picked ", winner, why)
            return _annotate_selection(
                winner, selection_kind="ranked_no_group", why=why
            )

        preferred_entries = [
            (group_name, self.get_group_match_key(group_name))
            for group_name in preferred_groups
        ]
        preferred_entries = [
            (group_name, match_key)
            for group_name, match_key in preferred_entries
            if match_key
        ]
        if not preferred_entries:
            winner, why = _pick(pool)
            _log_pick("group filter had no usable names; picked ", winner, why)
            return _annotate_selection(
                winner, selection_kind="ranked_invalid_group_filter", why=why
            )

        preferred_keys = {match_key for _, match_key in preferred_entries}
        if mix_by_upvote:
            # Historical flag name. Since upvotes were demoted to a weak
            # tiebreak this means "rank across the union of my preferred
            # groups" rather than "walk them in priority order".
            preferred = [
                v for v in pool
                if any(
                    self.group_matches_filter(self.get_group_infos(v), key)
                    for key in preferred_keys
                )
            ]
            if preferred:
                winner, why = _pick(preferred)
                _log_pick("mixed across preferred groups; picked ", winner, why)
                return _annotate_selection(
                    winner, selection_kind="preferred_mix_ranked", why=why
                )
            if not allow_group_fallback:
                _debug(
                    f"    Ch {chap_label}: none of the requested groups were "
                    f"present. Skipping. Available: {_available_groups()}."
                )
                return None
            winner, why = _pick(pool)
            _log_pick("no requested group present; fell back to ", winner, why)
            return _annotate_selection(
                winner, selection_kind="fallback_missing_group", why=why
            )

        for group_name, match_key in preferred_entries:
            candidates = [
                v for v in pool
                if self.group_matches_filter(self.get_group_infos(v), match_key)
            ]
            if candidates:
                winner, why = _pick(candidates)
                _log_pick(
                    f"priority group '{group_name}'; picked ", winner, why
                )
                return _annotate_selection(
                    winner,
                    selection_kind="preferred_priority",
                    requested_group=group_name,
                    why=why,
                )
        if not allow_group_fallback:
            _debug(
                f"    Ch {chap_label}: none of the requested groups were "
                f"present. Skipping. Available: {_available_groups()}."
            )
            return None
        winner, why = _pick(pool)
        _log_pick("no requested group present; fell back to ", winner, why)
        return _annotate_selection(
            winner, selection_kind="fallback_missing_group", why=why
        )

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        raise NotImplementedError

    # --- Cross-site search -------------------------------------------------
    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        """Search this site for comics matching `query`.

        Default no-op so unimplemented handlers self-select out of the
        orchestrator (sites/search_orchestrator.py). The orchestrator filters
        via sites.iter_search_capable_handlers(), which compares the bound
        method against this base no-op — so handlers must override this method
        on the class (not assign it post-init) to be picked up.

        Implementations should:
          - Let HTTP errors (5xx, connection errors, timeouts) propagate. The
            orchestrator's _run_one catches them and records the host in the
            probe-failure cache so the next search skips that host. Swallowing
            them here returns [] which looks identical to "no match for query"
            and leaves the cache empty — meaning every search keeps eating
            time on the dead host. Wrap *parsing* in try/except (returning []
            on malformed HTML is fine), not the HTTP call.
          - Use the provided make_request callable so retries, cooldowns, and
            cross-process rate-limit coordination flow through automatically.
          - Cap at `limit` hits to keep the merge step bounded.
        """
        return []

    @staticmethod
    def _pick_representative_chapters(
        chapters: List[Dict], n: int = 8,
    ) -> "List[Tuple[int, Dict]]":
        """Pick up to N chapters spread across the series for breadth sampling.

        Returns a list of `(absolute_index_in_chapters_list, chapter_dict)`
        tuples — the absolute index is needed by
        _pick_random_middle_page_index so the deterministic seed is stable
        across runs (cache replay relies on this).

        Strategy:
          - Skip first 1 + last 1 chapter (chapter 1 often differs in cover-
            page treatment; latest chapter may be partial / TBA placeholder).
          - From the trimmed pool, prefer whole-numbered chapters (skipping
            omake .5/.1 fragments that have atypical page counts).
          - Pick N evenly-spaced chapters from the preferred pool; if there
            are fewer than N whole-numbered chapters, fill the remainder
            from the full pool to hit N total.

        Degenerate cases:
          - Empty list → []
          - 1 chapter (oneshot) → [(0, that_chapter)]
          - 2-3 chapters → returns the chapter(s) after skipping first/last
            (down to 1 if all that's left)
          - Long series → up to N indices spread evenly

        Cross-file: called by _probe_chapter_aggregate (this file, post-Phase-2
        rewrite) and by calibrate_quality_probe.py via the same delegation.
        The v5 sampling strategy is documented in
        ~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 2 section).
        """
        if not chapters:
            return []
        total = len(chapters)
        # Oneshot — only one chapter, return it (skipping first/last would
        # leave nothing, defeating the probe).
        if total == 1:
            return [(0, chapters[0])]
        # Trim first and last 1 chapter when there's room. For total in
        # [2, 3], trimming both would leave 0-1 chapters; trim only as much
        # as keeps the pool non-empty.
        if total >= 4:
            trim_start, trim_end = 1, 1
        elif total == 3:
            trim_start, trim_end = 1, 0  # keep middle + last
        else:  # total == 2
            trim_start, trim_end = 0, 0  # keep both
        trimmed = list(enumerate(chapters))[trim_start: total - trim_end]
        if not trimmed:
            # Defensive (shouldn't hit given the conditions above).
            trimmed = list(enumerate(chapters))

        # Preferred pool: whole-numbered chapters only.
        whole_pool: List[Tuple[int, Dict]] = []
        for abs_idx, ch in trimmed:
            chap = ch.get("chap")
            if chap is None:
                continue
            try:
                f = float(chap)
            except (TypeError, ValueError):
                continue
            if f == int(f):
                whole_pool.append((abs_idx, ch))

        # If the whole-numbered pool meets the budget, sample evenly from it.
        # Otherwise fill from the full trimmed pool — partial chapters are
        # better than no sample.
        primary_pool = whole_pool if whole_pool else trimmed
        if len(primary_pool) <= n:
            base_picks = list(primary_pool)
        else:
            # Evenly-spaced sampling: step = len/N, take indices at
            # step/2, step*3/2, step*5/2, ... so we hit the middle of each
            # bucket (avoids over-weighting endpoints which trim already
            # handled, but bucketing center is still cleaner than edge picks).
            step = len(primary_pool) / n
            picks_idx = [min(int(step * (i + 0.5)), len(primary_pool) - 1) for i in range(n)]
            base_picks = [primary_pool[i] for i in picks_idx]

        # Top up from the full trimmed pool if we under-shot N (only when
        # whole_pool was the smaller primary pool).
        if len(base_picks) < n and primary_pool is whole_pool:
            seen = {abs_idx for abs_idx, _ in base_picks}
            for abs_idx, ch in trimmed:
                if abs_idx in seen:
                    continue
                base_picks.append((abs_idx, ch))
                seen.add(abs_idx)
                if len(base_picks) >= n:
                    break

        # Sort by absolute index for predictable ordering (matches v4's
        # chapter-list traversal direction so cache keys stay stable when
        # the sampler is re-run).
        base_picks.sort(key=lambda t: t[0])
        return base_picks[:n]

    @staticmethod
    def _pick_random_middle_page_index(
        n_pages: int, series_url: str, chapter_index: int,
        chapter: "Optional[Dict]" = None,
    ) -> "Optional[int]":
        """Deterministically pick a page index from the middle 50% of a chapter.

        Returns an int in [n_pages//4, 3*n_pages//4) for non-trivial chapters
        and a safe-middle for very short ones. The seed comes from
        SHA-1((series_url, stable_chapter_key)) where stable_chapter_key
        prefers identifiers that DON'T shift when the chapter list grows:
          1. ``chapter["url"]``    — the chapter URL is the most stable
                                     identifier upstream produces.
          2. ``chapter["chap"]``   — the chapter number (string-coerced
                                     so "47" == 47).
          3. ``chapter["hid"]``    — handler-specific hash ID where present.
          4. ``chapter["id"]``     — MangaDex-style UUID where present.
          5. ``str(chapter_index)``— positional fallback. PRE-v8 behavior;
                                     drifts on list growth but kept so
                                     callers that don't pass ``chapter``
                                     (legacy + tests) work unchanged.

        Why this matters: the previous seed `f"{series_url}:{chapter_index}"`
        keyed on the absolute position in the chapter list. When the
        publisher adds a new chapter (typically prepended → newest-first),
        every existing chapter's absolute index shifts by 1, every SHA-1
        seed changes, and cache replays fetch a DIFFERENT middle page than
        the original probe — defeating the "stable across sessions" claim.
        Keying on an intrinsic identifier preserves the invariant.

        Why stratify to middle 50%: the first quarter of a chapter often
        has cover/title splashes that compress better than typical content;
        the last quarter has credits / translator notes / promo pages that
        aren't representative. The middle is where the actual story content
        lives.

        Returns None when n_pages <= 0 (no pages to pick from).

        Cross-file: consumed by _probe_chapter_aggregate (this file). The
        same function is called from T3 pairwise + paired-comparison in
        sites/search_orchestrator.py — those call sites already pass the
        chapter NUMBER (int(chap_num)) as ``chapter_index`` rather than a
        positional list index, so they already get stability-by-number;
        the ``chapter`` kwarg just adds another preference layer.
        """
        if n_pages <= 0:
            return None
        if n_pages <= 4:
            return n_pages // 2  # very short chapter — just take the middle
        import hashlib
        stable_key: "Optional[str]" = None
        if isinstance(chapter, dict):
            for field in ("url", "chap", "hid", "id"):
                val = chapter.get(field)
                if val is not None and val != "":
                    stable_key = str(val)
                    break
        if stable_key is None:
            stable_key = str(chapter_index)
        seed_input = f"{series_url}:{stable_key}".encode("utf-8")
        digest = hashlib.sha1(seed_input).hexdigest()
        seed_int = int(digest[:8], 16)
        low = n_pages // 4
        high = (3 * n_pages) // 4  # exclusive
        if high <= low:
            return n_pages // 2
        span = high - low
        return low + (seed_int % span)

    @staticmethod
    def _pick_sample_indices(n: int) -> List[int]:
        """Pick up to 5 evenly-spaced page indices from a 0..n-1 image list.

        Targets: start, start-middle, middle, middle-last, last. For short
        chapters (<5 pages) returns all available indices. Dedupes for
        small N (e.g. n=3 → [0,1,2]).

        The 5-point spread is the multi-page probe's core idea: a single-
        sample probe can't tell a healthy site from one whose CDN throttles
        after the first request. By sampling 5 across the chapter we surface
        per-page failures (treated as 0 in the aggregate) — a 1-of-5 site
        scores ~20% of its peak quality, accurately reflecting "this CDN
        can't reliably serve a chapter".
        """
        if n <= 0:
            return []
        if n >= 5:
            raw = [0, n // 4, n // 2, (3 * n) // 4, n - 1]
        else:
            raw = list(range(n))
        return sorted(set(raw))

    def _fetch_probe_item_bytes_ex(self, item, scraper) -> Tuple[Optional[bytes], bool]:
        """Fetch image bytes for a single probe item, reporting timeout-vs-not.

        Returns ``(bytes_or_None, timed_out)``. ``timed_out`` is True ONLY when a
        network timeout (``_PROBE_TIMEOUT_EXCEPTIONS``) was raised — the caller
        EXCLUDES those from the quality aggregate (slowness, not a quality
        signal). A definitive failure (non-image dict, HTTP >= 400, non-image
        bytes, or any non-timeout exception) returns ``(None, False)`` so it
        still scores as a genuine 0.0.

        Handles both `get_chapter_images` return shapes:
          - `str` (most handlers): URL — fetched via scraper.get
          - `dict` with type=binary_image (MangaReader): pre-fetched bytes
        """
        if isinstance(item, dict):
            if item.get("type") == "binary_image":
                blob = item.get("data")
                if isinstance(blob, (bytes, bytearray)) and looks_like_real_image(blob):
                    return bytes(blob), False
            return None, False
        if isinstance(item, str) and item:
            try:
                response = scraper.get(item, timeout=15)
                if response.status_code >= 400:
                    return None, False
                data = response.content  # can itself time out on a chunked read
                if not looks_like_real_image(data):
                    return None, False
                return data, False
            except _PROBE_TIMEOUT_EXCEPTIONS:
                return None, True
            except Exception:
                return None, False
        return None, False

    def _fetch_probe_item_bytes(self, item, scraper) -> Optional[bytes]:
        """Bytes-only wrapper over _fetch_probe_item_bytes_ex for callers that
        don't need to know WHY a fetch failed (the throttle tail). Keeps the
        tail's cdn_reliability accounting unchanged."""
        return self._fetch_probe_item_bytes_ex(item, scraper)[0]

    def _probe_one_pick(
        self, abs_idx, chapter, hit, scraper, make_request, score_fn,
    ) -> "_ProbeSample":
        """Probe ONE breadth-sample chapter -> a _ProbeSample.

        Extracted VERBATIM from _probe_chapter_aggregate's former serial loop
        body so the serial and concurrent breadth paths share ONE implementation
        (any divergence would break the byte-identity guarantee). The four
        failure branches reproduce the exact field combinations the serial loop
        wrote: a failure keeps whatever it had computed so far (image_items once
        fetched, picked_page_idx once chosen, blob once downloaded) so the
        re-score pass + throttle tail see the same state. ``metadata`` is
        non-None ONLY on a fully scored page (the compacted-metas discriminator).

        ``score_fn`` is passed in rather than importing _score_image_blob here so
        this method stays clear of the search_orchestrator circular-import dance
        the caller already does at its late-import site (grep _score_image_blob).
        Must never raise: get_chapter_images failures are swallowed; the caller's
        worker wraps this in a further guard for anything unforeseen.
        """
        try:
            image_items = self.get_chapter_images(chapter, scraper, make_request)
        except _PROBE_TIMEOUT_EXCEPTIONS:
            # Slow/timeout, not a content failure — mark not-measured so the
            # aggregate excludes it instead of scoring a broken-CDN 0.0.
            return _ProbeSample(0.0, None, None, None, None, is_timeout=True)
        except Exception:
            image_items = None
        if not image_items:
            return _ProbeSample(0.0, None, None, None, None)
        page_idx = self._pick_random_middle_page_index(
            len(image_items), hit.url, abs_idx, chapter=chapter,
        )
        if page_idx is None:
            return _ProbeSample(0.0, None, image_items, None, None)
        blob, timed_out = self._fetch_probe_item_bytes_ex(image_items[page_idx], scraper)
        if not blob:
            return _ProbeSample(0.0, None, image_items, page_idx, None, is_timeout=timed_out)
        result = score_fn(blob)
        if result is None:
            # keep blob in case re-score works (mirrors the serial base path)
            return _ProbeSample(0.0, None, image_items, page_idx, blob)
        score, metadata = result
        return _ProbeSample(score, metadata, image_items, page_idx, blob)

    # Throttle-probe tail constants. The throttle-probe tail re-fetches up to
    # N additional pages from the highest-scoring chapter to compute a
    # cdn_reliability ratio. This is the v5 mitigation for the lost throttle-
    # detection signal that came free with the v4 "5 pages × 1 chapter"
    # probe. With breadth sampling we now do "1 page × 8 chapters" which
    # spreads samples across the series (better statistical signal) but only
    # tests the CDN once per chapter — a CDN that throttles after the first
    # request per chapter still serves every breadth sample. The throttle-
    # probe tail catches that by sequentially fetching THROTTLE_TAIL_PAGES
    # additional pages from one chapter, mimicking single-worker download
    # behavior. Stored in metadata as cdn_reliability (succeeded / attempted);
    # NOT folded into the composite score directly so a sleeping CDN doesn't
    # demote an otherwise quality source. Rizzcomic's handler override at
    # sites/rizzcomic.py consumes this field to bottom-out the composite
    # when a CDN is poisoned (cdn_reliability == 0).
    THROTTLE_TAIL_PAGES = 3

    # Per-SOURCE wall-clock budget for one _probe_chapter_aggregate call
    # (2026-07-12). Found live on the Frieren benchmark: a host whose chapter
    # pages TIME OUT (rather than fail fast) costs ~40-55s per sampled
    # chapter under the search request shim (2 attempts × 20s + a 15s page
    # fetch), so a full 8-chapter breadth probe needs >240s — it held the
    # orchestrator's ENTIRE probe phase to its PROBE_PHASE_DEADLINE_S on
    # every single search, and because the abandoned worker died with the
    # process, its result NEVER cached, so the 240s was re-paid every run
    # (mangakatana was the live culprit; the pre-2026-07-12 pipeline had the
    # same flaw, just masked by everything else being slow too). When the
    # budget expires mid-sampling, the REMAINING chapters count as failures
    # (0.0 — the same "honest broken-CDN" contract failed fetches already
    # use: a site that can't serve chapter pages promptly IS degraded) and
    # the partial aggregate returns + caches, so the cost is paid once per
    # TTL instead of once per search. 120s = half the phase deadline; a
    # healthy source's 8 samples finish in well under 30s. Not applied to
    # comix's override (its own browser time budgets bound it).
    PROBE_SOURCE_BUDGET_S = 120.0

    # Breadth-sample concurrency for _probe_chapter_aggregate (module default
    # _PROBE_BREADTH_CONCURRENCY, env AIO_PROBE_BREADTH_CONCURRENCY). 1 = serial.
    # A subclass whose per-chapter fetch must not run concurrently (e.g. a
    # browser handler) overrides this to 1. See the module-level constant's
    # comment for the full rationale + the byte-identity guarantee.
    PROBE_BREADTH_CONCURRENCY: int = _PROBE_BREADTH_CONCURRENCY

    def _probe_chapter_aggregate(
        self, hit: "SearchHit", scraper, make_request,
        max_samples: "Optional[int]" = None,
        fetch_memo=None,
    ) -> "Optional[tuple]":
        """Breadth-sampled chapter probe — fetches 1 page from each of 8
        chapters spread across the series, plus a throttle-probe tail.

        ``max_samples`` (v5 semantics, BREAKING from v4): when set, clamps
        the probe to that many CHAPTERS (was: that many pages within 1
        chapter). The orchestrator passes ``max_samples=2`` for low-title-
        match results on EXPENSIVE_PROBE handlers (browser-driven ones) per
        the Phase 5 quick-probe clamp. None (default) probes 8 chapters.

        Why breadth instead of depth: research (~/.claude/plans/how-robust-
        is-the-memoized-koala-agent-a42650755ce151e5a.md) showed that
        between-chapter variance (different scanners / dates / encoder
        settings) dwarfs within-chapter variance. Sampling 1 page across
        8 chapters is statistically a much better estimator of "site
        quality" than 5 pages of 1 chapter. The user's request was the
        trigger: "We take 5 images from 5 different chapters and give an
        average rating" — their description was inaccurate (we sampled
        1 chapter, not 5) but the underlying intent was right.

        Throttle-probe tail: after the 8 breadth samples, pick the chapter
        whose page scored highest, and sequentially fetch up to N additional
        pages from it (THROTTLE_TAIL_PAGES, default 3). The
        succeeded/attempted ratio becomes ``metadata["cdn_reliability"]``.
        This preserves the v4 throttle-detection signal (rizzchoros.cloud
        poisoning case from 2026-05-07) without polluting the composite —
        a sleeping CDN that revives mid-probe shouldn't crater the score
        of an otherwise high-quality source.

        Returns (aggregate_score, metadata) or None if every chapter failed
        (orchestrator falls back to cover probe). When all 8 chapters
        produce zero successful image fetches but the chapter list itself
        was readable, returns (0.0, samples=0/8 metadata) directly — same
        v4 "honest broken-CDN" semantics, just at chapter-granularity.

        Cross-file: scoring delegated to
        sites.search_orchestrator._score_image_blob via late import; the
        deterministic page-picker comes from _pick_random_middle_page_index
        (this file) so cache replays stay stable.
        """
        # Late import to avoid module-level circular dep with search_orchestrator.
        from .search_orchestrator import _score_image_blob

        if not hit or not hit.url:
            return None
        # Context + chapter list, optionally through the per-run FetchMemo
        # (sites/fetch_memo.py, 2026-07-12). The probe is the FIRST of up to
        # three phases (probe → T3 pairwise → winner chapter fetch) that all
        # need this source's context + chapter list; routing through the
        # memo lets the later phases reuse this fetch instead of re-hitting
        # the site. Failure semantics identical to the direct path: any
        # exception or empty list → None (orchestrator falls back to the
        # cover probe), and the memo stores nothing on failure (no negative
        # caching — the winner fetch retries with its own policy).
        if fetch_memo is not None:
            try:
                chapters = fetch_memo.get_chapters(
                    self, hit.url, "en", scraper, make_request,
                )
            except Exception:
                return None
        else:
            try:
                context = self.fetch_comic_context(hit.url, scraper, make_request)
            except Exception:
                return None
            if context is None:
                return None
            try:
                chapters = self.get_chapters(context, scraper, "en", make_request)
            except Exception:
                return None
        if not chapters:
            return None


        # v5 breadth sampling: pick N chapters (default 8, or max_samples
        # when caller clamps for quick-probe). _pick_representative_chapters
        # returns (absolute_index, chapter_dict) tuples; the absolute index
        # feeds _pick_random_middle_page_index's deterministic seed so cache
        # replays pick the same page.
        n_chapters = max_samples if (max_samples is not None and max_samples >= 1) else 8
        chapter_picks = self._pick_representative_chapters(chapters, n=n_chapters)
        if not chapter_picks:
            return None

        # Each chapter contributes at most 1 page; failures (get_chapter_images
        # raised, empty image list, image fetch failed, unscoreable bytes) count
        # as 0.0 — the "honest broken-CDN" contract, at chapter-granularity.
        #
        # v5.1: pass 1 scores with content_type="unknown" (== bw_manga
        # weights, the v5 default). We need the per-page metadata to
        # classify the series, but classification requires aggregating
        # across all pages — chicken-and-egg. The pragmatic resolution:
        # score once with "unknown", classify from aggregated metadata,
        # then re-score iff the classification produced a non-trivial
        # content_type (re-scoring with "unknown" would be a no-op). The
        # re-score path keeps the cached blob in `per_chapter_blobs` so
        # we don't re-fetch from the CDN.
        # Breadth pass: probe each picked chapter's single sample page. Formerly
        # a serial for-loop; now a bounded daemon pool (PROBE_BREADTH_CONCURRENCY)
        # so an HTML site paying a full origin GET per sample isn't N-times
        # serial. BYTE-IDENTICAL to serial: the (chapter, page) set is fixed
        # before any I/O by the pure pickers above, and the aggregation below is
        # order-independent. Each worker owns a distinct PREALLOCATED slot
        # (list[i]=x is atomic under the GIL) so there are no append races; the
        # sweep afterward rebuilds the four index-aligned lists + the compacted
        # per_chapter_metas in slot (= pick) order, matching the old append
        # semantics exactly.
        #
        # Per-source budget (see PROBE_SOURCE_BUDGET_S): probe_deadline gates
        # BOTH task start (a worker that pulls a pick past the deadline records a
        # failure without fetching — the concurrent analogue of the old
        # per-iteration skip) AND the wall-clock join (a worker blocked in a hung
        # GET can't stall the source past the budget). Any slot still None after
        # the join is a miss (never pulled / gated / in-flight at timeout) and is
        # swept to a 0.0 failure — the same "honest broken-CDN" contract. Daemon
        # pattern mirrors the orchestrator's probe-phase pool (grep _worker_loop
        # in search_orchestrator.py).
        probe_deadline = time.monotonic() + self.PROBE_SOURCE_BUDGET_S
        n_workers = max(
            1, min(int(self.PROBE_BREADTH_CONCURRENCY or 1), len(chapter_picks))
        )
        sample_results: List[Optional[_ProbeSample]] = [None] * len(chapter_picks)

        if n_workers <= 1:
            # Serial path: a single pick (max_samples=1) or a subclass forcing
            # concurrency 1. No thread/queue overhead; identical to the old loop.
            for slot, (abs_idx, chapter) in enumerate(chapter_picks):
                if time.monotonic() > probe_deadline:
                    break  # remaining slots stay None -> swept to failures
                sample_results[slot] = self._probe_one_pick(
                    abs_idx, chapter, hit, scraper, make_request, _score_image_blob,
                )
        else:
            work_q: "queue.Queue" = queue.Queue()
            for slot, (abs_idx, chapter) in enumerate(chapter_picks):
                work_q.put((slot, abs_idx, chapter))

            def _breadth_worker() -> None:
                while True:
                    try:
                        slot, abs_idx, chapter = work_q.get_nowait()
                    except queue.Empty:
                        return
                    if time.monotonic() > probe_deadline:
                        return  # task-start gate: don't begin a fetch past budget
                    try:
                        sample_results[slot] = self._probe_one_pick(
                            abs_idx, chapter, hit, scraper, make_request,
                            _score_image_blob,
                        )
                    except Exception:
                        # Belt-and-suspenders: a worker must never propagate and
                        # kill the pool (_probe_one_pick already swallows fetch
                        # errors; this covers anything unforeseen).
                        sample_results[slot] = _ProbeSample(0.0, None, None, None, None)

            workers = [
                threading.Thread(
                    target=_breadth_worker, name="probe-breadth", daemon=True,
                )
                for _ in range(n_workers)
            ]
            for t in workers:
                t.start()
            for t in workers:
                remaining = probe_deadline - time.monotonic()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)

        # Sweep in slot (= pick) order. A surviving None = a budget miss ->
        # recorded as a 0.0 failure. Rebuild the four index-aligned lists (one
        # entry per pick) + the compacted per_chapter_metas (successes only, in
        # pick order) so every downstream consumer (re-score stitch, throttle
        # tail, chapter_indices_sampled) is unchanged.
        budget_exhausted = any(r is None for r in sample_results)
        per_chapter_scores: List[float] = []
        per_chapter_metas: List[Dict] = []
        per_chapter_image_lists: List[Optional[List]] = []  # for throttle tail
        per_chapter_picked_page_idx: List[Optional[int]] = []
        per_chapter_blobs: List[Optional[bytes]] = []  # v5.1: kept for re-score pass
        # Pick-aligned "was this sample NOT measured (timeout / budget-miss)?"
        # flags. A surviving None slot = a budget miss = a timeout (never
        # fetched). Timeouts keep a 0.0 in per_chapter_scores for POSITIONAL
        # alignment (the throttle tail + re-score pass index into it by slot),
        # but are excluded from the score AGGREGATE below so a slow site isn't
        # penalized as broken. Grep per_chapter_is_timeout.
        per_chapter_is_timeout: List[bool] = []
        for r in sample_results:
            if r is None:
                r = _ProbeSample(0.0, None, None, None, None, is_timeout=True)
            per_chapter_scores.append(r.score)
            per_chapter_is_timeout.append(r.is_timeout)
            per_chapter_image_lists.append(r.image_items)
            per_chapter_picked_page_idx.append(r.picked_page_idx)
            per_chapter_blobs.append(r.blob)
            if r.metadata is not None:
                per_chapter_metas.append(r.metadata)

        # v5.1: classify series content_type from successful pages' metadata.
        # The classifier reads width / height / aspect / is_grayscale /
        # chroma_var — all are fields _score_image_blob already populates.
        # When content_type != "unknown" AND differs from the default
        # bw_manga weights, re-score each blob with the classified
        # content_type so the final T1 reflects per-content-type tuning.
        series_content_type = "unknown"
        if per_chapter_metas:
            try:
                from .search_orchestrator import _classify_series_content
                feature_view = [
                    {
                        "width": m.get("width", 0),
                        "height": m.get("height", 0),
                        "aspect": (m.get("width", 0) / m["height"]) if m.get("height") else 1.0,
                        "is_grayscale_page": bool(m.get("is_grayscale", False)),
                        "chroma_var": float(m.get("chroma_var", 0.0)),
                    }
                    for m in per_chapter_metas
                ]
                series_content_type = _classify_series_content(feature_view)
            except Exception:
                series_content_type = "unknown"

            # Re-score with the classified content_type only when it would
            # change weights/targets. "unknown" maps to bw_manga defaults
            # so we skip the re-score work in that branch (a no-op).
            if series_content_type not in ("unknown", "bw_manga"):
                rescored_scores: List[float] = []
                rescored_metas: List[Dict] = []
                rescore_idx = 0
                for chapter_idx, score in enumerate(per_chapter_scores):
                    if score <= 0.0:
                        # Failure stays a failure regardless of content_type.
                        continue
                    blob = per_chapter_blobs[chapter_idx]
                    if blob is None:
                        # Defensive: success score but no blob is impossible.
                        rescored_scores.append(score)
                        rescored_metas.append(per_chapter_metas[rescore_idx])
                        rescore_idx += 1
                        continue
                    new_result = _score_image_blob(
                        blob, content_type=series_content_type,
                    )
                    if new_result is None:
                        rescored_scores.append(score)
                        rescored_metas.append(per_chapter_metas[rescore_idx])
                    else:
                        new_score, new_meta = new_result
                        rescored_scores.append(new_score)
                        rescored_metas.append(new_meta)
                    rescore_idx += 1
                # Stitch the rescored successes back into per_chapter_scores
                # while preserving alignment with per_chapter_image_lists and
                # per_chapter_picked_page_idx (which track ALL chapters
                # including failed ones).
                new_per_chapter_scores: List[float] = []
                rescored_iter = iter(rescored_scores)
                for old_score in per_chapter_scores:
                    if old_score <= 0.0:
                        new_per_chapter_scores.append(old_score)
                    else:
                        try:
                            new_per_chapter_scores.append(next(rescored_iter))
                        except StopIteration:
                            new_per_chapter_scores.append(old_score)
                per_chapter_scores = new_per_chapter_scores
                per_chapter_metas = rescored_metas

        # All chapters produced 0 — site served chapter lists but no real
        # pages. v4-equivalent canonical broken-CDN signal at chapter
        # granularity. Returning 0.0 (not None) so the orchestrator records
        # the measured failure rather than camouflaging it via cover-probe
        # fallback (rizzchoros.cloud lesson from 2026-05-07).
        if not per_chapter_metas:
            # No chapter yielded a scoreable page. Distinguish "couldn't measure
            # anything IN TIME" from "measured, and it's broken":
            if all(per_chapter_is_timeout):
                # Every pick timed out / was budget-missed — we measured NOTHING.
                # Don't fabricate a 0.0 "broken" verdict for a merely-slow site;
                # return None so the orchestrator falls back to the cover probe /
                # seed prior (quality_basis becomes "seed"/"cover" → red triangle).
                return None
            # At least one GENUINE failure (empty list / 4xx / non-image /
            # unscoreable) and zero successes → honest broken-CDN 0.0, unchanged
            # (rizzchoros contract; rizzcomic's None→0.0 override relies on this).
            return 0.0, {
                "width": 0,
                "height": 0,
                "format": "FAILED",
                "size_bytes": 0,
                "samples_attempted": len(chapter_picks),
                "samples_succeeded": 0,
                "samples_measured": sum(1 for to in per_chapter_is_timeout if not to),
                "samples_timed_out": sum(1 for to in per_chapter_is_timeout if to),
                "cdn_reliability": 0.0,
                "probe_budget_exhausted": budget_exhausted,
            }

        # Hybrid median/mean aggregation across chapters (was: across pages
        # within one chapter), computed over MEASURED samples only (successes +
        # genuine failures). v5.2: timeouts / budget-misses are EXCLUDED, so a
        # slow site is scored on what we could actually fetch, not penalized for
        # pages we never reached. measured_scores is guaranteed non-empty here
        # (per_chapter_metas is non-empty ⇒ ≥1 success). Same hybrid rule as v4:
        # median when every measured sample succeeded (suppresses cross-chapter
        # content variance — e.g. a color splash chapter vs a B&W one), mean when
        # any measured sample was a GENUINE failure (preserves the throttle/
        # failure signal). Grep per_chapter_is_timeout.
        measured_scores = [
            s for s, to in zip(per_chapter_scores, per_chapter_is_timeout) if not to
        ]
        if all(s > 0.0 for s in measured_scores):
            aggregate_score = statistics.median(measured_scores)
        else:
            aggregate_score = sum(measured_scores) / len(measured_scores)

        # Throttle-probe tail: pick the highest-scoring chapter index from
        # the breadth pass and sequentially fetch THROTTLE_TAIL_PAGES more
        # pages from it. This re-introduces the v4 sequential-throttle
        # detection signal that the breadth pass loses (1 page per chapter
        # only tests the CDN's first-request behavior).
        #
        # Gate (v8): skip the entire tail when the caller clamped
        # ``max_samples`` (non-top candidates / quick-probe path in the
        # orchestrator). The tail costs THROTTLE_TAIL_PAGES extra image
        # GETs which dominates the per-source budget for clamped probes:
        # max_samples=1 was supposed to cost 1 GET; the unconditional
        # tail made it cost 4. For the orchestrator's PROBE_PHASE_DEADLINE_S
        # budget that's a 4x overrun, so clamped probes were getting
        # guillotined before completing on slow / CF-protected sites.
        # The tail's value (sequential throttle detection) needs a
        # representative breadth pass to be meaningful — there's no point
        # measuring CDN-reliability on a source where we already gave up
        # on breadth.
        cdn_reliability: Optional[float] = None
        tail_attempted = 0
        tail_succeeded = 0
        # Tail also respects the per-source budget: its 3 sequential fetches
        # cost up to ~45s against a timing-out host, and a budget-exhausted
        # breadth pass already established the degradation signal.
        if (
            per_chapter_scores and max_samples is None
            and time.monotonic() <= probe_deadline
        ):
            best_chapter_local_idx = max(
                range(len(per_chapter_scores)),
                key=lambda i: per_chapter_scores[i],
            )
            image_items = per_chapter_image_lists[best_chapter_local_idx]
            picked_page_idx = per_chapter_picked_page_idx[best_chapter_local_idx]
            if image_items and picked_page_idx is not None:
                n_pages = len(image_items)
                # Walk forward from the already-fetched page. Dedup against
                # both the picked page AND prior candidates: on short
                # chapters (n_pages <= THROTTLE_TAIL_PAGES) the wrap puts
                # us on the same page twice, and the previous `!=` filter
                # left the duplicates in place — tail_attempted counted
                # repeats while only one distinct page was actually probed,
                # so cdn_reliability mis-reported sequential CDN behavior
                # (re-fetched URL hits the CDN cache from attempt 1 on the
                # success side, or returns the same 5xx on the failure
                # side). Order-preserving set dedup keeps the walk-forward
                # ordering while guaranteeing each page is fetched at most
                # once.
                seen_tail_pages: set = {picked_page_idx}
                candidate_pages: List[int] = []
                for i in range(self.THROTTLE_TAIL_PAGES):
                    p = (picked_page_idx + 1 + i) % n_pages
                    if p in seen_tail_pages:
                        continue
                    seen_tail_pages.add(p)
                    candidate_pages.append(p)
                for p_idx in candidate_pages:
                    tail_attempted += 1
                    blob = self._fetch_probe_item_bytes(image_items[p_idx], scraper)
                    if blob:
                        tail_succeeded += 1
                if tail_attempted > 0:
                    cdn_reliability = tail_succeeded / tail_attempted

        # Metadata aggregation: mean across SUCCESSFUL samples for numeric
        # fields; majority vote for booleans; most-common for categorical.
        # The per-sample metadata schema is now the v5 _compute_t1_score
        # output (see sites/search_orchestrator.py:_compute_t1_score) with
        # many more fields than v4's 4-field schema. We aggregate every
        # numeric field we recognize; unknown fields pass through from the
        # first sample only (forward-compat — new component additions in
        # _compute_t1_score don't require updates here).
        from collections import Counter

        def _mean_field(field: str) -> Optional[float]:
            vals = [m.get(field) for m in per_chapter_metas]
            vals = [float(v) for v in vals if isinstance(v, (int, float))]
            if not vals:
                return None
            return round(sum(vals) / len(vals), 4)

        avg_w = sum(int(m.get("width", 0) or 0) for m in per_chapter_metas) // len(per_chapter_metas)
        avg_h = sum(int(m.get("height", 0) or 0) for m in per_chapter_metas) // len(per_chapter_metas)
        avg_size = sum(int(m.get("size_bytes", 0) or 0) for m in per_chapter_metas) // len(per_chapter_metas)
        fmts = [m.get("format", "UNKNOWN") for m in per_chapter_metas]
        most_common_fmt = Counter(fmts).most_common(1)[0][0] if fmts else "UNKNOWN"

        gs_count = sum(1 for m in per_chapter_metas if m.get("is_grayscale"))
        lossless_count = sum(1 for m in per_chapter_metas if m.get("is_lossless"))
        # v5.1: outlier aggregation uses majority vote (≥half of probed
        # pages must share the same outlier type for the aggregate to
        # inherit it). The v5 "first-found wins" rule was set when there
        # was only one source-level outlier type (webp_below_floor). v5.1
        # added per-page outliers (low_chroma, fake_sharpened,
        # heavy_watermark) that legitimately VARY across pages — e.g.
        # linewebtoon serves mostly PNG but a stray JPEG proxy thumbnail
        # would mark the whole source as low_chroma under first-found.
        # Majority vote also preserves the rizzchoros throttle_detected
        # signal (which fires uniformly across all pages when the CDN is
        # broken).
        outlier_counts = Counter(
            m.get("outlier") for m in per_chapter_metas if m.get("outlier")
        )
        majority_threshold = max(1, (len(per_chapter_metas) + 1) // 2)
        majority_outlier = next(
            (name for name, cnt in outlier_counts.most_common()
             if cnt >= majority_threshold),
            None,
        )

        aggregate_metadata: Dict[str, Any] = {
            "width": int(avg_w),
            "height": int(avg_h),
            "format": most_common_fmt,
            "size_bytes": int(avg_size),
            "samples_attempted": len(chapter_picks),
            "samples_succeeded": len(per_chapter_metas),
            # v5.2 (2026-07-14): samples that could not be MEASURED in time
            # (network timeout or budget-miss) — EXCLUDED from the score
            # aggregate above (slow != broken). samples_measured = attempted -
            # timed_out = successes + genuine failures. Drives the UI partial-
            # probe Clock hint (SearchSourceCard.jsx keys on samples_timed_out).
            "samples_measured": sum(1 for to in per_chapter_is_timeout if not to),
            "samples_timed_out": sum(1 for to in per_chapter_is_timeout if to),
            # Numeric T1 components (mean across successful samples).
            "bpp": _mean_field("bpp"),
            "decode_quality": _mean_field("decode_quality"),
            "res_norm": _mean_field("res_norm"),
            "blockiness": _mean_field("blockiness"),
            "fft_hf_ratio": _mean_field("fft_hf_ratio"),
            "tenengrad": _mean_field("tenengrad"),
            "tenengrad_norm": _mean_field("tenengrad_norm"),
            # v5.1 USM-damped Tenengrad — REQUIRED for the v6 cache-load
            # gate (sites/search_orchestrator.py:REQUIRED_V6_FIELDS). Without
            # this aggregate-level entry, every chapter-probe cache write
            # got DROPPED on next-session load because the gate's
            # `all(f in meta for f in REQUIRED_V6_FIELDS)` check failed
            # → 30-day TTL had zero effect on the dominant probe path,
            # every search re-probed from scratch. Grep target:
            # tenengrad_clean. Per-page metadata writes the field at
            # search_orchestrator.py:_compute_t1_score / _compute_t1_score_bw.
            "tenengrad_clean": _mean_field("tenengrad_clean"),
            "jpeg_qf": _mean_field("jpeg_qf"),
            "jpeg_qf_norm": _mean_field("jpeg_qf_norm"),
            "jpeg_nse": _mean_field("jpeg_nse"),
            "t1_score": _mean_field("t1_score"),
            # Majority vote for content-type classifiers. Edge case: 1 color
            # splash + 7 B&W → reports B&W (correct — dominant content type).
            "is_grayscale": gs_count >= max(1, len(per_chapter_metas) // 2 + 1),
            "is_lossless": lossless_count >= max(1, len(per_chapter_metas) // 2 + 1),
            # v5.1: majority-vote outlier (≥half of probed pages share it).
            # See `majority_outlier` derivation above for rationale — first-
            # found was too noisy for per-page outliers added in v5.1.
            "outlier": majority_outlier,
            # Throttle-probe tail result — drives the rizzcomic override
            # short-circuit at sites/rizzcomic.py. None when the tail
            # couldn't run (e.g. only 1 chapter probed total).
            "cdn_reliability": cdn_reliability,
            # True when PROBE_SOURCE_BUDGET_S expired mid-sampling and the
            # remaining chapters were recorded as failures — the score is a
            # bounded partial aggregate of a timing-out host. Cache-audit /
            # UI-tooltip diagnostic; not read by ranking.
            "probe_budget_exhausted": budget_exhausted,
            # Provenance for debugging / cache audit. The picked chapter
            # indices let calibration replay deterministically what was
            # measured.
            "chapter_indices_sampled": [abs_idx for abs_idx, _ in chapter_picks],
            # v5.1 (Phase 4): series-level content_type from
            # _classify_series_content (search_orchestrator.py). The string
            # drives per-content-type T1 weights + res_norm targets in
            # _compute_t1_score; the rescored per-page metadata in
            # per_chapter_metas already reflects the classification.
            "content_type": series_content_type,
            # Mean chroma variance across successful pages — useful for the
            # classifier's color/B&W discrimination and for the UI tooltip.
            "chroma_var": _mean_field("chroma_var"),
        }
        # T2/T3 placeholders propagate from per-sample metadata (every sample
        # carries these; we surface the t2_available bit and any populated
        # T2 fields as means). Phase 3 will populate t2_score / clip_iqa /
        # niqe; Phase 4 populates paired_quality_adjustment.
        aggregate_metadata.setdefault(
            "t2_available",
            any(m.get("t2_available") for m in per_chapter_metas),
        )
        for f in ("t2_score", "clip_iqa_mean", "niqe_score", "niqe_norm"):
            aggregate_metadata.setdefault(f, _mean_field(f))
        # clip_iqa_scores is a per-prompt dict; preserve from first sample
        # that has it populated (all should agree per Phase 3 design).
        for m in per_chapter_metas:
            if m.get("clip_iqa_scores"):
                aggregate_metadata.setdefault("clip_iqa_scores", m["clip_iqa_scores"])
                break
        else:
            aggregate_metadata.setdefault("clip_iqa_scores", None)
        # T3 fields default null (populated post-probe by orchestrator).
        aggregate_metadata.setdefault("paired_quality_adjustment", None)
        aggregate_metadata.setdefault("paired_anchor_site", None)
        aggregate_metadata.setdefault("paired_dists_median", None)
        aggregate_metadata.setdefault("paired_pairs_compared", 0)

        return aggregate_score, aggregate_metadata

    def _probe_cover_image(
        self, hit: "SearchHit", scraper, make_request
    ) -> Optional[bytes]:
        """Cover-image fallback when chapter probe fails.

        Faster than chapter probe (1 HTTP request vs 3-4) but biased
        per-site because covers and chapter pages have different
        compression policies on most aggregator CDNs. Override when the
        cover URL needs site-specific cleanup before fetching (see
        MangaFire's _probe_cover_image which strips the @<digits>
        thumbnail token).
        """
        if not hit or not getattr(hit, "cover", None):
            return None
        cover_url = hit.cover
        if isinstance(cover_url, str) and cover_url.startswith("localfile://"):
            try:
                parsed = urlparse(cover_url)
                path = unquote(parsed.path or "")
                if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
                    path = path[1:]
                with open(path, "rb") as f:
                    data = f.read()
                if not looks_like_real_image(data):
                    return None
                return data
            except Exception:
                return None
        try:
            response = scraper.get(cover_url, timeout=10)
            if response.status_code >= 400:
                return None
            data = response.content
            if not looks_like_real_image(data):
                return None
            return data
        except Exception:
            return None


__all__ = [
    "BaseSiteHandler",
    "SiteComicContext",
    "SearchHit",
]
