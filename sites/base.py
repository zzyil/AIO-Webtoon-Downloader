from __future__ import annotations

import datetime as dt
import math
import os
import queue
import re
import socket
import statistics
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from ._image_io import (
    IMAGE_ACCEPT,
    IMAGE_ACCEPT_HEADERS,
    finalize_pending_image,
    looks_like_real_image,
)
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
    from curl_cffi.const import CurlHttpVersion as _CurlHttpVersion
    from curl_cffi.const import CurlOpt as _CurlOpt
    _CURL_CFFI_AVAILABLE = True
except Exception:  # ImportError or any sub-dep failure
    _CurlCffiAsyncSession = None  # type: ignore[assignment]
    _CurlHttpVersion = None  # type: ignore[assignment]
    _CurlOpt = None  # type: ignore[assignment]
    _CURL_CFFI_AVAILABLE = False


# --- fast-download timeout semantics ---------------------------------------
# THE BUG THIS ENCODES A FIX FOR (bench/linewebtoonlogs.md, 2026-08-20):
# curl_cffi's scalar `timeout=` is a TOTAL-TRANSFER deadline (it sets only
# CurlOpt.TIMEOUT_MS — see curl_cffi/requests/utils.py:set_curl_options), while
# `requests`/cloudscraper's `timeout=` — which aio-dl.py:_try_download_url uses
# on the SLOW path — is a per-read STALL timeout. So the same `--http-timeout 30`
# means "30s of silence" on the slow path and "30s total, healthy or not" here.
# On LINE Webtoon (400-800 KB vertical-strip pages on Naver's swebtoon-phinf
# CDN) a page that HTTP/2-starves down to ~20 KB/s needs ~35s, so it was
# guillotined mid-transfer, retried from byte 0, and guillotined again:
# 17 of the 18 logged failures were transfers still making progress, one at
# 670472 of 678424 bytes (98.8%). Two such pages then failed the chapter's
# zero-tolerance gate and --multi-source "rescued" a 120-page official chapter
# with a 103-page third-party re-host.
#
# The fix is to measure STALLS instead of totals: LOW_SPEED_LIMIT/_TIME abort
# only when throughput actually collapses, so a slow-but-live transfer finishes
# while a dead socket still dies on the user's `--http-timeout` schedule.
# TIMEOUT_MS survives as a ceiling so nothing can hang indefinitely on a
# trickle that stays just above the low-speed floor.
#
# A retry RESUMES the bytes the failed attempt delivered (HTTP Range, see
# _PartialImage below). It was first left out on the theory that a stalled page
# needs a different connection, not a byte offset; the 2026-10-20 library
# refresh disproved that: swebtoon-phinf served ONE Tower of God image at
# ~3-5 KB/s for 8+ minutes on fresh connections and HTTP/1.1 alike (a cold
# edge cache, by every sign), and two attempts moved 551 KB + 820 KB of a
# 995 KB page, more than the page, while each restart from byte 0 under the
# ceiling could never finish it. The chapter fell to --multi-source
# (bench/library-refresh/logs/tower-of-god/download-1.log, ch 284 and 453).

# A transfer below this many bytes/sec for the caller's whole `timeout` window
# is dead rather than slow. Well under the ~20 KB/s a starved HTTP/2 stream on
# a busy CDN connection still achieves, so genuinely-moving transfers never
# trip it. The DURATION is the caller's --http-timeout, not a constant here,
# which is what makes the flag mean the same thing it means on the slow path.
_FAST_DL_STALL_BYTES_PER_SEC = 1024

# Absolute per-page ceiling as a multiple of the caller's timeout, applied via
# TIMEOUT_MS. 6x the 30s default = 3 min, comfortably above the ~40s a worst-
# case 800 KB page needs at the slowest rate observed live (17 KB/s).
_FAST_DL_CEILING_MULTIPLIER = 6

# Attempts per page. Attempt 0 rides the shared multiplexed session; later
# attempts escalate onto their own connection (see _fast_dl_retry_options),
# which is what breaks an HTTP/2 stream that stalled at a fixed byte offset
# (the log shows the same URL dying at exactly 131064 bytes three times, then
# "HTTP/2 stream 211 was not closed cleanly: PROTOCOL_ERROR").
_FAST_DL_DEFAULT_ATTEMPTS = 3


def _fast_dl_stall_options(timeout: float) -> Dict[Any, Any]:
    """curl options implementing "abort on stall, not on elapsed time".

    Returned as a session-level `curl_options` dict because curl_cffi applies
    those AFTER every option it derives itself ("set extra options, after all
    others, because it will alter some options"), which is what lets these
    override the CONNECTTIMEOUT the scalar `timeout=` would otherwise leave
    unset. Per-request `curl_options` is NOT available on AsyncSession.request
    in curl_cffi 0.15 — only on the constructor — hence one session per
    attempt tier rather than one option dict per call.
    """
    if not _CURL_CFFI_AVAILABLE:
        return {}
    t = max(1.0, float(timeout))
    return {
        _CurlOpt.CONNECTTIMEOUT_MS: int(t * 1000),
        _CurlOpt.LOW_SPEED_LIMIT: _FAST_DL_STALL_BYTES_PER_SEC,
        _CurlOpt.LOW_SPEED_TIME: int(math.ceil(t)),
    }


