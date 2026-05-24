from __future__ import annotations

import logging
import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

from datetime import datetime, timezone

from .base import (
    BaseSiteHandler,
    IncompleteChapterError,
    SearchHit,
    SiteComicContext,
)
from ._publishers import lookup_publisher


_logger = logging.getLogger(__name__)


# Module-level fire-and-forget /api/report POSTs to the MD@H operator
# network. Without batching, the prior code spawned one daemon thread per
# page per node-attempt; a 200-page chapter through 4 swaps would spawn
# ~800 threads in <2s, putting unnecessary pressure on Python's thread
# allocator. A small bounded pool delivers the same end result
# (eventually fire each report) at a fraction of the overhead.
#
# Reports are cosmetic to the user — failure to deliver one doesn't
# affect the download. Sized at 4 because reports complete in ~50-200ms
# on healthy nodes and we never need more concurrency than the
# image-fetch pool (3 workers); 4 leaves slack for occasional bursts.
#
# Daemon Thread + Queue, NOT ThreadPoolExecutor. ThreadPoolExecutor
# workers register in concurrent.futures.thread._threads_queues and
# CPython's _python_exit (registered via threading._register_atexit, runs
# BEFORE the atexit module's hooks at interpreter teardown) joins them
# unconditionally — even after shutdown(wait=False). When api.mangadex
# .network is slow or its TLS conn trickles bytes, the requests.post
# timeout=10 doesn't actually fire (urllib3 resets the read timer per
# byte), so a stuck SSL recv pins Python at exit for minutes — visible
# to the user as the search UI hanging "forever" after run_search_mode
# has already printed its JSON to stdout. Diagnosed via py-spy dump on
# a real Toradora multi-source hang (2026-05-18): MainThread sat in
# _python_exit → join() while four md-report_* workers were all blocked
# in mangadex._post → ssl.recv_into.
#
# Daemon threads are skipped by _python_exit and die with the process,
# which is the correct semantics for fire-and-forget telemetry. Grep
# target if this ever needs adjusting: _REPORT_QUEUE / _report_worker.
#
# v8 hardening (2026-05-24):
#   - LAZY spawn via _ensure_report_workers() instead of eager import-time
#     for-loop. sites/__init__.py imports MangaDex eagerly so every
#     aio-dl run was paying for 4 idle daemons even when no MangaDex
#     chapter was downloaded. The lazy path defers thread creation
#     until the first _enqueue_report() call (i.e., the first MD page
#     fetch in the process), so search-only / non-MD runs don't pay
#     the cost.
#   - BOUNDED queue (maxsize=256). The previous queue.Queue() default
#     of unbounded grew unboundedly during the exact failure mode the
#     design comment above addresses: TLS recv stalls on api.mangadex
#     .network mean workers can't drain faster than producers enqueue,
#     and a 30-minute bulk download could accumulate tens of thousands
#     of (url, payload) tuples. 256 entries × ~few hundred bytes per
#     tuple = ~tens of kB ceiling; sustained burst >256/sec drops
#     newest, which is the right semantics for cosmetic telemetry.
#   - None-SENTINEL safe worker. The unpack `url, payload = q.get()`
#     would TypeError on a None enqueue (today nothing enqueues None
#     but a future shutdown sentinel would crash all 4 workers since
#     the outer try/except only wraps requests.post, not the unpack).
#     The worker now checks for None first and exits cleanly.
_REPORT_QUEUE: queue.Queue = queue.Queue(maxsize=256)
_REPORT_WORKERS_STARTED = False
_REPORT_WORKERS_LOCK = threading.Lock()


def _report_worker() -> None:
    """Drain (url, payload) tuples from _REPORT_QUEUE and POST them.
    Daemon thread target — exits with the interpreter. Errors are
    swallowed because reports are cosmetic to the user.

    None on the queue is treated as a shutdown sentinel and exits the
    worker cleanly. Today nothing enqueues None (workers are daemons
    and die with the interpreter), but the future-proofing matters: if
    a maintainer ever adds an explicit shutdown via `_REPORT_QUEUE.put(None)`
    the unpack would otherwise TypeError out and silently kill all 4
    workers since the outer try/except only wraps the POST itself.
    """
    while True:
        item = _REPORT_QUEUE.get()
        if item is None:
            return
        try:
            url, payload = item
        except (TypeError, ValueError):
            # Malformed enqueue — skip without dying.
            continue
        try:
            requests.post(url, json=payload, timeout=10.0)
        except Exception:
            pass


def _ensure_report_workers() -> None:
    """Lazy-start the 4 daemon workers on first enqueue.

    sites/__init__.py eagerly imports MangaDex so this module's globals
    initialize on every aio-dl run; spawning the daemons at import time
    paid for 4 threads in every search-only / non-MD process. Lazy
    initialization defers the cost to the first report.

    Double-checked locking: cheap fast-path read of the started flag
    without the lock, then re-check after acquiring it so concurrent
    first-callers don't all spawn redundant worker sets.
    """
    global _REPORT_WORKERS_STARTED
    if _REPORT_WORKERS_STARTED:
        return
    with _REPORT_WORKERS_LOCK:
        if _REPORT_WORKERS_STARTED:
            return
        for _i in range(4):
            threading.Thread(
                target=_report_worker,
                name=f"md-report_{_i}",
                daemon=True,
            ).start()
        _REPORT_WORKERS_STARTED = True


def _enqueue_report(url: str, payload: dict) -> None:
    """Best-effort report enqueue. Lazy-starts workers and drops the
    payload (instead of blocking) when the bounded queue is full —
    image-fetch threads must NOT stall on cosmetic telemetry. Same
    contract as the previous unbounded `_REPORT_QUEUE.put()`: the
    caller doesn't see failures."""
    _ensure_report_workers()
    try:
        _REPORT_QUEUE.put_nowait((url, payload))
    except queue.Full:
        # Telemetry is cosmetic; dropping is correct semantics under
        # back-pressure (the alternative — blocking image fetch — would
        # make the actual download slower). No log: silent drop matches
        # the worker's silent POST-failure handling.
        pass


class MangaDexSiteHandler(BaseSiteHandler):
    name = "mangadex"
    domains = ("mangadex.org", "www.mangadex.org")

    _API_BASE = "https://api.mangadex.org"
    _UPLOADS_BASE = "https://uploads.mangadex.org"
    # Per the MangaDex docs and `mansuf/mangadex-downloader`'s reference
    # implementation, MD@H operators want clients to POST fetch outcomes to
    # https://api.mangadex.network/report. Helps the network score node
    # health and rotate unhealthy nodes out of the pool. Cosmetic for one
    # client (no reliability degradation if you skip it) but cheap and
    # good citizenship.
    _REPORT_URL = "https://api.mangadex.network/report"
    # MD@H node-swap retry budget. Each swap re-calls /at-home/server to
    # reassign the chapter to a (probably-different) node, then retries
    # only the indices that failed against the prior node. Mansuf's loop
    # is unbounded; we cap at 4 to stay well under the 40 req/min budget
    # on /at-home/server even under heavy retry load.
    _MDAH_DATA_SWAPS = 4
    # After exhausting `data` mode, fall back to `data-saver` (smaller,
    # different file set, more likely cached on more nodes). 2 swaps
    # because if data-saver also fails, the chapter is genuinely missing.
    _MDAH_DATA_SAVER_SWAPS = 2
    # Per-host parallelism: Mihon caps the OkHttp client at rateLimit(3).
    # 3 in-flight requests against a single MD@H node is the sweet spot
    # before the node starts rate-limiting per-IP.
    _IMAGE_WORKERS = 3
    # Read timeout: image GETs against MD@H have to clear the CDN edge,
    # disk cache, and possibly the origin pull. 30s matches dl_image's
    # default and Mihon's setup.
    _IMAGE_TIMEOUT_S = 30.0
    # /at-home/server retry budget (total attempts, including the first).
    # The MangaDex assignment server itself — distinct from the MD@H operator
    # network — is occasionally flaky and will 500 on a chapter UUID one
    # second and 200 on the next. Without retries here the HTTPError would
    # propagate out of get_chapter_images, bypass the strict-wrapper multi-
    # source fallback in aio-dl.py (it only converts IncompleteChapterError),
    # and get silently absorbed by the chapter loop's bare `except Exception`
    # so the user sees "Chapter N (group)" with no follow-up line — same
    # symptom the user reported on Shuumatsu no Valkyrie 2026-05-16.
    # 3 attempts × bounded exponential backoff (~1s/2s/4s with jitter) caps
    # the worst-case wall-clock at ~8s before propagating; well under the
    # 30s downstream node-swap budget, so this layer only spends time when
    # it has a realistic chance of recovering.
    _AT_HOME_RETRIES = 3

    def configure_session(self, scraper, args) -> None:
        # Per api.mangadex.org/docs/2-limitations: User-Agent is mandatory and
        # must NOT be a browser-impersonation. Identify the tool. Mihon does
        # the same with its own AppName/Version string. Without a real UA the
        # API may rate-limit aggressively or refuse outright in the future.
        scraper.headers["User-Agent"] = (
            "AIO-Webtoon-Downloader/1.0 "
            "(+https://github.com/Thundia2/AIO-Webtoon-Downloader)"
        )

    # ------------------------------------------------------------------ helpers
    def _extract_manga_id(self, url: str) -> str:
        parsed = urlparse(url)
        segments = [seg for seg in parsed.path.split("/") if seg]
        uuid_pattern = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
        for segment in segments:
            if uuid_pattern.fullmatch(segment):
                return segment.lower()
        if uuid_pattern.fullmatch(parsed.path.strip("/")):
            return parsed.path.strip("/").lower()
        raise RuntimeError(
            "Unable to determine MangaDex ID. Use a URL of the form "
            "'https://mangadex.org/title/<uuid>/...'."
        )

    def _request(
        self,
        scraper,
        make_request,
        endpoint: str,
        params: Optional[Dict[str, str]] = None,
    ):
        url = f"{self._API_BASE}{endpoint}"
        resp = make_request(url if not params else (url, params), scraper)
        return resp.json()

    def _title_from_attributes(self, attributes: Dict) -> str:
        title = attributes.get("title") or {}
        if isinstance(title, dict):
            for key in ("en", "ja", "jp", "ko"):
                if key in title and title[key]:
                    return title[key]
            if title:
                return next(iter(title.values()))
        return attributes.get("altTitles", [{}])[0].get("en") or "Unknown Manga"

    def _description_from_attributes(self, attributes: Dict) -> Optional[str]:
        description = attributes.get("description") or {}
        if isinstance(description, dict):
            for key in ("en", "ja", "jp", "ko"):
                if description.get(key):
                    return description[key]
            if description:
                return next(iter(description.values()))
        return None

    def _cover_url(self, manga_id: str, relationships: Iterable[Dict]) -> Optional[str]:
        """Return the cover URL, preferring the MangaDex-hosted 512px thumbnail
        variant (`{filename}.512.jpg`).

        The raw cover URL works too, but the 512px variant is purpose-built
        for thumbnails: it's typically <50 KB (vs 1-3 MB for the original
        upload), smaller cache files, and CDN-fronted on
        ``uploads.mangadex.org`` which makes it more reliably served. Mihon
        and HakuNeko both prefer the 512px variant for the same reasons.
        Cross-file: the Library tab's cover-cache fetch (electron/library.js
        :downloadCoverImage) also benefits — bare ``https.get`` against the
        thumbnail variant is more permissive than against the raw upload.
        """
        for rel in relationships:
            if rel.get("type") == "cover_art" and rel.get("attributes"):
                file_name = rel["attributes"].get("fileName")
                if file_name:
                    return f"{self._UPLOADS_BASE}/covers/{manga_id}/{file_name}.512.jpg"
        return None

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(
        self,
        url: str,
        scraper,
        make_request,
    ) -> SiteComicContext:
        manga_id = self._extract_manga_id(url)
        params = [
            ("includes[]", "author"),
            ("includes[]", "artist"),
            ("includes[]", "cover_art"),
        ]
        resp = make_request(
            f"{self._API_BASE}/manga/{manga_id}?{'&'.join(f'{k}={v}' for k, v in params)}",
            scraper,
        )
        data = resp.json().get("data")
        if not data:
            raise RuntimeError("MangaDex API did not return data for this ID.")
        attributes = data.get("attributes") or {}
        relationships = data.get("relationships") or []

        title = self._title_from_attributes(attributes)
        description = self._description_from_attributes(attributes)

        authors = []
        artists = []
        for rel in relationships:
            if rel.get("type") == "author" and rel.get("attributes"):
                name = rel["attributes"].get("name")
                if name:
                    authors.append(name)
            if rel.get("type") == "artist" and rel.get("attributes"):
                name = rel["attributes"].get("name")
                if name:
                    artists.append(name)

        comic: Dict[str, object] = {
            "hid": manga_id,
            "title": title,
            "desc": description,
            "cover": self._cover_url(manga_id, relationships),
            "authors": authors or artists,
            "artists": artists or authors,
            # NOTE: DMCA-affected titles (e.g. "Na Honjaman Level-Up",
            # 32d76d19-8a05-4db0-9fc2-e0b0648fe9d0) return an empty
            # attributes.tags array from MangaDex's API — the takedown
            # response strips the tag-relationship along with chapter
            # availability. Detected upstream via the dmca_likely flag
            # (chapter_count_hint vs actual_chapter_count). For
            # Komikku-mode details.json, the genre array will be empty
            # on those titles. Not a parser bug; the data is hollowed at
            # the source. See dry_run_komikku_findings.md §C.
            "genres": [tag.get("name") for tag in attributes.get("tags", []) if tag.get("name")],
            "_manga_id": manga_id,
        }

        status = attributes.get("status")
        if isinstance(status, str) and status:
            comic["status"] = status

        # attributes.altTitles is a list of single-key {lang_code: title} dicts.
        # Collect every string value; spec-pinned languages (en/ja/etc.) come
        # first because the API returns them in catalog order.
        alt_titles: List[str] = []
        for entry in attributes.get("altTitles") or []:
            if isinstance(entry, dict):
                for value in entry.values():
                    if isinstance(value, str) and value and value not in alt_titles:
                        alt_titles.append(value)
        if alt_titles:
            comic["alt_names"] = alt_titles

        year = attributes.get("year")
        if isinstance(year, int) and year > 0:
            comic["year"] = year

        return SiteComicContext(comic=comic, title=title, identifier=manga_id, soup=None)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        manga_id = context.comic.get("_manga_id") or context.identifier
        params = {
            "manga": manga_id,
            "limit": "100",
            "offset": "0",
            "order[chapter]": "asc",
            "includes[]": ["scanlation_group", "user"],
            "contentRating[]": ["safe", "suggestive", "erotica"],
        }
        languages = []
        if language and language.lower() != "all":
            languages = [lang.strip() for lang in language.split(",") if lang.strip()]
        if languages:
            params["translatedLanguage[]"] = languages

        chapters: List[Dict] = []
        offset = 0
        while True:
            query_params = []
            for key, value in params.items():
                if isinstance(value, list):
                    for entry in value:
                        query_params.append((key, entry))
                else:
                    query_params.append((key, value))
            query_params = [(k, str(v)) for k, v in query_params]
            query_params.append(("offset", str(offset)))
            url = f"{self._API_BASE}/chapter?" + "&".join(f"{k}={v}" for k, v in query_params)
            resp = make_request(url, scraper).json()
            data = resp.get("data", [])
            total = resp.get("total", 0)
            for chapter in data:
                attr = chapter.get("attributes") or {}
                relationships = chapter.get("relationships") or []
                chapter_id = chapter.get("id")
                # Post-2024 DMCA flag: chapters whose images were nuked but
                # whose metadata is kept for chapter-discussion continuity
                # carry `isUnavailable: true`. They will NEVER resolve to
                # an /at-home/server response with images, so skip them
                # outright — saves us the wasted API call + the resulting
                # ChapterSkippedError + alt-source fallback churn. Mihon's
                # MangaDexHelper uses this same flag to prefix unavailable
                # chapters with "[Unavailable]"; we just drop them since
                # the multi-source orchestrator's job is to find a working
                # alternative anyway.
                if attr.get("isUnavailable") is True:
                    continue
                group_name = None
                group_id = None
                for rel in relationships:
                    if rel.get("type") == "scanlation_group":
                        group_id = rel.get("id")
                        group_name = rel.get("attributes", {}).get("name")
                        break
                # Phase 4c is_official annotation: match scanlation_group
                # against sites/official_publishers.json (UUID first, name
                # alias fallback). When True, the chapter_merger will rank
                # this source first within a chapter row, and downstream
                # JSON output exposes it for UI badges. canonical publisher
                # name from the catalog (e.g. "MangaPlus" not "MangaPlus by
                # Shueisha") is used as `publisher` so cross-site dedupe by
                # publisher works even when group_name strings drift.
                is_official, publisher_canonical = lookup_publisher(group_id, group_name)
                chapters.append(
                    {
                        "hid": chapter_id,
                        "chap": attr.get("chapter") or attr.get("title") or attr.get("volume"),
                        "title": attr.get("title"),
                        "url": chapter_id,
                        "group_name": group_name,
                        "language": attr.get("translatedLanguage"),
                        "uploaded": self._parse_timestamp(attr.get("publishAt")),
                        "is_official": is_official,
                        "publisher": publisher_canonical,
                    }
                )
            offset += len(data)
            if offset >= total or not data:
                break
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        group = chapter_version.get("group_name")
        return group if isinstance(group, str) and group else None

    # ---------------------------------------------- chapter image download
    def _fetch_at_home_assignment(
        self,
        scraper,
        chapter_id: str,
    ) -> Dict:
        """Get a fresh MD@H baseUrl + filename list from /at-home/server.

        Always passes ``forcePort443=true`` so the assigned node listens on
        the standard HTTPS port (corp firewalls / mobile carriers often drop
        the random high ports MD@H operators default to). Always sets
        Cache-Control: no-cache so retries get a freshly-load-balanced node
        instead of the same broken assignment from a transparent cache.

        Mihon mirrors this with ``CacheControl.FORCE_NETWORK`` on its retry
        path (MangaDexHelper.kt:213-254); the ``forcePort443`` query is the
        same flag that Mihon exposes as ``STANDARD_HTTPS_PORT_PREF``.

        Transient-5xx retry policy: the MangaDex assignment server (distinct
        from the MD@H operator network) is occasionally flaky and will 500
        on a given chapter UUID one moment and 200 the next. We retry up to
        ``_AT_HOME_RETRIES`` total attempts with bounded exponential backoff
        (~1s / 2s / 4s with jitter) on 5xx responses and network exceptions.
        4xx responses fail fast (a 404 here means the chapter genuinely does
        not exist at MD@H; retrying is pointless and would burn the API's
        40 req/min budget per IP). The downstream node-swap loop in
        get_chapter_images is for a DIFFERENT failure mode — bad bytes from
        a single MD@H node — so the two retry layers serve disjoint causes
        and don't compound.

        Raises RuntimeError on malformed response. The caller catches and
        wraps that into IncompleteChapterError when no successful baseUrl
        is ever obtained. requests.HTTPError on 4xx / final-attempt 5xx
        propagates; aio-dl.py:_process_chapter_impl's call-site catches it
        and converts to ChapterSkippedError so multi-source fallback engages.
        """
        url = f"{self._API_BASE}/at-home/server/{chapter_id}?forcePort443=true"
        last_exc: Optional[Exception] = None
        resp: Optional[requests.Response] = None
        for attempt in range(self._AT_HOME_RETRIES):
            try:
                resp = scraper.get(
                    url,
                    headers={"Cache-Control": "no-cache, no-store"},
                    timeout=30.0,
                )
            except requests.RequestException as exc:
                # Network error / timeout / SSL error — treat as transient.
                # We retry up to the budget and propagate the last one on
                # exhaustion. Re-binding resp to None avoids reusing a
                # response object from a prior loop iteration.
                last_exc = exc
                resp = None
                if attempt < self._AT_HOME_RETRIES - 1:
                    wait_s = min(2.0 ** attempt, 5.0) * random.uniform(0.85, 1.15)
                    _logger.info(
                        "[mangadex] /at-home/server %s network error on "
                        "attempt %d/%d (%s); retrying in %.1fs",
                        chapter_id, attempt + 1, self._AT_HOME_RETRIES, exc, wait_s,
                    )
                    time.sleep(wait_s)
                    continue
                raise
            status = getattr(resp, "status_code", None) or 0
            if status >= 500 and attempt < self._AT_HOME_RETRIES - 1:
                wait_s = min(2.0 ** attempt, 5.0) * random.uniform(0.85, 1.15)
                _logger.info(
                    "[mangadex] /at-home/server %s returned %d on "
                    "attempt %d/%d; retrying in %.1fs",
                    chapter_id, status, attempt + 1, self._AT_HOME_RETRIES, wait_s,
                )
                time.sleep(wait_s)
                continue
            # Success path (2xx) or a non-retryable response (4xx) or a final-
            # attempt 5xx — break out and let raise_for_status decide below.
            break
        if resp is None:
            # The loop terminates with resp set unless the very last attempt
            # raised a RequestException without a Response object (which we
            # re-raised above). Defensive: surfaces the network-exception
            # path explicitly rather than crashing on a None.raise_for_status.
            if last_exc is not None:
                raise last_exc
            raise RuntimeError(
                "MangaDex _fetch_at_home_assignment exhausted retries "
                "without obtaining a response"
            )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"MangaDex /at-home/server returned non-JSON: {exc}"
            ) from exc
        base_url = data.get("baseUrl")
        chapter_data = data.get("chapter") or {}
        file_hash = chapter_data.get("hash")
        data_filenames = chapter_data.get("data") or []
        saver_filenames = chapter_data.get("dataSaver") or []
        if not base_url or not file_hash or not data_filenames:
            raise RuntimeError(
                "MangaDex /at-home/server returned incomplete payload "
                f"(baseUrl={'set' if base_url else 'missing'}, "
                f"hash={'set' if file_hash else 'missing'}, "
                f"data_count={len(data_filenames)})"
            )
        return {
            "base_url": base_url,
            "file_hash": file_hash,
            "data_filenames": list(data_filenames),
            # dataSaver may be empty on very old chapters. We keep it empty
            # rather than substituting data_filenames because data and
            # data-saver use DIFFERENT per-page filename hashes — `data`
            # uses ``<n>-<hash>.png`` while `dataSaver` uses ``<n>-<hash2>.jpg``.
            # Using data_filenames in the /data-saver/ URL path would 404 every
            # page. The caller (get_chapter_images) checks for emptiness here
            # and forces saver_swaps_left=0 so the data-saver retry phase is
            # skipped entirely when there's nothing to chew on.
            "saver_filenames": list(saver_filenames),
        }

    def _fetch_image_blob(
        self,
        scraper,
        url: str,
    ) -> Tuple[bool, Optional[bytes], int, int, bool, Optional[int]]:
        """Fetch a single MD@H image URL.

        Returns ``(success, blob, bytes_received, duration_ms, cached, status)``.
        success=True iff HTTP 200 with non-empty body. cached is True iff the
        ``X-Cache`` response header begins with "HIT" (per MD@H operator
        convention) — propagated to the /api/report payload. duration_ms is
        wall-clock from request start to response complete.

        Errors are swallowed and reported as success=False; the caller
        decides whether to retry on a different node. The blob is None on
        failure regardless.
        """
        start = time.monotonic()
        blob: Optional[bytes] = None
        bytes_received = 0
        cached = False
        status: Optional[int] = None
        success = False
        try:
            resp = scraper.get(url, timeout=self._IMAGE_TIMEOUT_S, stream=True)
            status = resp.status_code
            cached = (resp.headers.get("X-Cache", "") or "").upper().startswith("HIT")
            if status == 200:
                blob = resp.content
                bytes_received = len(blob)
                success = bytes_received > 0
                if not success:
                    blob = None
            else:
                # Drain so the connection can be reused. .content already drains.
                bytes_received = len(resp.content or b"")
        except Exception:
            # Network error, timeout, SSL issue — treat as a node-side failure.
            # Caller will swap to a fresh node on the next iteration.
            pass
        duration_ms = int((time.monotonic() - start) * 1000)
        return success, blob, bytes_received, duration_ms, cached, status

    def _report_to_mdah(
        self,
        url: str,
        success: bool,
        bytes_received: int,
        duration_ms: int,
        cached: bool,
    ) -> None:
        """Fire-and-forget report to https://api.mangadex.network/report.

        MD@H operator network uses these to score node health and rotate
        unhealthy nodes out of the pool. Skips URLs on uploads.mangadex.org
        — only the .mangadex.network nodes accept reports (per mansuf's
        downloader.py:309-338 comment "domain that is not from
        mangadex.network are not allowed to report").

        Enqueued onto the module-level _REPORT_QUEUE (4 daemon workers)
        instead of spawning a fresh thread per page — the prior approach
        could spawn ~800 threads in <2s on a long chapter through multiple
        swaps. Errors are swallowed by _report_worker; reporting is
        cosmetic.
        """
        if "mangadex.network" not in url:
            return
        payload = {
            "url": url,
            "success": bool(success),
            "bytes": int(bytes_received),
            "duration": int(duration_ms),
            "cached": bool(cached),
        }
        # Best-effort fire-and-forget via _enqueue_report (v8): the
        # helper lazy-starts the worker daemons on first call and drops
        # silently when the bounded queue (maxsize=256) is full. Daemon
        # workers die with the interpreter, so a slow report in flight
        # at process exit gets dropped (not joined). Grep target:
        # _enqueue_report / _ensure_report_workers in this file.
        _enqueue_report(self._REPORT_URL, payload)

    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,
    ) -> List[Dict]:
        """Resilient MangaDex chapter download.

        Replaces the legacy "return URL list, let dl_image handle download"
        flow with a handler-side pipeline that knows how to recover from
        per-node MD@H failures. The reason: MD@H assigns a SINGLE node per
        chapter via /at-home/server, and that node's content cache is
        independent of every other node's. When the assigned node 404s on
        some tiles (cmdxd98sb0x3yprd.mangadex.network during user reports
        2026-05-08), dl_image's URL-variant retry doesn't help — every
        variant points at the same broken node. The fix is to re-call
        /at-home/server, get a (probably-different) node, and retry only
        the failed indices.

        Pipeline:
          1. Fetch initial /at-home/server (forcePort443=true).
          2. Download all pages in a 3-worker thread pool. Each fetch is
             reported to /api/report (fire-and-forget background thread).
          3. If any page failed, re-call /at-home/server for a fresh node
             and retry ONLY the failed indices. Up to 4 swaps in `data`
             mode.
          4. After 4 unsuccessful data swaps, switch to `data-saver` mode
             (smaller, different file set, more likely cached on more
             nodes). Up to 2 more swaps.
          5. Return binary_image entries with pre-fetched bytes (extension
             auto-sniffed from blob magic by aio-dl.py's Phase 1).
          6. Raise IncompleteChapterError if pages remain missing — the
             chapter loop converts this to ChapterSkippedError, triggering
             the multi-source / inline-retry / hard-abort machinery.

        Cross-file:
          - Caller is aio-dl.py:_process_chapter_impl Phase 1 (around line
            4453). It treats binary_image entries as "already on disk; no
            URL to fetch" so dl_image's parallel pool is bypassed entirely
            for MangaDex chapters.
          - IncompleteChapterError lives in sites/base.py — defined there
            so any handler that does its own retry policy can signal
            partial completion without needing aio-dl.py imports.
          - /api/report endpoint and payload shape from mansuf/mangadex-
            downloader/downloader.py and the MangaDex docs (api.mangadex
            .org/docs/04-chapter/retrieving-chapter/).
        """
        chapter_id = chapter.get("url") or chapter.get("hid")
        if not chapter_id:
            raise RuntimeError("MangaDex chapter missing identifier.")

        assignment = self._fetch_at_home_assignment(scraper, str(chapter_id))
        base_url = assignment["base_url"]
        file_hash = assignment["file_hash"]
        data_filenames: List[str] = assignment["data_filenames"]
        saver_filenames: List[str] = assignment["saver_filenames"]

        n_pages = len(data_filenames)
        if n_pages == 0:
            raise RuntimeError("MangaDex chapter has no pages.")

        # idx → blob bytes for successfully-downloaded pages. We pull from
        # this dict at the end to assemble binary_image entries in original
        # page order, regardless of completion order across retries.
        blobs: Dict[int, bytes] = {}
        # idx → real filename used (data vs data-saver path) — used to sniff
        # the extension when building the binary_image entry. data and
        # dataSaver have *different* filenames per page; we have to track
        # which mode produced each blob.
        used_filename: Dict[int, str] = {}
        pending = set(range(n_pages))
        mode = "data"
        data_swaps_left = self._MDAH_DATA_SWAPS
        saver_swaps_left = self._MDAH_DATA_SAVER_SWAPS
        # When dataSaver is empty on the assignment (very old chapters that
        # never had a saver-quality variant uploaded), the API would 404 every
        # /data-saver/ URL because we have no real filenames for that path.
        # _fetch_at_home_assignment intentionally returns saver_filenames=[]
        # in this case (rather than substituting data_filenames, whose hashes
        # don't apply to the saver path). Disable the saver phase up-front so
        # the loop bails cleanly when data swaps are exhausted instead of
        # building 404-bound URLs from a placeholder filename list.
        if not saver_filenames:
            saver_swaps_left = 0
            _logger.info(
                "[mangadex] chapter %s has no dataSaver variant; "
                "data-saver fallback unavailable",
                chapter_id,
            )

        # Pool placement: outside the while loop so node-swap iterations
        # don't pay the pool startup/teardown cost (~10ms each on Windows,
        # plus thread allocation churn). Worker count is bounded by
        # IMAGE_WORKERS regardless of swap; the pool queues submissions
        # when N pending > workers. 3 workers matches Mihon's rateLimit(3)
        # — enough to saturate a healthy node's per-IP rate limit, not so
        # many that we trigger MD@H's per-IP throttle.
        pool_workers = max(1, min(self._IMAGE_WORKERS, n_pages))
        with ThreadPoolExecutor(
            max_workers=pool_workers, thread_name_prefix="md-fetch"
        ) as pool:
            while pending:
                # Build URLs for the current pending set + mode.
                if mode == "data":
                    path_seg = "data"
                    names = data_filenames
                else:
                    path_seg = "data-saver"
                    names = saver_filenames
                url_by_idx: Dict[int, str] = {
                    idx: f"{base_url}/{path_seg}/{file_hash}/{names[idx]}"
                    for idx in pending
                }

                # Submit + wait on this batch. Pool worker cap caps in-flight
                # concurrency; submissions beyond worker count queue inside
                # the executor.
                fut_to_idx = {
                    pool.submit(self._fetch_image_blob, scraper, url): idx
                    for idx, url in url_by_idx.items()
                }
                for fut in as_completed(fut_to_idx):
                    idx = fut_to_idx[fut]
                    url = url_by_idx[idx]
                    try:
                        success, blob, bytes_recv, dur_ms, cached, status = fut.result()
                    except Exception:
                        success = False
                        blob = None
                        bytes_recv = 0
                        dur_ms = 0
                        cached = False
                        status = None

                    self._report_to_mdah(url, success, bytes_recv, dur_ms, cached)

                    if success and blob:
                        blobs[idx] = blob
                        used_filename[idx] = names[idx]
                        pending.discard(idx)
                    else:
                        _logger.debug(
                            "[mangadex] page %d/%d failed status=%s on %s",
                            idx + 1, n_pages, status,
                            urlparse(url).netloc,
                        )

                if not pending:
                    break

                # Some indices still failing — swap MD@H node (or fall back
                # to data-saver). The chapter.hash and filenames don't
                # change between swaps; only the baseUrl rotates to a
                # different node.
                failed_count = len(pending)
                failed_host = urlparse(base_url).netloc
                if data_swaps_left > 0 and mode == "data":
                    data_swaps_left -= 1
                    _logger.info(
                        "[mangadex] %d/%d pages failed on %s; "
                        "swapping MD@H node (data swaps remaining: %d)",
                        failed_count, n_pages, failed_host, data_swaps_left,
                    )
                    try:
                        assignment = self._fetch_at_home_assignment(scraper, str(chapter_id))
                    except Exception as exc:
                        _logger.warning(
                            "[mangadex] /at-home/server re-fetch failed: %s; "
                            "falling back to data-saver immediately",
                            exc,
                        )
                        # Force the mode-switch below by zeroing data_swaps.
                        data_swaps_left = 0
                        continue
                    base_url = assignment["base_url"]
                    # Re-bind hash/filenames. They typically don't change
                    # between swaps for the same chapter, but a stale
                    # assignment could in principle reset on the server
                    # side; rebinding keeps us consistent with the latest
                    # API response.
                    file_hash = assignment["file_hash"]
                    data_filenames = assignment["data_filenames"]
                    saver_filenames = assignment["saver_filenames"]
                    continue

                if mode == "data":
                    # Exhausted data-mode swaps. Fall back to data-saver —
                    # but only when the chapter actually has dataSaver
                    # filenames. Old chapters can have an empty dataSaver
                    # list (we no longer substitute data_filenames as a
                    # placeholder because the per-page hashes differ
                    # between modes). When saver is unavailable, bail
                    # with whatever pages we managed instead of building
                    # 404-bound URLs from an empty filename list.
                    if not saver_filenames:
                        _logger.info(
                            "[mangadex] %d/%d pages still missing after %d data-mode "
                            "swaps; chapter has no dataSaver variant. Bailing.",
                            failed_count, n_pages, self._MDAH_DATA_SWAPS,
                        )
                        break
                    # Keep current baseUrl for the first saver attempt (the
                    # failed pages may simply not be on this node in EITHER
                    # mode, but it's cheap to test before swapping again).
                    _logger.info(
                        "[mangadex] %d/%d pages still missing after %d data-mode "
                        "swaps; falling back to data-saver",
                        failed_count, n_pages, self._MDAH_DATA_SWAPS,
                    )
                    mode = "data-saver"
                    continue

                if saver_swaps_left > 0:
                    saver_swaps_left -= 1
                    _logger.info(
                        "[mangadex] data-saver: %d/%d pages still missing; "
                        "swapping MD@H node (saver swaps remaining: %d)",
                        failed_count, n_pages, saver_swaps_left,
                    )
                    try:
                        assignment = self._fetch_at_home_assignment(scraper, str(chapter_id))
                    except Exception:
                        break  # Out of options; fall through to incomplete-error.
                    base_url = assignment["base_url"]
                    file_hash = assignment["file_hash"]
                    data_filenames = assignment["data_filenames"]
                    saver_filenames = assignment["saver_filenames"]
                    continue

                # All swaps exhausted with pages still pending. Bail.
                break

        if pending:
            host = urlparse(base_url).netloc if base_url else "mangadex.network"
            raise IncompleteChapterError(
                pages_ok=len(blobs),
                pages_total=n_pages,
                host=host,
                reason="mdah_persistent_failure_after_swaps",
            )

        # Assemble binary_image entries in original page order. The blob's
        # extension is sniffed by aio-dl.py:_sniff_image_extension when the
        # entry is written to disk (Phase A 2026-05-07 sniff infra). We
        # carry an explicit `extension` so the file lands with the right
        # suffix even on tiny blobs the magic sniffer would punt on.
        entries: List[Dict] = []
        for idx in range(n_pages):
            blob = blobs.get(idx)
            if not blob:
                continue  # shouldn't happen given the pending check above
            filename = used_filename.get(idx, data_filenames[idx])
            ext = self._extension_from_filename(filename)
            entries.append({
                "type": "binary_image",
                "data": blob,
                "extension": ext,
                # Page indices are 1-based in MangaDex display. Filename
                # placeholder mirrors what dl_image would have produced
                # (n_<page>_<width>.ext is the chapter loop's pattern; we
                # only set the *base* name and let aio-dl.py re-prefix).
                "name": f"{idx + 1:04d}{ext}",
            })
        return entries

    @staticmethod
    def _extension_from_filename(filename: str) -> str:
        """Extract the trailing extension (with dot) from an MD@H filename
        like ``1-abc123def.png``. Falls back to ``.jpg`` if the filename
        doesn't carry a recognizable extension — better to write something
        than to drop the page entirely.
        """
        if not filename:
            return ".jpg"
        dot = filename.rfind(".")
        if dot < 0 or dot == len(filename) - 1:
            return ".jpg"
        ext = filename[dot:].lower()
        # Sanity-clamp to known image formats; anything weird → .jpg.
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
            return ".jpg"
        return ext


    def _parse_timestamp(self, value: Optional[str]) -> int:
        if not value:
            return 0
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0

    # ----------------------------------------------------------------- search
    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        """Search MangaDex via /manga?title=… (public REST API, no auth).

        Cross-file note: the URL we return must resolve back to this handler
        via sites.get_handler_for_url() — we use https://mangadex.org/title/<uuid>
        because _extract_manga_id() in fetch_comic_context already accepts that
        shape (mangadex.py:28).
        """
        clean = (query or "").strip()
        if not clean:
            return []

        params = [
            ("title", clean),
            ("limit", str(max(1, min(int(limit or 20), 100)))),
            ("order[relevance]", "desc"),
            ("includes[]", "cover_art"),
            ("contentRating[]", "safe"),
            ("contentRating[]", "suggestive"),
            ("contentRating[]", "erotica"),
        ]
        # If user asked for a specific language, narrow to titles that have
        # at least one chapter in it. 'en' is the most useful default; 'all'
        # disables the filter.
        if language and language.lower() != "all":
            for lang in (l.strip() for l in language.split(",")):
                if lang:
                    params.append(("availableTranslatedLanguage[]", lang))

        url = f"{self._API_BASE}/manga?" + "&".join(
            f"{k}={quote(str(v), safe='')}" for k, v in params
        )

        # HTTP failures propagate; orchestrator records dead host in the cache.
        # JSON-parse failures are treated as empty results (the API rarely
        # returns invalid JSON for valid HTTP responses, and we don't want a
        # one-off content-type glitch to suppress this host for an hour).
        resp = make_request(url, scraper)
        try:
            data = resp.json()
        except Exception:
            return []

        hits: List[SearchHit] = []
        results = data.get("data") or []
        total = max(1, len(results))
        for idx, manga in enumerate(results):
            attributes = manga.get("attributes") or {}
            relationships = manga.get("relationships") or []
            manga_id = manga.get("id")
            if not manga_id:
                continue

            title = self._title_from_attributes(attributes)
            alt_titles = self._collect_alt_titles(attributes)
            year = attributes.get("year") if isinstance(attributes.get("year"), int) else None
            cover = self._cover_url(manga_id, relationships)

            # raw_score: position-based; first result is 1.0, last is ~0.05.
            # The orchestrator computes its own title-match score with rapidfuzz,
            # so this is just a stable fallback when no other signal is present.
            raw_score = max(0.05, 1.0 - (idx / total))

            chapter_count_hint = (
                self._safe_int(attributes.get("lastChapter"))
                if attributes.get("lastChapter") else None
            )

            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=f"https://mangadex.org/title/{manga_id}",
                    cover=cover,
                    alt_titles=alt_titles,
                    year=year,
                    language=None,
                    chapter_count_hint=chapter_count_hint,
                    raw_score=raw_score,
                )
            )

        # Per-hit DMCA-detection probe. The May 15 2025 joint DMCA hollowed out
        # MangaDex's accessible EN catalog for many licensed series — metadata
        # still claims 96 chapters but only 1 is fetchable. Compare metadata
        # claim (attributes.lastChapter, captured above as chapter_count_hint)
        # to the actual fetchable EN chapter count via the chapter API's
        # `total` field (limit=1 returns just the count, not the list).
        # When the gap is substantial, set dmca_likely=True so the orchestrator
        # surfaces it in the JSON output and Phase 4 multi-source can demote
        # the source for that series.
        #
        # Costs ~1 extra HTTP call per hit (~50-200ms each). Wrapped in
        # best-effort try/except — a failed probe just leaves the field None.
        languages: List[str] = []
        if language and language.lower() != "all":
            languages = [l.strip() for l in language.split(",") if l.strip()]

        for hit in hits:
            manga_id = hit.url.rsplit("/", 1)[-1]
            try:
                params = [("manga", manga_id), ("limit", "1")]
                for lang in languages:
                    params.append(("translatedLanguage[]", lang))
                ch_url = f"{self._API_BASE}/chapter?" + "&".join(
                    f"{k}={quote(str(v), safe='')}" for k, v in params
                )
                ch_resp = make_request(ch_url, scraper)
                ch_data = ch_resp.json()
                actual_total = ch_data.get("total")
                if isinstance(actual_total, int):
                    hit.actual_chapter_count = actual_total
                    if (
                        hit.chapter_count_hint
                        and hit.chapter_count_hint > 5
                        and actual_total < hit.chapter_count_hint * 0.3
                    ):
                        hit.dmca_likely = True
            except Exception:
                # Best-effort: leave fields unset. The orchestrator handles
                # absent fields gracefully (no warning surfaced).
                continue

        return hits

    def _collect_alt_titles(self, attributes: Dict) -> List[str]:
        """Collect every alt-title across languages plus the non-primary
        entries from the 'title' field. rapidfuzz token_set_ratio works best
        when fed the romaji/japanese/korean variants alongside the English
        title (e.g. 'Frieren' matching 'Sousou no Frieren')."""
        out: List[str] = []
        title_dict = attributes.get("title") or {}
        if isinstance(title_dict, dict):
            for v in title_dict.values():
                if isinstance(v, str) and v:
                    out.append(v)
        for entry in attributes.get("altTitles") or []:
            if not isinstance(entry, dict):
                continue
            for v in entry.values():
                if isinstance(v, str) and v:
                    out.append(v)
        # Dedupe preserving order
        seen: set = set()
        unique: List[str] = []
        for t in out:
            key = t.lower()
            if key not in seen:
                seen.add(key)
                unique.append(t)
        return unique

    @staticmethod
    def _safe_int(value) -> Optional[int]:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None


__all__ = ["MangaDexSiteHandler"]