def _fast_dl_retry_options(timeout: float) -> Dict[Any, Any]:
    """Stall options plus "do not touch the pooled connection".

    FRESH_CONNECT opens a new connection for the request and FORBID_REUSE
    closes it afterwards, so a retry cannot land back on the same HTTP/2
    connection whose stream just stalled — and cannot poison the pool for the
    pages still succeeding on it.
    """
    opts = _fast_dl_stall_options(timeout)
    if opts:
        opts[_CurlOpt.FRESH_CONNECT] = 1
        opts[_CurlOpt.FORBID_REUSE] = 1
    return opts


# Bytes a resumed request re-fetches from BEFORE the end of what is held, and
# that must come back identical. This is the proof that the response continues
# the same object: swebtoon-phinf sends no ETag, and its Last-Modified is the
# edge's cache-fill time (it changed between two requests ten minutes apart),
# so neither validator identifies the bytes. 4 KB of compressed image data
# matching by chance is not a real possibility.
_FAST_DL_RESUME_OVERLAP = 4096

_CONTENT_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$", re.I)


class _PartialImage:
    """The bytes of ONE page that failed attempts delivered, kept so the next
    attempt asks only for the rest (fast_download_images, grep _PartialImage).

    Contract: `complete()` only ever returns bytes that are byte-for-byte one
    object. Every way that could break resets the held bytes and the next
    attempt starts from 0, exactly the pre-resume behaviour:
      - a 206 must start at the offset asked for, carry the same total the
        first response declared, and repeat the held overlap window exactly
        (_FAST_DL_RESUME_OVERLAP); anything else is a different object or a
        broken range implementation;
      - a 200 to a Range request (CDNs that ignore Range, or serve a cold
        object whole) is a fresh full body and replaces the held bytes;
      - a compressed body (Content-Encoding other than identity) is never held:
        curl hands back DECODED bytes, whose offsets mean nothing to Range;
      - the assembled body must be exactly the declared total long.
    Page identity is not this class's concern: it is keyed by one page's URL
    inside one call, so it cannot move bytes between pages. A page that never
    assembles stays a miss for the chapter completeness gate in aio-dl.py.
    Held in memory for the duration of one fast_download_images call; a later
    call (the prefetch handing over to the foreground, an inline chapter
    retry) starts the page from 0.
    """

    __slots__ = ("data", "total")

    def __init__(self) -> None:
        self.data = b""
        self.total: Optional[int] = None

    def reset(self) -> None:
        self.data = b""
        self.total = None

    def resume_from(self) -> Optional[int]:
        """Byte offset for the next request's Range header, None = whole."""
        if not self.data or not self.total or len(self.data) >= self.total:
            return None
        return max(0, len(self.data) - _FAST_DL_RESUME_OVERLAP)

    @staticmethod
    def _encoded(resp: Any) -> bool:
        enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
        return enc not in ("", "identity")

    def _absorb_206(self, resp: Any, asked_from: Optional[int]) -> bool:
        """Splice a 206 body onto the held bytes. False = not the same object
        (the held bytes are then reset)."""
        m = _CONTENT_RANGE_RE.match(resp.headers.get("Content-Range") or "")
        content = resp.content or b""
        if (
            asked_from is None
            or m is None
            or self._encoded(resp)
            or int(m.group(1)) != asked_from
            or int(m.group(3)) != self.total
        ):
            self.reset()
            return False
        overlap = self.data[asked_from:]
        # A transfer that died inside the overlap window can only be checked
        # on what it delivered; it adds nothing, so the held bytes stand.
        if len(content) <= len(overlap):
            if overlap[:len(content)] != content:
                self.reset()
                return False
            return True
        if content[:len(overlap)] != overlap:
            self.reset()
            return False
        self.data = self.data[:asked_from] + content
        if len(self.data) > self.total:
            self.reset()
            return False
        return True

    def absorb_failure(self, resp: Any, asked_from: Optional[int]) -> None:
        """Keep what a failed attempt (the request raised) delivered.
        `resp` is curl_cffi's `exc.response`: on a transfer error it still
        holds the status, headers and every body byte received."""
        if resp is None or not (resp.content or b""):
            return
        if resp.status_code == 206:
            self._absorb_206(resp, asked_from)
            return
        if resp.status_code != 200 or self._encoded(resp):
            return
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if total <= 0 or len(resp.content) >= total:
            # No declared length = nothing to prove the assembled body
            # against; a body at full length did not fail on its bytes.
            self.reset()
            return
        self.data = bytes(resp.content)
        self.total = total

    def complete(self, resp: Any, asked_from: Optional[int]) -> Optional[bytes]:
        """The whole object from a successful 206, or None when the response
        does not continue the held bytes (they are reset; the next attempt
        starts from 0)."""
        if not self._absorb_206(resp, asked_from):
            return None
        if len(self.data) != self.total:
            self.reset()
            return None
        body, self.data, self.total = self.data, b"", None
        return body


# Exceptions from a probe fetch that mean "we couldn't MEASURE this page in time"
# (slowness), NOT "the page/CDN is definitively broken". The matched-page probe
# (sites/match_session.py) counts these as timeouts, never as failures, so a
# merely-slow site is not rated `unreachable`. requests.exceptions.Timeout
# already covers BOTH ConnectTimeout and ReadTimeout. Base
# requests.exceptions.ConnectionError (refused / reset / DNS) is DELIBERATELY
# NOT here — that's a genuine reachability failure and must count as a failed
# fetch. Grep _PROBE_TIMEOUT_EXCEPTIONS: _fetch_probe_item_bytes_ex,
# match_fetch_window, match_session, mangadex's match_fetch_window override.
try:
    import requests as _requests
    _PROBE_TIMEOUT_EXCEPTIONS = (_requests.exceptions.Timeout, socket.timeout)
except Exception:
    _PROBE_TIMEOUT_EXCEPTIONS = (socket.timeout,)


class SiteBusyError(Exception):
    """Raised by a handler's search() when THIS MACHINE, not the site, kept it
    from answering — today only mangafire, whose browser profile another AIO
    process was holding and whose temporary-profile fallback also failed
    (sites/mangafire_vrf.py, grep profile_contended).

    The search orchestrator treats it as neither success nor failure: no
    ProbeFailureCache strike (a busy profile says nothing about the host), and a
    site-health entry with status "busy" that the UI shows as a notice but never
    counts toward recommending the site be disabled (grep `"busy"` in
    sites/search_orchestrator.py and UI-source/src/hooks/useDownloader.js).

    ``reason`` is a short machine token for the payload; the message is for logs.
    """

    def __init__(self, message: str, reason: str = "busy") -> None:
        super().__init__(message)
        self.reason = reason or "busy"


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

    `pages` ({1-based page index: image bytes}) is what the handler DID get. A
    handler that downloads in-process (MangaDex MD@H) passes it so the bytes
    become multi-source rescue evidence (aio-dl.py grep
    `_capture_partial_pages(n, _cfp.pages_from_blobs`) instead of vanishing with
    the exception. Optional: URL-listing handlers have no bytes to give.
    """

    def __init__(
        self,
        pages_ok: int,
        pages_total: int,
        host: str = "",
        reason: str = "",
        pages: Optional[Dict[int, bytes]] = None,
    ) -> None:
        self.pages_ok = int(pages_ok)
        self.pages_total = int(pages_total)
        self.host = host or ""
        self.reason = reason or "handler_incomplete"
        self.pages = dict(pages or {})
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
      - census_total:  distinct chapter numbers with a readable version, the
                       denominator (build_group_census).
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


def is_placeholder_chapter(chapter: Any) -> bool:
    """True for a chapter row that is listed but can never be downloaded as
    content: a `_locked` placeholder (tapas premium episodes; MangaDex
    publisher link-outs, `_locked_kind="external"`) or a row a handler marked
    `_undownloadable`.

    Placeholders stay in chapter lists on purpose: the number is still known,
    so the version ranker can prefer a readable version of it and
    --multi-source can fill it from another site. Everything that must not
    treat one as content goes through this helper or readable_chapters:
    select_best_chapter_version, build_group_census, aio_search_cli's alt
    chapter lists, the search match probe's match_readable_versions (grep
    both names).
    """
    if not isinstance(chapter, dict):
        return False
    return bool(chapter.get("_locked")) or chapter.get("_undownloadable") is True


def readable_chapters(chapters: Optional[List[Dict]]) -> List[Dict]:
    """The rows of `chapters` that are real content (see is_placeholder_chapter)."""
    return [c for c in (chapters or []) if not is_placeholder_chapter(c)]


def daemon_map(fn: Callable, items: Iterable, width: int, name: str, default: Any = None) -> List:
    """`[fn(x) for x in items]` on up to `width` DAEMON threads, in input
    order; an item whose call raised gets `default`. Each call runs in a copy
    of the CALLER's context, taken here before any thread starts (worker
    threads start with an empty one: memory contextvar-worker-thread-trap).

    Daemon threads, NOT ThreadPoolExecutor: concurrent.futures joins its
    workers at interpreter exit even after shutdown(wait=False), so one fetch
    hung on a trickling TLS read holds the process open after the search has
    printed its result (real case in sites/mangadex.py's _REPORT_QUEUE
    comment). The match session abandons such fetches at the source budget
    (grep _abandon_over_budget in match_session.py), so they must not pin exit.
    This call itself still waits for every item; its caller is the one that
    gets abandoned."""
    import contextvars

    items = list(items)
    out: List[Any] = [default] * len(items)
    width = max(1, min(int(width or 1), len(items)))
    if len(items) <= 1 or width == 1:
        for i, x in enumerate(items):
            try:
                out[i] = fn(x)
            except Exception:
                pass
        return out
    # one context per item: a Context cannot be entered by two threads at once
    ctxs = [contextvars.copy_context() for _ in items]
    work: "queue.Queue" = queue.Queue()
    for i, x in enumerate(items):
        work.put((i, x))

    def _run() -> None:
        while True:
            try:
                i, x = work.get_nowait()
            except queue.Empty:
                return
            try:
                out[i] = ctxs[i].run(fn, x)
            except Exception:
                pass

    threads = [threading.Thread(target=_run, name=name, daemon=True) for _ in range(width)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


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

    Placeholders (is_placeholder_chapter) are not a group's work: a publisher
    whose every row is a MangaDex link-out supplies nothing. The total counts
    only numbers with a readable version, so a series that is half link-outs
    doesn't push its real translator out of the top band.
    """
    census: Dict[str, int] = {}
    readable_total = 0
    for versions in chapters_by_num.values():
        readable = readable_chapters(versions)
        if readable:
            readable_total += 1
        seen_here: set = set()
        for version in readable:
            for info in handler.get_group_infos(version):
                key = handler.get_group_match_key(info.name)
                if key:
                    seen_here.add(key)
        for key in seen_here:
            census[key] = census.get(key, 0) + 1
    return census, readable_total


# --- Search matched-page probe: per-window fetch concurrency ----------------
# How many of one match window's pages BaseSiteHandler.match_fetch_window
# fetches CONCURRENTLY on a bounded daemon pool (daemon_map). Serial fetches
# made a slow-per-request host cost the window's page count times its latency;
# the pool collapses that to ceil(pages / N) waves. Which pages are fetched is
# fixed before any I/O (match_window_order), so concurrency only reorders the
# fetches. Default 4 keeps the per-host burst modest. Env override tunes the
# fleet default without a release (mirrors search_orchestrator._FANOUT_MAX); a
# subclass sets PROBE_BREADTH_CONCURRENCY=1 to force serial (e.g. a browser
# handler avoiding concurrent tabs). Grep target: PROBE_BREADTH_CONCURRENCY.
# ("BREADTH" is historical: the knob first bounded the old breadth-sampling
# probe, and the name was kept so handler overrides and the env var still apply.)
_PROBE_BREADTH_CONCURRENCY = 4
try:
    _PROBE_BREADTH_CONCURRENCY = max(
        1,
        int(os.environ.get("AIO_PROBE_BREADTH_CONCURRENCY", "") or _PROBE_BREADTH_CONCURRENCY),
    )
except (TypeError, ValueError):
    pass


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
    # True on handlers whose per-chapter fetch is expensive by design (e.g.
    # browser-driven capture). Its one reader today is the search site-health
    # verdict: such a handler is "constitutionally slow" and exempt from the
    # slow-probe signal, so a healthy slow probe never gets the site
    # recommended for disabling (grep EXPENSIVE_PROBE in
    # search_orchestrator.py, _classify_site_health). No handler sets it
    # today (MangaFire's 2026 REST-API rewrite dropped its VRF cost).
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
    # Why this matters: webtoons.com serves vertical-scroll PNGs at the
    # intentional 720–800px width while aggregators re-host them upscaled to
    # 1500–2000px JPEG, which is generation loss from the official files.
    # The old single-image scorer rewarded that resolution, and without this
    # flag search confidently chose toonily over the literal publisher. The
    # matched-page ranker (sites/page_ranker.py) gates its resolution credit
    # on an enlargement test, but the publisher still wins its tie here
    # regardless of measured quality. Cross-file:
    # sites/search_orchestrator.py:SourceEntry.is_official + source_rank_key
    # consume this flag; grep OFFICIAL_PUBLISHER for the consumer side.
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

        Order: the image Accept first, then extra headers, then Referer, then
        User-Agent — each later group overrides the earlier one on a key
        collision (so a handler needing a different Accept just puts it in
        FAST_DL_EXTRA_HEADERS; none does today).

        Accept matters: curl_cffi's `impersonate` seeds a *document* Accept, and
        hosts that content-negotiate then serve an HTML wrapper page for an
        image URL. See sites/_image_io.py:IMAGE_ACCEPT.
        """
        headers: Dict[str, str] = {"Accept": IMAGE_ACCEPT}
        headers.update(self.FAST_DL_EXTRA_HEADERS)
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
        attempts: int = _FAST_DL_DEFAULT_ATTEMPTS,
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
          timeout:        STALL budget, not a total-transfer deadline: a page
                          is abandoned after `timeout` seconds below
                          _FAST_DL_STALL_BYTES_PER_SEC, not after `timeout`
                          seconds of downloading. Also the connect timeout.
                          30s matches aio-dl.py's default _HTTP_TIMEOUT and
                          the meaning `requests` gives the same number on the
                          slow path. An absolute ceiling of
                          _FAST_DL_CEILING_MULTIPLIER x timeout still applies.
                          See the module-level comment above
                          _FAST_DL_STALL_BYTES_PER_SEC for why this is not
                          curl_cffi's default scalar-timeout behaviour.
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
          attempts:       Tries per page before it counts as a miss. Attempt 0
                          shares the multiplexed session; every later attempt
                          gets its own connection, and the last also drops to
                          HTTP/1.1 (grep _fast_dl_retry_options). Callers pass
                          the user's --http-max-retries; the old hardcoded 2
                          ignored it entirely.
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

        # Stall budget vs. absolute ceiling — see the module-level comment on
        # _FAST_DL_STALL_BYTES_PER_SEC. `timeout` is what the user set with
        # --http-timeout; `ceiling` only exists so a transfer that trickles
        # just above the low-speed floor can't run forever.
        stall_timeout = max(1.0, float(timeout))
        ceiling = stall_timeout * _FAST_DL_CEILING_MULTIPLIER
        n_attempts = max(1, int(attempts))

        async def _fetch_one(
            get_session, sema, page_idx: int, url: str, folder: str, filename: str
        ) -> Tuple[int, Optional[str]]:
            base, _ = os.path.splitext(filename)
            if not base:
                base = filename
            pending_path = os.path.join(folder, f".pending_{base}{pending_suffix}")
            host = urlparse(url).netloc

            # Escalating attempts rather than N identical tries. Attempt 0 uses
            # the shared multiplexed session; later attempts get their own
            # connection so a page whose HTTP/2 stream stalled is not retried
            # onto the very connection that starved it, and the final attempt
            # also drops to HTTP/1.1 (grep _tier_for_attempt).
            # No URL-variant cascade — alternates rarely exist on image CDNs and
            # subclasses can override fast_download_images entirely if they
            # need one. (MangaFire confirmed: alternative path segments
            # /o/, /full/, /orig/ and extensions .png, .webp all 404.)
            #
            # The inter-attempt backoff is deliberately taken OUTSIDE the
            # semaphore: a page that is only sleeping must not occupy one of
            # the `concurrency` slots that its still-healthy siblings need.
            #
            # Each failed attempt's bytes are kept and the next attempt asks
            # for the rest (grep _PartialImage for the guards that keep a
            # spliced body one object).
            retry_delay = 0.0
            partial = _PartialImage()
            for attempt in range(n_attempts):
                if is_cancelled is not None and is_cancelled():
                    return page_idx, None
                if retry_delay:
                    await asyncio.sleep(retry_delay)
                    retry_delay = 0.0
                asked_from = partial.resume_from()
                req_headers = headers
                if asked_from is not None:
                    req_headers = dict(headers)
                    req_headers["Range"] = f"bytes={asked_from}-"
                async with sema:
                    # Re-check after sema acquire — coroutines that were
                    # queued before cancel was set should still bail here
                    # rather than firing a GET they were already cancelled
                    # for. (Without this, large queues + late cancel = the
                    # remaining tail still issues HTTP requests.)
                    if is_cancelled is not None and is_cancelled():
                        return page_idx, None
                    try:
                        session = await get_session(attempt)
                        r = await session.get(url, headers=req_headers, timeout=ceiling)
                    except Exception as exc:
                        # On a transfer error curl_cffi still attaches the
                        # response with every byte received (exc.response).
                        partial.absorb_failure(getattr(exc, "response", None), asked_from)
                        # Per-attempt visibility: shows up in aio-dl's stderr
                        # which the UI's LogPanel surfaces. Ported from the
                        # 2026-05-13-deleted mangafire-specific
                        # fast_download_images for log-verbosity parity.
                        # Subclasses that don't want this can override
                        # fast_download_images and skip the print.
                        import sys
                        held = (f" (holding {len(partial.data)}/{partial.total} bytes "
                                f"to resume)" if partial.resume_from() is not None else "")
                        print(f"[-] curl_cffi exception: {exc} for URL: {url}{held}", file=sys.stderr)
                        if attempt < n_attempts - 1:
                            retry_delay = min(4.0, 1.0 * (attempt + 1))
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
                body = r.content
                resume_rejected = False
                if r.status_code == 206:
                    # Only a resumed request gets a 206; complete() returns
                    # None unless the response provably continues the held
                    # bytes, which it then resets.
                    body = partial.complete(r, asked_from)
                    resume_rejected = body is None
                elif r.status_code == 200:
                    # A whole body, also when a Range request was ignored.
                    partial.reset()
                elif r.status_code == 416 and asked_from is not None:
                    partial.reset()
                    resume_rejected = True
                if resume_rejected or r.status_code not in (200, 206) or not looks_like_real_image(body):
                    import sys
                    body_len = len(r.content) if r.content else 0
                    note = (" (resume rejected: not a continuation of the held bytes; "
                            "next attempt starts from byte 0)" if resume_rejected else "")
                    print(
                        f"[-] curl_cffi status={r.status_code} "
                        f"size={body_len} for URL: {url}{note}",
                        file=sys.stderr,
                    )
                    # A permanent 4xx will not become a 200 on attempt 4, and
                    # retrying it costs more than wall-clock: record_host_failure
                    # only fires on the LAST attempt, so grinding through the
                    # full budget also DELAYS the ghost-chapter detector that
                    # feeds on those uniform (status, body_size) signatures
                    # (mangafire's 5051-byte 403 placeholders — grep
                    # _is_ghost_chapter_signature in aio-dl.py). 408/429 are the
                    # two 4xx that genuinely mean "later", so they keep retrying.
                    # Mirrors _classify_response_failure's permanent/retryable
                    # split on the slow path.
                    # A rejected resume (incl. 416) says nothing about the
                    # page: the retry asks for the whole object.
                    permanent = (
                        not resume_rejected
                        and 400 <= r.status_code < 500
                        and r.status_code not in (408, 429)
                    )
                    if attempt < n_attempts - 1 and not permanent:
                        retry_delay = min(4.0, 1.0 * (attempt + 1))
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
                        fh.write(body)
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

        # Three session tiers, regardless of how many attempts the user allows:
        #   0 = shared, multiplexed, keep-alive (the fast path)
        #   1 = own connection per request, still HTTP/2
        #   2 = own connection per request, forced HTTP/1.1
        # Collapsing the middle attempts onto one tier matters because each
        # tier is a real AsyncSession (curl_options is constructor-only in
        # curl_cffi 0.15) — keying tiers by raw attempt number would build a
        # fresh session per retry for no behavioural difference.
        _TIER_SHARED, _TIER_FRESH, _TIER_H11 = 0, 1, 2

        def _tier_for_attempt(attempt: int) -> int:
            if attempt <= 0:
                return _TIER_SHARED
            if attempt >= n_attempts - 1 and n_attempts >= 3:
                return _TIER_H11
            return _TIER_FRESH

        def _session_kwargs_for_tier(tier: int) -> Dict[str, Any]:
            """AsyncSession kwargs for one tier. Later tiers open their own
            connection per request, and the last also drops to HTTP/1.1, so a
            page that a stalled or protocol-erroring HTTP/2 connection could
            not deliver gets a genuinely different transport rather than
            another identical try on the same wire."""
            kw: Dict[str, Any] = {"impersonate": self.FAST_DL_IMPERSONATE}
            if cookies:
                kw["cookies"] = cookies
            if tier == _TIER_SHARED:
                kw["curl_options"] = _fast_dl_stall_options(stall_timeout)
            else:
                kw["curl_options"] = _fast_dl_retry_options(stall_timeout)
                if tier == _TIER_H11 and _CurlHttpVersion is not None:
                    kw["http_version"] = _CurlHttpVersion.V1_1
            return kw

        async def _run() -> List[Tuple[int, Optional[str]]]:
            sema = asyncio.Semaphore(max(1, int(concurrency)))
            # Tier 0: one AsyncSession across all pages of this chapter so
            # HTTP/2 multiplex + connection keepalive amortize TLS handshake
            # cost. impersonate sets the JA3/JA4 + h2 settings frame to match a
            # real browser — should not strictly be needed for cookieless
            # edge-cached image CDNs, but defensive (and free).
            #
            # Retry tiers are built LAZILY and cached: a clean chapter (the
            # overwhelmingly common case) never pays for a second session, but
            # when several pages do fail they share one retry session instead
            # of each constructing their own.
            sessions: Dict[int, Any] = {}
            session_lock = asyncio.Lock()

            async def _get_session(attempt: int):
                tier = _tier_for_attempt(attempt)
                s = sessions.get(tier)
                if s is not None:
                    return s
                async with session_lock:
                    s = sessions.get(tier)
                    if s is None:
                        s = _CurlCffiAsyncSession(**_session_kwargs_for_tier(tier))
                        sessions[tier] = s
                    return s

            try:
                tasks = [
                    _fetch_one(_get_session, sema, p_idx, url, folder, name)
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
            finally:
                # Close every tier we actually opened. Swallowing here is
                # deliberate: a close failure must not turn a chapter whose
                # pages all landed into a failed one.
                for s in sessions.values():
                    try:
                        await s.close()
                    except Exception:
                        pass

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

    # --- Native volume releases ---------------------------------------------
    # Some sites publish whole-volume scans as releases separate from their
    # chapters (MangaFire: /api/titles/{hid}/volumes + /api/volumes/{id}).
    # Volumes are NOT built here by grouping chapters by their volume tag.
    # That was decided against (2026-11-06; memory volume-downloads.md).
    #
    # Override contract for a volume-capable handler:
    #   get_volumes        -> chapter-shaped dicts, with "chap" = volume number
    #   get_volume_images  -> page URLs of one volume
    #   has_volumes        -> optional cheap per-series hint
    # Callers never call these two fetchers directly. They go through
    # list_volumes (stamps the kind tag) and get_item_images (routes by it), so
    # a volume can't reach get_chapter_images. On a site whose volume and
    # chapter ids overlap, that call would silently return a different
    # chapter's pages.
    #
    # The kind tag is the literal "volume", the value of item_kinds.VOLUME.
    # sites/ does not import that root module. Keep the two in step:
    # grep _ITEM_KIND_VOLUME.
    _ITEM_KIND_VOLUME = "volume"

    def get_volumes(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        """Override: this series' native volume releases in *language*.
        [] means none, which is also the default for every site without volumes."""
        return []

    def get_volume_images(self, volume: Dict, scraper, make_request) -> List[str]:
        """Override alongside get_volumes: the page URLs of one volume."""
        raise NotImplementedError(
            f"{type(self).__name__} lists volumes but cannot fetch their pages"
        )

    def has_volumes(self, context: SiteComicContext) -> Optional[bool]:
        """Cheap per-series hint read from data the context fetch already has.
        None = unknown; ask get_volumes. Used for --list-chapters --with-volumes
        and for a clear `no_volumes` message."""
        return None

    @classmethod
    def supports_volumes(cls) -> bool:
        """True when the class overrides get_volumes. Derived from the code, not
        declared, so it can't drift from the implementation. Same pattern as
        api.py's search_capable. The UI's capability list reads it (grep
        handler_capabilities)."""
        return cls.get_volumes is not BaseSiteHandler.get_volumes

    def list_volumes(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        """get_volumes, normalized. Do not override.

        Each item comes back with ``_kind="volume"``, ``vol`` = ``chap``, and
        ``chap`` in the ``:g`` label form that --list-chapters and the ledger
        use. Two kinds of item are dropped with a warning:
          - a non-numeric number: the pipeline buckets, filters and names by
            the number, and a label it can't parse would be dropped silently
            further down anyway;
          - a duplicate number: the LAST one wins, which for MangaFire is the
            newest release.
        """
        out: Dict[str, Dict] = {}
        for raw in self.get_volumes(context, scraper, language, make_request) or []:
            item = dict(raw)
            try:
                num = float(item.get("chap"))
                if not math.isfinite(num):
                    raise ValueError
            except (TypeError, ValueError):
                # stderr: --list-chapters prints its JSON on stdout.
                import sys

                print(f"[!] {self.name}: skipping volume with no usable number: "
                      f"{item.get('title') or item.get('chap')!r}", file=sys.stderr)
                continue
            label = f"{num:g}"
            item["chap"] = label
            item["vol"] = label
            item["_kind"] = self._ITEM_KIND_VOLUME
            out[label] = item
        return sorted(out.values(), key=lambda v: float(v["chap"]))

    def get_item_images(self, item: Dict, scraper, make_request) -> List[str]:
        """THE entry point for fetching one item's pages. Do not override.

        Routes on the ``_kind`` tag that list_volumes stamps. Untagged items
        are chapters, so every existing chapter path behaves exactly as
        before.
        """
        if item.get("_kind") == self._ITEM_KIND_VOLUME:
            return self.get_volume_images(item, scraper, make_request)
        return self.get_chapter_images(item, scraper, make_request)

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
         1 downloadable    A placeholder (is_placeholder_chapter: a MangaDex
                           publisher link-out, a tapas locked episode) is
                           metadata, not a chapter. select_best_chapter_version
                           already ranks among readable versions only when any
                           exist, so this tier is a backstop for an
                           all-placeholder pool. Set ONLY from an explicit
                           handler flag, never inferred from a missing field.
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
            0 if is_placeholder_chapter(version) else 1,
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

        # Readable first: when any version of this number is real content,
        # placeholders (is_placeholder_chapter: MangaDex publisher link-outs,
        # tapas locked episodes) leave the pool entirely, so they can't win on
        # official/census, can't skew the page-count median, and a --group
        # naming the publisher finds no match here and takes the normal group
        # fallback (user decision 2026-10-17, plan
        # sync-temp/plans/mangadex-link-outs.md). A placeholder-only number
        # keeps its placeholders, so the downloader can still fill that number
        # from another site.
        readable = readable_chapters(versions)
        placeholders_dropped = len(versions) - len(readable) if readable else 0
        pool = readable or versions

        # --mtl exclude: drop CONFIRMED machine translations outright. Never
        # `suspect` — hard-dropping real chapters on a heuristic is not a
        # trade the flag is allowed to make. Runs over the readable pool. When
        # every readable version is MTL but the number also has placeholders,
        # the placeholders come back instead of a skip, so the downloader can
        # fill the number from another site exactly as for a placeholder-only
        # number (user decision 2026-10-17, plan mangadex-link-outs.md L3).
        excluded_mtl = 0
        if policy.mtl == "exclude":
            probe_ctx = self._build_rank_context(pool, policy)
            keep = [
                v for i, v in enumerate(pool)
                if probe_ctx["signals"][i]["mtl_verdict"] != MTL_CONFIRMED
            ]
            excluded_mtl = len(pool) - len(keep)
            if not keep and placeholders_dropped:
                _debug(
                    f"    Ch {pool[0].get('chap', '?')}: every readable version "
                    f"is machine-translated and --mtl exclude is set; keeping "
                    f"the placeholder so another site can supply it."
                )
                keep = [v for v in versions if is_placeholder_chapter(v)]
                placeholders_dropped = 0
            elif not keep:
                _debug(
                    f"    Ch {pool[0].get('chap', '?')}: every readable version "
                    f"is machine-translated and --mtl exclude is set. Skipping."
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
            if version.get("_locked_kind") == "external":
                bits.append("publisher link")
            elif is_placeholder_chapter(version):
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
            dropped = (
                f" ({placeholders_dropped} unreadable version(s) set aside)"
                if placeholders_dropped else ""
            )
            _debug(
                f"    Ch {chap_label}: {prefix}{_describe(winner)}{tail}{reason}{dropped}."
            )

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

    # --- Matched-page search ranking (sites/match_session.py) --------------
    # The search ranker fetches the SAME chapter numbers from every source and
    # compares the same pages (plan sync-temp/plans/image-ranker-rework.md, P2).
    # These two methods are its whole handler surface, split so the session can
    # schedule them as separate tasks: the list first (to agree on targets),
    # then one window per target. Overridable per handler: mangadex overrides
    # match_fetch_window (its get_chapter_images downloads the whole chapter).

    # Pages per window: the one at the chapter's relative middle and +-2 around
    # it. P2.2: +-2 found 5% more matches than +-1 (credit pages and split pages
    # shift a peer's index by one or two).
    MATCH_WINDOW_PAGES = 5

    def match_readable_versions(self, chapters: Optional[List[Dict]]) -> Dict[int, Dict]:
        """Integer chapter number -> the ONE version a download would pick.

        Same bucketing as aio-dl.py's download path (grep `chapters_by_num`:
        `:g` labels, "Oneshot" -> 1) and the same ranker with a per-series
        census (build_group_census + select_best_chapter_version), so the pages
        compared are the pages the user would get. Placeholders never count.
        Only whole numbers are kept: .5 extras rarely exist on every site.
        A list with no whole number but exactly one readable chapter (a oneshot
        under a non-numeric label) maps it to 1."""
        readable = readable_chapters(chapters)
        by_num: Dict[str, List[Dict]] = {}
        for ch in readable:
            raw = ch.get("chap")
            if raw is None:
                continue
            label = f"{raw:g}" if isinstance(raw, (int, float)) else str(raw)
            if label.strip().lower() in ("oneshot", "one-shot"):
                label = "1"
            try:
                value = float(label)
            except (TypeError, ValueError):
                continue
            if value != int(value):
                continue
            by_num.setdefault(str(int(value)), []).append({**ch, "chap": str(int(value))})
        if not by_num:
            return {1: readable[0]} if len(readable) == 1 else {}
        census, total = build_group_census(self, by_num)
        policy = GroupSelectionPolicy(census=census or None, census_total=total)
        out: Dict[int, Dict] = {}
        for label, versions in by_num.items():
            try:
                best = self.select_best_chapter_version(
                    versions, [], False, selection_policy=policy,
                )
            except Exception:
                best = versions[0]
            if best is not None and not is_placeholder_chapter(best):
                out[int(label)] = best
        return out

    def match_fetch_window(
        self, chapter: Dict, scraper, make_request, n_pages: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fetch the page window of one chapter for matching.

        Returns {"pages": [(index, bytes)], "n_total": pages in the chapter,
        "attempted": fetches tried, "failed": non-timeout failures,
        "timed_out": timeouts}. The image-list call counts as one attempt when
        it fails (a chapter whose list cannot be read served nothing). Never
        raises. Timeouts are counted apart because slowness is not a quality
        verdict: the session rates a source `unreachable` only when every
        non-timeout fetch failed (grep `unreachable` in match_session.py)."""
        n = int(n_pages or self.MATCH_WINDOW_PAGES)
        out: Dict[str, Any] = {"pages": [], "n_total": 0, "attempted": 0, "failed": 0, "timed_out": 0}
        try:
            items = self.get_item_images(chapter, scraper, make_request)
        except (_PROBE_TIMEOUT_EXCEPTIONS, SiteBusyError):
            # SiteBusyError: this machine failed the call, not the site
            out.update(attempted=1, timed_out=1)
            return out
        except Exception:
            items = None
        if not items:
            out.update(attempted=1, failed=1)
            return out
        total = len(items)
        out["n_total"] = total
        order = self.match_window_order(total, n)
        # The window's pages are fetched concurrently, PROBE_BREADTH_CONCURRENCY
        # at a time (the same per-source knob the old probe used; a handler that
        # sets 1 is fetched serially), on daemon threads that carry the
        # session's interactive_solving(False) (see daemon_map).
        results = daemon_map(
            lambda i: self._fetch_probe_item_bytes_ex(items[i], scraper),
            order, self.PROBE_BREADTH_CONCURRENCY, "match-page", default=(None, False),
        )
        for i, (blob, timed_out) in zip(order, results):
            out["attempted"] += 1
            if blob:
                out["pages"].append((i, blob))
            elif timed_out:
                out["timed_out"] += 1
            else:
                out["failed"] += 1
        return out

    @staticmethod
    def match_window_order(total: int, n: int) -> List[int]:
        """Page indices of a match window over a `total`-page chapter: the page
        at the relative middle, then -1, +1, -2, +2 (most likely matches
        first). Shared by every match_fetch_window override, so all sources
        sample the same relative positions."""
        centre = int(round(0.5 * (total - 1)))
        order: List[int] = []
        for d in range(n):
            off = (d + 1) // 2 * (1 if d % 2 == 0 else -1)
            i = centre + off
            if 0 <= i < total and i not in order:
                order.append(i)
        return order

    def _fetch_probe_item_bytes_ex(self, item, scraper) -> Tuple[Optional[bytes], bool]:
        """Fetch image bytes for a single probe item, reporting timeout-vs-not.

        Returns ``(bytes_or_None, timed_out)``. ``timed_out`` is True ONLY when a
        network timeout (``_PROBE_TIMEOUT_EXCEPTIONS``) was raised — the match
        session counts those apart (slowness, not a quality signal). A
        definitive failure (non-image dict, HTTP >= 400, non-image bytes, or
        any non-timeout exception) returns ``(None, False)`` and counts as a
        failed fetch. Called by match_fetch_window (and its overrides); never
        raises.

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
                # Image Accept, same reason as the download path: a wrapper-page
                # response is a 200 that fails looks_like_real_image, which would
                # fail every page and rate the whole site unreachable on a
                # purely request-side mistake. Grep IMAGE_ACCEPT.
                response = scraper.get(
                    item, timeout=15, headers=IMAGE_ACCEPT_HEADERS
                )
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

    # Per-SOURCE wall-clock budget for the search's matched-page probe, read by
    # sites/match_session.py (grep PROBE_SOURCE_BUDGET_S there): counted while
    # at least one of the source's tasks is running; once spent, its unrun
    # windows are skipped like the phase deadline's, and the source is rated on
    # what it already served (slowness is never a quality verdict). Exists
    # because a host whose pages TIME OUT rather than fail fast costs ~40-55 s
    # per chapter under the search request shim (2 attempts x 20 s + a 15 s page
    # fetch), which used to hold the whole probe phase to its deadline
    # (search_orchestrator.PROBE_PHASE_DEADLINE_S) on every search. 120 s = half
    # the phase deadline. A handler whose fetches are slow by design may raise it.
    PROBE_SOURCE_BUDGET_S = 120.0

    # How many of one chapter's match-window pages match_fetch_window fetches at
    # once (module default _PROBE_BREADTH_CONCURRENCY, env
    # AIO_PROBE_BREADTH_CONCURRENCY). 1 = serial: a handler whose page fetches
    # must not run concurrently (e.g. a browser handler) sets 1; one whose CDN
    # is slow per request but tolerates bursts may raise it (atsumaru does).
    PROBE_BREADTH_CONCURRENCY: int = _PROBE_BREADTH_CONCURRENCY


__all__ = [
    "BaseSiteHandler",
    "SiteComicContext",
    "SearchHit",
]
