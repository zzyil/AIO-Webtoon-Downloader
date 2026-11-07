"""CLI glue for cross-site search mode.

Imported by aio-dl.py when --search is passed. Kept as a separate module so
the 4794-line main script doesn't grow unbounded with the search feature.

Owns:
  - building a fresh, handler-appropriate scraper for each search-capable
    handler (mirrors aio-dl.py's main() scraper creation around line 3211).
    Each handler gets its own scraper so headers set by configure_session
    don't collide across handlers (mangafire.py uses .update() which clobbers
    a shared session).
  - delegating to sites.search_orchestrator.search_all().
  - emitting JSON when --search is the only mode, OR returning the winner
    URL when --auto-pick is set.
  - the multi-source discovery result aio-dl.py consumes (alternatives dict,
    catalog, resume-cache sources), built in ONE place
    (build_alternatives_from_records) for all four entry points: direct-URL
    search, prefetched JSON, resume cache, and --search --auto-pick's own
    fetched lists (alternatives_from_search_state).

Cross-file:
  - sites/search_orchestrator.py — the actual ranking logic.
  - aio-dl.py:make_request — passed in so retries, cooldowns, and the
    cross-process coordinator integrate transparently.
  - aio-dl.py:_record_rate_limit — used by ProbeFailureCache.record_cooldown
    so a host that times out twice in 1h is suppressed via the same machinery
    that handles rate-limits, not a parallel system.
"""

from __future__ import annotations

import json
import sys

# Force UTF-8 on stdio before anything prints. When Electron spawns this
# script (UI-source/electron/searcher.js:160), stdio is a pipe, not a TTY,
# so Python falls back to locale.getpreferredencoding(False) → cp1252 on
# default Western Windows (ACP=1252). Log decoration uses → ─ — × ≥ which
# cp1252 can't encode, crashing the run with UnicodeEncodeError.
# errors='replace' keeps it crash-proof for any future char.
# Mirrors aio-dl.py top-of-file block. Grep: UnicodeEncodeError reconfigure
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import time
from typing import Any, Callable, List, Optional

import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from sites import get_handler_by_name
from sites.base import SearchHit, readable_chapters
from sites.chapter_merger import _classify_main_chapters, align_chapter_lists
from sites.fetch_memo import FetchMemo
from sites.http_session import create_http_session
from sites.search_orchestrator import (
    DEFAULT_MIN_MATCH,
    DEFAULT_PER_SITE_TIMEOUT_S,
    ImageQualityCache,
    ProbeFailureCache,
    best_title_of,
    search_all,
    source_rank_key,
)


# Host suffix → handler name for URL-mode --search. The conceptual boundary is
# "sites whose keyword search is unreliable or dead," so pasting a series URL is
# the only reliable way to force the site into cross-site comparison:
#   - mangafire: typeahead is intermittent.
#   - comix: the keyword API is signed + encrypted (403 "Missing token."), so
#     ComixSiteHandler.search() returns []; a /title/ URL resolves via the SSR
#     #initial-data blob (plain HTTP, no browser). See sites/comix.py.
# Add a host here (and, if its get_chapters is expensive, a count branch below)
# to extend the feature.
_URL_SEED_HOSTS = {
    "mangafire.to": "mangafire",
    "comix.to": "comix",
}


def parse_disable_sites(args) -> set:
    """Parse the --disable-sites comma list off `args` into a lowercased set of
    handler names. The user's opted-out (unreachable/slow) sites; fed to
    search_all(exclude_sites=) here and to aio-dl.py's multi-source alternatives
    guard-filter. Tolerant of None / blanks / stray whitespace. Matched against
    handler.name (the JSON 'site' field) everywhere."""
    raw = getattr(args, "disable_sites", None) or ""
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _try_extract_seed_hit(query: str, fetch_memo=None) -> Optional[SearchHit]:
    """If `query` is a URL for a URL-seedable site, scrape it into a SearchHit.

    The "URL-mode --search" feature (originally MangaFire-only) exists for sites
    whose keyword search can't be relied on: paste a series URL and that site is
    GUARANTEED to participate in cross-site comparison. Supported hosts live in
    _URL_SEED_HOSTS; other sites' search is reliable enough that URL-mode adds
    no value there.

    ``fetch_memo`` (sites/fetch_memo.py, 2026-07-12): when provided, the
    context (and, for mangafire, the chapter list) this resolution already
    fetched is pre-registered so the probe and winner-fetch phases reuse
    it instead of re-fetching. The seed's bare requests.Session is
    deliberately NOT registered as the memo scraper — it was configured with
    args=None and would drift from the args-configured scrapers the other
    phases build.

    Returns None if `query` isn't a URL — the caller proceeds with normal
    title-based search using `query` as-is. Raises SystemExit if the URL is a
    seedable host that can't be resolved (bad slug, network error), OR is a URL
    for an unsupported host — the user passed a URL expecting a seed, so fail
    loudly rather than silently treat the raw URL as a search string.
    """
    if not query:
        return None
    q = query.strip()
    if not q.lower().startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(q)
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    site = next(
        (
            name
            for suffix, name in _URL_SEED_HOSTS.items()
            if host == suffix or host.endswith("." + suffix)
        ),
        None,
    )
    if site is None:
        supported = ", ".join(sorted(_URL_SEED_HOSTS))
        sys.exit(
            f"--search received a URL but URL-mode is only supported for: "
            f"{supported} (those sites' keyword search is unreliable/dead). "
            f"Got host: {host!r}. For other sites, search by title."
        )
    # Resolve the URL via the handler's fetch_comic_context. Both supported
    # handlers extract the id from the URL themselves (mangafire: new/old shape;
    # comix: slug hid) and populate the comic dict — mangafire via a plain JSON
    # GET, comix via the SSR #initial-data blob. No browser in either path.
    handler = get_handler_by_name(site)
    if handler is None:
        sys.exit(f"{site} handler unavailable")
    session = requests.Session()
    try:
        handler.configure_session(session, None)
    except Exception:
        pass

    def _mk(url, scraper):
        return scraper.get(url, timeout=30)

    try:
        ctx = handler.fetch_comic_context(q, session, _mk)
    except Exception as exc:
        sys.exit(f"Failed to resolve {site} URL: {exc}")
    comic = ctx.comic or {}
    title = ctx.title or comic.get("title")
    if not title or title == "Unknown Title":
        sys.exit(f"{site} URL didn't yield a title: {q}")

    # Chapter count is per-site. MangaFire's get_chapters is a cheap REST call,
    # so verify the actual fetchable count (feeds the orchestrator's DMCA/count
    # heuristics). comix's get_chapters is a slow browser DOM scrape — DON'T run
    # it just to seed a search; use the #initial-data `latestChapter` hint
    # instead (a metadata claim, not verified → chapter_count_hint only,
    # actual=None), so resolving a comix URL stays a single cheap HTTP GET.
    count_hint: Optional[int] = None
    actual: Optional[int] = None
    seed_chapters: Optional[list] = None
    if site == "mangafire":
        try:
            chaps = handler.get_chapters(ctx, session, "en", _mk)
            count_hint = actual = len(chaps) if chaps else None
            seed_chapters = chaps if chaps else None
        except Exception:
            count_hint = actual = None
    else:
        for _key in ("latestChapter", "finalChapter"):
            _val = comic.get(_key)
            if _val in (None, "", 0, "0"):
                continue
            try:
                count_hint = int(float(_val))
                break
            except (TypeError, ValueError):
                continue

    # Pre-populate the per-run fetch memo with data this resolution already
    # paid for. Keyed on the SAME url the SearchHit below carries
    # (comic.get("url") or q) so probe/winner lookups — which key on
    # src.url == hit.url — hit it. Best-effort: memo failures must never
    # break seed resolution.
    if fetch_memo is not None:
        seed_url = comic.get("url") or q
        try:
            fetch_memo.put_context(site, seed_url, ctx)
            if seed_chapters:
                fetch_memo.put_chapters(site, seed_url, "en", seed_chapters)
        except Exception:
            pass

    # comix omits is_official (defer to the handler, matching its own search()
    # hits); MangaFire is a non-publisher aggregator → False.
    is_official = False if site == "mangafire" else None
    return SearchHit(
        site=site,
        title=title,
        url=comic.get("url") or q,
        cover=comic.get("cover"),
        alt_titles=comic.get("alt_names") or [],
        year=comic.get("year"),
        language=None,
        chapter_count_hint=count_hint,
        actual_chapter_count=actual,
        dmca_likely=False,
        # 1.0 because the title came directly from the URL's series page —
        # the user's intent is unambiguous, so seed it as a perfect match.
        raw_score=1.0,
        is_official=is_official,
    )


def _primary_alt_names(context: Any) -> List[str]:
    """The handler-scraped alt_names of a download's primary series, as strings.

    Handlers store them on ``context.comic["alt_names"]`` (grep
    `comic["alt_names"]` under sites/) — usually a list, occasionally a single
    string. Anything else, or a context with no comic dict, gives [].
    """
    comic = getattr(context, "comic", None)
    if not isinstance(comic, dict):
        return []
    raw = comic.get("alt_names")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(a) for a in raw if isinstance(a, str) and a.strip()]


def _search_make_request_factory(timeout: float, attempts: int = 2):
    """Build a make_request specialized for search: fast-fail, light retries.

    The default aio-dl.py make_request retries up to 6× with exponential
    backoff capped at 45s per attempt — appropriate for chapter image
    downloads but a deadlock for cross-site search where one slow site
    can block the orchestrator for minutes. Search probes need <30s
    end-to-end across all sites; we do at most `attempts` quick tries per
    URL with a small fixed pause between, then propagate the exception so
    the orchestrator's probe-failure cache records the host.

    5xx responses are converted to exceptions here so the probe-failure
    cache catches dead sites returning CF 522 / 503. Without this, a
    handler would just see a response, parse zero results from the error
    page, and return [] — looking identical to "no match for query" and
    leaving the cache empty. 4xx-as-dead-host is per-handler (e.g.,
    mangafire's 403 JSON marker) since some 4xx codes are legit per-query.
    """
    pause_s = 0.5

    def _mr(url, scraper):
        last_exc: Optional[Exception] = None
        for i in range(attempts):
            try:
                r = scraper.get(url, timeout=timeout)
                if 500 <= r.status_code < 600:
                    raise requests.exceptions.HTTPError(
                        f"search probe got server error {r.status_code}",
                        response=r,
                    )
                return r
            except Exception as e:
                last_exc = e
                if i < attempts - 1:
                    time.sleep(pause_s)
                    continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"search request failed without exception: {url}")

    return _mr


def _build_scraper(args: Any) -> Any:
    """The same session aio-dl.py main() builds (sites/http_session.py), plus
    the --cookies jar."""
    scraper = create_http_session()
    cookies = getattr(args, "cookies", "") or ""
    if cookies:
        try:
            scraper.cookies.update(
                dict(kv.split("=", 1) for kv in cookies.split(";") if "=" in kv)
            )
        except Exception:
            pass
    return scraper


def _scraper_factory_for(args: Any) -> Callable:
    """Return a callable: handler -> scraper. Each handler gets a fresh
    scraper so configure_session(headers) calls don't pollute each other."""
    def factory(handler) -> Any:
        s = _build_scraper(args)
        try:
            handler.configure_session(s, args)
        except Exception:
            # configure_session is best-effort; if it fails, the bare scraper
            # still works for most sites.
            pass
        return s

    return factory


def _fetch_chapters_for_winner(
    candidate,
    args,
    make_request,
    *,
    parallelism: Optional[int] = None,
    on_status=None,
    fetch_memo=None,
):
    """Fetch chapter lists for each source of the winner SeriesCandidate in parallel.

    Returns a list of dicts in the same order as candidate.sources, each:
      {site, chapters, scraper, handler, context, url}
    Sources that fail to return chapters get an empty chapters list (not None)
    so the alignment step still gets a placeholder; their scraper/handler/context
    can still be populated for retry purposes.

    The richer return shape (vs the original tuple) is needed for Phase 4b:
    aio-dl.py main() needs the live scraper/context per source to populate the
    per-chapter fallback dict consumed by _process_chapter_strict.

    2026-07-12 perf changes:
      - ``parallelism=None`` (the new default) resolves to min(n_sources, 8)
        — was a hardcoded 4. Each source is ONE distinct host doing 1-2
        requests, so the wider burst is politeness-safe; at 4, ten alt
        sources took 3 sequential waves.
      - The list fetches use an internal bounded-retry shim (2 attempts ×
        30s, same _search_make_request_factory the search probes use)
        instead of the caller-supplied make_request — aio-dl.py's default
        retries 6× with 45s-capped backoff, so ONE dead alt could stall
        this phase for minutes. The passed ``make_request`` stays in the
        signature: public callers (aio-dl.py via find_alternatives /
        build_alternatives_*) still pass it, and the per-chapter DOWNLOAD
        path keeps using aio-dl's own make_request — only the list fetch
        here is bounded. Accepted trade-off: a transient-5xx alt that 6
        retries would have rescued contributes no fallback for this run.
      - ``fetch_memo`` (sites/fetch_memo.py): context + chapters + scraper
        come from the per-run memo when provided — for sources the probe
        phase already touched this is zero-network (the 2nd-3rd fetch of
        identical data before this change).
    """
    if on_status:
        on_status(f"[*] Fetching chapter lists from {len(candidate.sources)} sources...")
    _fetch_started = time.monotonic()

    language = (
        getattr(args, "language", None)
        or getattr(args, "search_language", None)
        or "en"
    )

    # Bounded-retry shim for the list fetches (see docstring). make_request
    # (the 6-retry one) is intentionally NOT used below.
    winner_mr = _search_make_request_factory(timeout=30.0, attempts=2)

    def _fetch_one(source) -> dict:
        handler = get_handler_by_name(source.site)
        if handler is None:
            return {
                "site": source.site, "url": source.url,
                "chapters": [], "scraper": None, "handler": None, "context": None,
            }

        def _build_configured() -> Any:
            s = _build_scraper(args)
            try:
                handler.configure_session(s, args)
            except Exception:
                pass
            return s

        if fetch_memo is not None:
            # Reuse the probe phase's scraper for this (site, url) — CF
            # clearance and cookies included; fresh build only on miss.
            scraper = fetch_memo.get_scraper(
                source.site, source.url, _build_configured,
            )
        else:
            scraper = _build_configured()
        try:
            if fetch_memo is not None:
                ctx = fetch_memo.get_context(handler, source.url, scraper, winner_mr)
            else:
                ctx = handler.fetch_comic_context(source.url, scraper, winner_mr)
        except Exception as exc:
            if on_status:
                on_status(f"  {source.site}: fetch_comic_context failed ({type(exc).__name__})")
            return {
                "site": source.site, "url": source.url,
                "chapters": [], "scraper": scraper, "handler": handler, "context": None,
            }
        try:
            if fetch_memo is not None:
                chapters = fetch_memo.get_chapters(
                    handler, source.url, language, scraper, winner_mr,
                )
            else:
                chapters = handler.get_chapters(ctx, scraper, language, winner_mr)
        except Exception as exc:
            if on_status:
                on_status(f"  {source.site}: get_chapters failed ({type(exc).__name__})")
            return {
                "site": source.site, "url": source.url,
                "chapters": [], "scraper": scraper, "handler": handler, "context": ctx,
            }
        if not isinstance(chapters, list):
            chapters = []
        # An ALT source must never OFFER a chapter it can't serve. tapas emits
        # premium/wait-to-unlock episodes and MangaDex emits publisher link-outs
        # as placeholders — meaningful only on the PRIMARY (where --multi-source
        # fills them from peers). Acting as an alternative, such a site can't
        # deliver those pages, so drop them; otherwise the aligner would
        # advertise a placeholder as a "fallback" that always fails when tried
        # (or, for a MangaDex `pages: 1` link-out, "rescue" a chapter with the
        # EXTERNAL CHAPTER card). An empty list after this means the site has
        # nothing readable — the primary switch reads that via readable_sites.
        # Cross-file: sites/base.py is_placeholder_chapter.
        chapters = readable_chapters(chapters)
        return {
            "site": source.site, "url": source.url,
            "chapters": chapters, "scraper": scraper, "handler": handler, "context": ctx,
        }

    # Per-source durations so the summary line can NAME what dominated the
    # phase (e.g. a comix browser chapter scrape that had no memo entry).
    fetch_times: list = []

    def _timed_fetch(source) -> dict:
        t0 = time.monotonic()
        try:
            return _fetch_one(source)
        finally:
            fetch_times.append((source.site, time.monotonic() - t0))

    if parallelism is None:
        parallelism = min(len(candidate.sources), 8)
    workers = max(1, min(parallelism, len(candidate.sources)))
    results: list = [None] * len(candidate.sources)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ms-chap") as pool:
        futures = {pool.submit(_timed_fetch, src): idx for idx, src in enumerate(candidate.sources)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result(timeout=60.0)
            except Exception:
                src = candidate.sources[idx]
                results[idx] = {
                    "site": src.site, "url": src.url, "chapters": [],
                    "scraper": None, "handler": None, "context": None,
                }
    if on_status:
        ok = sum(1 for r in results if r and r.get("chapters"))
        slowest = sorted(fetch_times, key=lambda kv: -kv[1])[:3]
        slow_str = (
            " (slowest: " + " · ".join(f"{n} {t:.1f}s" for n, t in slowest) + ")"
            if slowest else ""
        )
        on_status(
            f"[*] Chapter lists fetched: {ok}/{len(candidate.sources)} "
            f"sources in {time.monotonic() - _fetch_started:.1f}s{slow_str}"
        )
    return [r for r in results if r is not None]


def _readable_sites(source_records: list) -> list:
    """Sites from _fetch_chapters_for_winner records that can serve as a
    download primary: a context and a non-empty list (placeholders are
    already stripped there, so non-empty means readable chapters exist)."""
    return [
        r["site"] for r in source_records
        if r.get("chapters") and r.get("context") is not None and r.get("handler") is not None
    ]


def _catalog_from_records(source_records: list, primary_site: str) -> list:
    """The full readable chapter list of every usable alt site, for the
    multi-source rescue's alt catalog (aio-dl.py grep `_ms_catalog`;
    sites/rescue_pairing.py builds the ordered lists). The per-number
    alternatives dict keeps only the entries the aligner could pair by label,
    which is exactly what sites that number differently break; content-proven
    rescue needs every entry. Same records the dict is built from, so the live
    handler/scraper/context refs are shared, not copied. The primary's own site
    is left out, as in the dict."""
    return [
        {"site": r["site"], "url": r.get("url"), "chapters": list(r["chapters"]),
         "handler": r["handler"], "scraper": r.get("scraper"), "context": r["context"]}
        for r in source_records
        if r.get("site") != primary_site and r.get("chapters")
        and r.get("context") is not None and r.get("handler") is not None
        and r.get("scraper") is not None
    ]


def build_alternatives_from_records(
    source_records: list, alt_sources: list, primary_handler, primary_chapters, args,
) -> dict:
    """The discovery result every multi-source entry point returns, built from
    alt chapter lists already in hand: the per-number alternatives dict
    (aligned with the primary's REAL list as anchor), the consensus, the ranked
    sources for the resume cache and the primary switch, and the full catalog.

    `source_records` are _fetch_chapters_for_winner records; any for the
    primary's own site are left out. `alt_sources` are the ranked sources the
    records were fetched from (objects with .site / .url / .title / .cover),
    echoed as `resolved_sources`. Callers: find_alternatives_for_direct_url,
    build_alternatives_from_payload, alternatives_from_search_state.
    """
    primary_site = primary_handler.name
    records = [r for r in source_records if r.get("site") != primary_site]
    sources_with_chapters: list = [(primary_site, primary_chapters or [])]
    for rec in records:
        sources_with_chapters.append((rec["site"], rec["chapters"]))
    alignment = align_chapter_lists(
        sources_with_chapters,
        collapse_splits=bool(getattr(args, "collapse_splits", False)),
    )

    records_by_site = {rec["site"]: rec for rec in records}
    alternatives_by_chap_num: dict = {}
    for entry in alignment.chapter_map:
        alts = []
        for site, ch_dict in entry.sources:
            if site == primary_site:
                continue
            rec = records_by_site.get(site)
            if not rec or not rec.get("context") or not rec.get("handler"):
                continue
            alts.append({
                "site": site,
                "url": rec["url"],
                "chapter": ch_dict,
                "scraper": rec["scraper"],
                "handler": rec["handler"],
                "context": rec["context"],
            })
        if alts:
            alternatives_by_chap_num[entry.chapter_num] = alts

    return {
        "alternatives_by_chap_num": alternatives_by_chap_num,
        # Consumed by aio-dl.py at the group_chapters_for_download call site
        # (refined collapse-splits rules). None = no peer overlap.
        "consensus_set": (
            alignment.consensus_set if alignment.consensus_set else None
        ),
        "consensus_max": alignment.consensus_max,
        # (site, url, title, cover) of the ranked alternatives. aio-dl.py
        # persists these into run_params.json's `multi_source_cache` so a
        # resume rebuilds the alternatives via build_alternatives_from_payload
        # without a cross-site search (grep _build_multi_source_cache_payload).
        "resolved_sources": [
            {"site": s.site, "url": s.url,
             "title": getattr(s, "title", "") or "",
             "cover": getattr(s, "cover", None)}
            for s in alt_sources if s.site != primary_site
        ],
        # Sites whose fetched list had readable chapters, in fetch order. The
        # primary switch in aio-dl.py reads it (grep _pick_primary_switch).
        "readable_sites": _readable_sites(records),
        # Every alt site's full list, for the content-proven rescue.
        "catalog": _catalog_from_records(records, primary_site),
    }


def alternatives_from_search_state(state: dict, primary_handler, primary_chapters, args) -> dict:
    """The discovery result for a --search --multi-source --auto-pick run, from
    the chapter lists the search already fetched (take_latest_multi_source_state),
    so aio-dl.py gets the same alternatives, catalog and resume cache as a
    direct-URL run without a second search or a second round of list fetches.
    The primary is the handler main() actually resolved, not the search's
    alignment anchor; its site is dropped from the alternatives."""
    return build_alternatives_from_records(
        state.get("source_records") or [], state.get("sources") or [],
        primary_handler, primary_chapters, args,
    )


def run_search_mode(
    args: Any,
    make_request: Callable,
    record_rate_limit: Optional[Callable[[str, float], None]] = None,
) -> Optional[str]:
    """Entry point called by aio-dl.py when args.search is set.

    Args:
      args: argparse Namespace (must include args.search; optional args.auto_pick,
            args.search_language, args.search_parallelism, etc.)
      make_request: aio-dl.py's make_request — kept in the signature for
            symmetry with the rest of the codebase, but NOT used directly
            for the search probes themselves. The aio-dl.py make_request
            retries 6× with exp-backoff capped at 45s — fine for chapter
            downloads, fatal for parallel search where one slow site
            could lock the orchestrator for minutes. We build a fast-fail
            request shim here instead. The probe-failure cache still hooks
            into _record_rate_limit (passed as record_rate_limit) so a
            host that times out twice is suppressed for 1h via the same
            cooldown machinery used by the download path.
      record_rate_limit: aio-dl.py's _record_rate_limit (host, delay) — used
            by the probe-failure cache to suppress dead hosts via existing
            rate-limit machinery.

    Returns:
      None if --auto-pick was not set (we printed JSON and the caller should
        exit cleanly).
      str URL of the winning candidate's top source if --auto-pick was set
        (caller should set args.comic_url and fall through to normal flow).

    Raises SystemExit on user-facing errors (no candidates with --auto-pick,
    empty query, etc.) so the message reaches stderr cleanly.
    """
    query = (getattr(args, "search", "") or "").strip()
    if not query:
        sys.exit("--search requires a non-empty title.")

    # Per-run fetch memo (sites/fetch_memo.py, 2026-07-12): shared by the
    # matched-page probe phase and the winner chapter fetch so each
    # (site, url)'s context + chapter list is fetched from the network at
    # most once per run. In-process only — dies with this invocation.
    fetch_memo = FetchMemo()

    # URL-mode: if the user supplied a series URL instead of title text (a
    # MangaFire or comix /title/ URL — see _URL_SEED_HOSTS), resolve it into a
    # seed hit. The orchestrator then runs the normal cross-site search using
    # the resolved title, with that site's data already guaranteed to be present.
    # The seed resolution pre-populates the memo (context + mangafire chapter
    # list) so the winner fetch gets the seed source for free.
    seed_hits: list = []
    seed = _try_extract_seed_hit(query, fetch_memo=fetch_memo)
    if seed is not None:
        seed_hits.append(seed)
        # Use the scraped title as the effective query for other sites — the
        # original URL is meaningless to the other handlers.
        query = seed.title

    language = getattr(args, "search_language", None) or getattr(args, "language", "en") or "en"
    parallelism = max(1, int(getattr(args, "search_parallelism", 6) or 6))
    timeout = float(getattr(args, "search_timeout", DEFAULT_PER_SITE_TIMEOUT_S) or DEFAULT_PER_SITE_TIMEOUT_S)
    min_match = float(getattr(args, "search_min_match", DEFAULT_MIN_MATCH) or DEFAULT_MIN_MATCH)
    auto_pick = bool(getattr(args, "auto_pick", False))
    json_output = bool(getattr(args, "search_json", False)) or not auto_pick

    cache = ProbeFailureCache(record_cooldown=record_rate_limit)
    img_cache = ImageQualityCache()
    factory = _scraper_factory_for(args)
    search_mr = _search_make_request_factory(timeout=timeout, attempts=2)

    # Status output always goes to stderr — JSON consumers reading stdout
    # are unaffected, but a user who pipes the JSON still sees progress in
    # their terminal.
    def _status(msg: str) -> None:
        print(msg, file=sys.stderr)

    # Search-health sink (2026-07-13): search_all fills this with per-site
    # slow/down verdicts + phase timings; surfaced in the JSON payload below so
    # the UI can recommend disabling chronically slow/down sites.
    diag: dict = {}
    candidates = search_all(
        query,
        factory,
        search_mr,
        language=language,
        parallelism=parallelism,
        per_site_timeout_s=timeout,
        min_match=min_match,
        probe_failure_cache=cache,
        img_quality_cache=img_cache,
        seed_hits=seed_hits or None,
        # Search mode probes every eligible source (including the seed
        # source, when --search was given a URL) so the JSON output carries
        # comparable ratings. The SKIP_QUALITY_PROBE class-attr opt-out is
        # honored in search_orchestrator._probe_eligible (a generic hook - no
        # handler sets it today) - no plumbing here.
        on_status=_status,
        seeded_only=bool(getattr(args, "seeded_only", False)),
        # Probe scope (2026-07-12, user decision): --auto-pick only ever
        # reads candidates[0] → probing other candidates' sources is pure
        # waste (1). UI searches (JSON output) probe the top TWO candidates
        # ("titles can be slightly different" — keep the runner-up
        # comparable); candidates #3+ fall back to seed and carry
        # quality_basis="seed" so SearchSourceCard flags them. Candidate
        # ORDER is unaffected (pure title-match).
        probe_candidate_limit=(1 if auto_pick else 2),
        fetch_memo=fetch_memo,
        # Drop the user's disabled sites from the fan-out + probe entirely.
        exclude_sites=parse_disable_sites(args),
        diagnostics=diag,
    )

    multi_source = bool(getattr(args, "multi_source", False))

    # Multi-source: fetch chapter lists for the winner's sources and align
    # them, for the JSON chapter map and (--auto-pick) the primary choice; the
    # fetched lists are handed to aio-dl.py for the rescue (search_ms_state).
    winner_chapter_map = None
    search_ms_state = None  # handed to aio-dl.py main() via take_latest_multi_source_state
    if multi_source and candidates:
        winner = candidates[0]
        # Apply quality threshold to the WHOLE candidate's sources before
        # fetching chapter lists, so we don't waste time pre-fetching from
        # low-quality / foreign-language sites that would never be picked
        # as alternatives anyway.
        quality_min = float(getattr(args, "multi_source_quality_min", 0.65) or 0.65)
        # Keep top-ranked source even if it's below threshold (it's the
        # primary the user picks via --auto-pick); just filter the rest.
        eligible_alts = _filter_and_rank_alt_sources(
            winner.sources[1:],  # skip top
            primary_host="",  # no primary-host yet; --auto-pick uses winner.sources[0]
            quality_min=quality_min,
            primary_edition=getattr(winner.sources[0], "edition", None),
            rank_pool=winner.sources,
        )
        # The winner candidate is reduced to: top source + eligible alts.
        from sites.search_orchestrator import SeriesCandidate
        winner = SeriesCandidate(
            canonical_title=winner.canonical_title,
            canonical_year=winner.canonical_year,
            sources=[winner.sources[0]] + eligible_alts,
        )
        if winner.sources:
            source_records = _fetch_chapters_for_winner(
                winner, args, make_request, on_status=_status,
                fetch_memo=fetch_memo,
            )
            _status(fetch_memo.stats_line())
            # Alignment input is the older tuple shape; project the richer
            # records down for align_chapter_lists.
            # collapse_splits flows from the --collapse-splits CLI flag
            # (default False as of 2026-05-27 opt-in flip). Affects per-
            # source `effective_chapters` AND, when consensus is present,
            # the actual download list. UI reads `collapse_splits_applied`
            # to label which interpretation the displayed counts reflect.
            collapse_splits = bool(getattr(args, "collapse_splits", False))
            alignment = align_chapter_lists(
                [(rec["site"], rec["chapters"]) for rec in source_records],
                collapse_splits=collapse_splits,
            )
            # Aggregate "effective" count across the whole aligned chapter_map.
            # When collapse is applied, this is the headline "Y main chapters
            # aligned" the UI surfaces alongside the raw entry count. We need
            # to count each chapter_map entry's chapter_num — the union of
            # everything any source contributed (anchor + orphans).
            # consensus_set passed through so the aggregate matches per-source
            # `effective_chapters` (post-refinement), e.g. for Shangri-La
            # Frontier the aggregate drops from 290 to 283 = 305 - 22
            # source-only .1 fragments. Without the consensus arg here, the
            # aggregate would still report 290 while per-source mangafire
            # reports 283 — inconsistent. (2026-05-27 cross-source duplicate
            # detection; see ~/.claude/plans/ultrathink-mangafire-and-some-flickering-sparkle.md)
            all_aligned_nums = [
                e.chapter_num for e in alignment.chapter_map
            ]
            _, effective_aligned = _classify_main_chapters(
                all_aligned_nums,
                collapse_splits=collapse_splits,
                consensus_set=alignment.consensus_set,
            )
            winner_chapter_map = {
                "canonical_title": winner.canonical_title,
                "merge_diagnostics": alignment.merge_diagnostics,
                "collapse_splits_applied": alignment.collapse_splits_applied,
                "effective_chapters_aligned": effective_aligned,
                # consensus_set serialized as a sorted list — sets aren't
                # JSON-serializable. Consumers (downstream aio-dl.py via
                # --multi-source-prefetched, and the UI if it ever needs to
                # render which chapters are peer-confirmed) rebuild the set
                # via `set(consensus_set)`.
                "consensus_set": sorted(alignment.consensus_set),
                "consensus_max": alignment.consensus_max,
                "chapters": [
                    {
                        "chapter_num": entry.chapter_num,
                        "chapter_label": entry.chapter_label,
                        # Sources are already ranked official-first (chapter_merger
                        # sorts by is_official within each entry). The 4-tuple
                        # surfaces translation provenance to the JSON output so
                        # UI / downstream consumers can render an "official" badge
                        # or rank-by-publisher. Phase 4c (2026-05-07).
                        "sources": [
                            {
                                "site": site,
                                "is_official": ch.get("is_official") is True,
                                "publisher": ch.get("publisher"),
                                "group_name": ch.get("group_name"),
                            }
                            for site, ch in entry.sources
                        ],
                    }
                    for entry in alignment.chapter_map
                ],
                "total_chapters_aligned": len(alignment.chapter_map),
            }
            # The alignment anchor (most chapters) becomes the download
            # primary below. The fetched records ride to aio-dl.py as they are
            # (live scraper/handler/context refs): main() builds the
            # alternatives, catalog and resume cache from them once it has
            # resolved the primary and fetched ITS list (grep
            # alternatives_from_search_state), the same as a direct-URL run.
            # Alternatives keyed here would be keyed on the anchor's list as
            # the search saw it, not on the list the download walks.
            search_ms_state = {
                "anchor_site": next(
                    (s for s, d in alignment.merge_diagnostics.items()
                     if d.get("role") == "anchor"),
                    None,
                ),
                "source_records": source_records,
                # Ranked as the search ranked them (top source first); the
                # resume cache and the primary switch keep this order.
                "sources": list(winner.sources),
            }

    if json_output and not auto_pick:
        payload = {
            "query": query,
            "language": language,
            "candidates": [c.to_json() for c in candidates],
        }
        # Per-site slow/down verdicts (2026-07-13) — absent when every site was
        # healthy, so the UI shows no callout. eligible_count lets the UI's
        # "Disable & re-search" guard avoid emptying the roster.
        if diag.get("site_health"):
            payload["site_health"] = diag["site_health"]
        if diag.get("eligible_count") is not None:
            payload["eligible_count"] = diag["eligible_count"]
        # tested_sites = the sites actually searched this run. The UI decays a
        # tracked site's strike only when it was tested-and-healthy (in here,
        # absent from site_health); an untested site is left alone. Emitted
        # whenever the search fanned out at all (search_orchestrator.py).
        if diag.get("tested_sites"):
            payload["tested_sites"] = diag["tested_sites"]
        if winner_chapter_map is not None:
            payload["winner_chapter_map"] = winner_chapter_map
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return None

    # --auto-pick path
    if not candidates:
        sys.exit(f"No candidates found for '{query}'.")
    winner = candidates[0]
    if not winner.sources:
        sys.exit(f"Top candidate '{winner.canonical_title}' has no sources.")
    top = winner.sources[0]

    # Multi-source informational output for --auto-pick path.
    if winner_chapter_map is not None:
        diags = winner_chapter_map.get("merge_diagnostics", {})
        total = winner_chapter_map.get("total_chapters_aligned", 0)
        print(
            f"[*] Multi-source alignment for '{winner.canonical_title}': "
            f"{total} chapters across {len(diags)} sources",
            file=sys.stderr,
        )
        # Show effective count (split-collapsed) alongside raw entries when
        # they diverge — flags inflated catalogs at a glance. Same data the
        # UI surfaces in SearchChapterMap.
        for site, info in diags.items():
            n = info.get("total_chapters", 0)
            eff = info.get("effective_chapters", n)
            comp = info.get("compatibility", 0)
            note = info.get("skipped_reason", "")
            count_str = (
                f"chapters={n:>4}"
                if eff == n
                else f"chapters={eff:>4} (raw {n})"
            )
            print(
                f"    {site:18s} {count_str} compat={comp:.0%} "
                + (f"({note})" if note else ""),
                file=sys.stderr,
            )

    # In multi-source mode, the alignment ANCHOR (source with most chapters)
    # is the better primary download source than winner.sources[0] (which
    # ranks by title-match+seed and may be DMCA-affected). Switch to the
    # anchor's URL so primary downloads have full coverage; alternatives
    # remain available for per-chapter fallback.
    primary_url = top.url
    primary_site = top.site
    if search_ms_state is not None:
        anchor_site = search_ms_state.get("anchor_site")
        if anchor_site:
            anchor_record = next(
                (r for r in search_ms_state["source_records"]
                 if r["site"] == anchor_site),
                None,
            )
            if anchor_record and anchor_record.get("url"):
                primary_url = anchor_record["url"]
                primary_site = anchor_site
            if primary_site != top.site:
                print(
                    f"[*] Multi-source: switching primary from '{top.site}' to "
                    f"'{primary_site}' (anchor with most chapters)",
                    file=sys.stderr,
                )

    print(
        f"[*] --auto-pick selected '{winner.canonical_title}' "
        f"from {primary_site} (match={top.title_match:.2f}, seed={top.seed_quality:.2f})",
        file=sys.stderr,
    )

    # Stash the fetched records on a module attribute so aio-dl.py main() can
    # pick them up after run_search_mode returns: live scrapers/handlers/
    # contexts can't ride the str return value.
    global _LATEST_MULTI_SOURCE_STATE
    _LATEST_MULTI_SOURCE_STATE = search_ms_state
    return primary_url


# Side-channel from run_search_mode to aio-dl.py main(): {"anchor_site",
# "source_records" (live refs), "sources" (ranked)}. Set on the --auto-pick
# path with --multi-source; main() takes it right after the call and turns it
# into alternatives once the primary is resolved (alternatives_from_search_state,
# aio-dl.py grep _ms_search_state). None when multi-source is off.
_LATEST_MULTI_SOURCE_STATE = None


def take_latest_multi_source_state():
    """One-shot getter: returns the multi-source state from the most recent
    run_search_mode call and clears it."""
    global _LATEST_MULTI_SOURCE_STATE
    val = _LATEST_MULTI_SOURCE_STATE
    _LATEST_MULTI_SOURCE_STATE = None
    return val


# Bases whose source served pages to the matched-page probe (sites/
# match_session.py). User decision 2026-10-18 ("all measured"): every such
# source is a fallback whatever its rating, ordered by quality; the seed floor
# only gates sources the probe never measured.
_SERVED_BASES = frozenset({"matched", "partial", "solo", "unmatched"})


def _edition_of(s) -> str:
    """'alternate' for a fan-colored / different edition, else 'original'
    (undecided counts as original). Mirror: editionOf in
    UI-source/src/lib/prefetchedAlts.js."""
    return "alternate" if getattr(s, "edition", None) == "alternate" else "original"


def _filter_and_rank_alt_sources(
    sources, *, primary_host: str = "", quality_min: float = 0.65,
    primary_edition: Optional[str] = None, rank_pool=None,
) -> list:
    """Filter a winner candidate's sources down to the fallback list, in the
    search's own source order (search_orchestrator.source_rank_key). That
    order is chapter_merger's alignment input order, so it also breaks a
    chapter-coverage tie for the anchor in favour of the better copy (plan
    decision E) and orders the primary switch's targets.

    Kept:
      - every source that served pages to the matched-page probe (basis in
        _SERVED_BASES), whatever its rating (user decision "all measured");
      - an unmeasured source (pending / seed) when seed_quality >= quality_min:
        unknown-quality extras default to 0.50 and are mostly foreign-language
        (raise or lower with --multi-source-quality-min).
    Dropped: the primary's host, SKIP_MULTI_SOURCE handlers, `unreachable`
    sources (every page fetch failed), and sources of another edition than
    the primary's (decision: a colored copy never fills a B&W download, nor the
    reverse; primary_edition None = original).

    rank_pool: the whole candidate's sources, when `sources` is a slice of it,
    so the title band is measured from the candidate's best title match.
    """
    want = "alternate" if primary_edition == "alternate" else "original"
    eligible = []
    for s in sources:
        if primary_host and urlparse(s.url).netloc.lower() == primary_host:
            continue
        # Generic opt-out for a handler whose chapter-list fetch is far more
        # expensive than its peers'; NO handler sets it today (comix used
        # to). Same check in build_alternatives_from_payload.
        handler = get_handler_by_name(s.site)
        if handler is not None and getattr(handler, "SKIP_MULTI_SOURCE", False):
            continue
        basis = getattr(s, "basis", None)
        if basis == "unreachable" or _edition_of(s) != want:
            continue
        if basis in _SERVED_BASES or s.seed_quality >= quality_min:
            eligible.append(s)
    best = best_title_of(list(rank_pool) if rank_pool is not None else list(sources))
    eligible.sort(key=lambda s: source_rank_key(s, best))
    return eligible


def _normalize_url_for_compare(u: str) -> str:
    """Normalize a URL to host+path-lowercase-no-trailing-slash for source
    matching. URLs from direct user input vs search-result feeds can differ
    in trailing slash, casing, or query params even when they refer to the
    same series — strip those for comparison."""
    if not u:
        return ""
    try:
        p = urlparse(u)
    except Exception:
        return u.lower().strip("/")
    return f"{p.netloc.lower()}{p.path.lower().rstrip('/')}"


def find_alternatives_for_direct_url(
    primary_url: str,
    primary_handler,
    primary_context,
    primary_chapters,
    args,
    make_request,
    record_rate_limit=None,
    on_status=None,
):
    """Build a `_multi_source_alternatives`-shaped dict for a direct-URL
    invocation (no --search). Same data shape as the search-driven path
    produces; aio-dl.py's _process_chapter_strict reads it identically.

    Flow:
      1. Determine the series title from the primary's already-fetched
         context (fetch_comic_context; for MangaFire this is now a plain
         JSON API call — no browser fallback needed).
      2. Run the search orchestrator using that title — finds candidates
         across all search-capable handlers.
      3. Take the top candidate's sources, drop any whose host matches
         primary's URL (avoid querying primary as its own alternative).
      4. Pre-fetch chapter lists for the alternatives in parallel.
      5. Run alignment with primary's chapters as anchor (since primary
         is what the user picked — its chapter set is canonical for the
         download).
      6. Return a dict with alternatives_by_chap_num AND the consensus_set
         from alignment so the download path can refine its collapse rules
         with cross-source duplicate detection (2026-05-27).

    Return shape:
      {
        "alternatives_by_chap_num": Dict[float, List[alt]],
        "consensus_set": Set[float] | None,    # None when single source / no overlap
        "consensus_max": float | None,
      }
    Returns {"alternatives_by_chap_num": {}, ...} when search yields nothing,
    when no alternatives have chapters, or on any failure. The user's direct-
    URL download still proceeds; multi-source just provides no fallback in
    that case.
    """
    _empty_result = {
        "alternatives_by_chap_num": {},
        "consensus_set": None,
        "consensus_max": None,
        "resolved_sources": [],
        "readable_sites": [],
    }
    # Step 1: title.
    title = (
        getattr(primary_context, "title", None)
        or (primary_context.comic.get("title") if getattr(primary_context, "comic", None) else None)
        or ""
    )
    title = (title or "").strip()
    # (MangaFire's fetch_comic_context now returns a real title from JSON — the
    # old Playwright "Unknown Title" CF-block fallback was removed with the 2026
    # REST-API rewrite.)
    if not title:
        if on_status:
            on_status("[!] Multi-source: couldn't determine title from URL; skipping alternatives discovery")
        return _empty_result

    if on_status:
        on_status(f"[*] Multi-source: searching for alternatives to '{title}'...")

    # Step 2: search.
    timeout = float(getattr(args, "search_timeout", DEFAULT_PER_SITE_TIMEOUT_S) or DEFAULT_PER_SITE_TIMEOUT_S)
    parallelism = max(1, int(getattr(args, "search_parallelism", 6) or 6))
    min_match = float(getattr(args, "search_min_match", DEFAULT_MIN_MATCH) or DEFAULT_MIN_MATCH)
    language = (
        getattr(args, "search_language", None)
        or getattr(args, "language", "en")
        or "en"
    )

    cache = ProbeFailureCache(record_cooldown=record_rate_limit)
    img_cache = ImageQualityCache()
    factory = _scraper_factory_for(args)
    search_mr = _search_make_request_factory(timeout=timeout, attempts=2)
    # Per-run fetch memo (sites/fetch_memo.py): probe → winner-fetch reuse,
    # same as run_search_mode. Scoped to this discovery call.
    fetch_memo = FetchMemo()

    candidates = search_all(
        title,
        factory,
        search_mr,
        language=language,
        parallelism=parallelism,
        per_site_timeout_s=timeout,
        min_match=min_match,
        probe_failure_cache=cache,
        img_quality_cache=img_cache,
        # Direct-URL --multi-source: the primary is committed (we download
        # from primary_url regardless) but still probed, not skipped: its
        # pages are what the alternatives are compared with, and its edition
        # decides which alternatives qualify (_filter_and_rank_alt_sources).
        on_status=on_status,
        seeded_only=bool(getattr(args, "seeded_only", False)),
        # Only candidates[0] is consumed below — probing the sub-series
        # candidates' sources would be pure waste on the download-start
        # path (2026-07-12 perf change; ranking unaffected — candidate
        # order is pure title-match).
        probe_candidate_limit=1,
        fetch_memo=fetch_memo,
        # Don't fan out to the user's disabled sites when discovering
        # alternatives; aio-dl.py also guard-filters the assembled dict to catch
        # prefetched/cached alts that predate the disable.
        exclude_sites=parse_disable_sites(args),
        # The primary page's own other names (the handler's alt_names, read off
        # the series page this download already fetched — never AniList
        # synonyms, which aren't written back into ctx.comic). Same role as a
        # URL seed's alt_titles in run_search_mode: sites that list the series
        # only under another name get searched AND scored by it. Without this,
        # a MangaFire "Monster Tale" download found no alternative on the
        # sites that list it as "Bakemonogatari". See search_all title_variants.
        title_variants=_primary_alt_names(primary_context),
    )
    if not candidates:
        if on_status:
            on_status("[!] Multi-source: no search candidates found; no alternatives available")
        return _empty_result

    # Step 3: pick the top candidate, drop sources on the same host as primary,
    # and filter out unknown-quality (default seed=0.50) sources that are
    # mostly foreign-language Madara extras. The user's --multi-source-quality-min
    # flag overrides the default 0.65 threshold.
    primary_host = ""
    try:
        primary_host = urlparse(primary_url).netloc.lower()
    except Exception:
        pass

    quality_min = float(getattr(args, "multi_source_quality_min", 0.65) or 0.65)
    winner = candidates[0]
    primary_entry = next(
        (s for s in winner.sources
         if primary_host and urlparse(s.url).netloc.lower() == primary_host),
        None,
    )
    alt_sources = _filter_and_rank_alt_sources(
        winner.sources,
        primary_host=primary_host,
        quality_min=quality_min,
        primary_edition=getattr(primary_entry, "edition", None),
    )
    if not alt_sources:
        if on_status:
            on_status(
                f"[!] Multi-source: no eligible alternatives for '{title}' "
                f"(quality_min={quality_min:.2f}; lower with "
                f"--multi-source-quality-min if you want broader fallback)"
            )
        return _empty_result

    # Step 4: pre-fetch chapter lists from alternatives.
    # Build a synthetic candidate exposing just the alt sources to reuse
    # _fetch_chapters_for_winner without modification.
    from sites.search_orchestrator import SeriesCandidate
    alt_candidate = SeriesCandidate(
        canonical_title=winner.canonical_title,
        canonical_year=winner.canonical_year,
        sources=alt_sources,
    )
    source_records = _fetch_chapters_for_winner(
        alt_candidate, args, make_request, on_status=on_status,
        fetch_memo=fetch_memo,
    )
    if on_status:
        on_status(fetch_memo.stats_line())

    # Steps 5-6: align with the primary as anchor (the user picked this URL, so
    # its chapter set is canonical for the download) and build the result.
    return build_alternatives_from_records(
        source_records, alt_sources, primary_handler, primary_chapters, args,
    )


def build_alternatives_from_prefetched(
    prefetched_path: str,
    primary_handler,
    primary_context,
    primary_chapters,
    args,
    make_request,
    on_status=None,
):
    """Skip-the-search version of find_alternatives_for_direct_url.

    Used when the UI already ran a multi-source search and just wants to
    download from one of the surfaced sources. The Search tab writes a JSON
    file with the candidate's primary + alternatives URLs; we read it,
    fetch chapter lists from the alternatives in parallel (the only
    network step we still need), align with the user's primary chapters,
    and return the same alternatives_by_chap_num dict shape that
    find_alternatives_for_direct_url produces. Caller (aio-dl.py main()
    around the multi_source block) consumes it identically.

    Why a side-channel JSON file instead of CLI args?
      - The alts list can have ~10 (site, url) pairs; CLI quoting on
        Windows makes that brittle, especially across spawn boundaries.
      - File is a small JSON in ~/.aio-dl/cache/, deleted by the
        downloader.js spawn-close handler. Same place ImageQualityCache
        and ProbeFailureCache snapshots live.

    Failure modes — all yield the empty-result dict so multi-source quietly
    degrades to single-source rather than blocking the download:
      - File missing / unreadable
      - Malformed JSON (no `alternatives` key, etc.)
      - Every alt's handler is unknown (renamed sites, etc.)
      - Every alt's chapter-list fetch throws

    Return shape — same as find_alternatives_for_direct_url (2026-05-27):
      {
        "alternatives_by_chap_num": Dict[float, List[alt]],
        "consensus_set": Set[float] | None,
        "consensus_max": float | None,
      }

    Cross-file:
      - Path passed via aio-dl.py's --multi-source-prefetched flag
        (argparse). aio-dl.py main() branches on it.
      - JSON is written by UI-source/electron/downloader.js before spawn
        when SearchSourceCard's handleDownload sees prefetchedAlts in args.
    """
    import json as _json
    import os as _os

    _empty_result = {
        "alternatives_by_chap_num": {},
        "consensus_set": None,
        "consensus_max": None,
        "resolved_sources": [],
        "readable_sites": [],
    }

    if not prefetched_path or not _os.path.exists(prefetched_path):
        if on_status:
            on_status(
                f"[!] Multi-source: prefetched alts file missing ({prefetched_path}); "
                f"skipping discovery"
            )
        return _empty_result

    try:
        with open(prefetched_path, "r", encoding="utf-8") as f:
            payload = _json.load(f)
    except (OSError, _json.JSONDecodeError) as exc:
        if on_status:
            on_status(
                f"[!] Multi-source: failed to read prefetched alts: "
                f"{type(exc).__name__}: {exc}"
            )
        return _empty_result

    return build_alternatives_from_payload(
        payload,
        primary_handler=primary_handler,
        primary_context=primary_context,
        primary_chapters=primary_chapters,
        args=args,
        make_request=make_request,
        on_status=on_status,
    )


def build_alternatives_from_payload(
    payload,
    primary_handler,
    primary_context,
    primary_chapters,
    args,
    make_request,
    on_status=None,
):
    """Shared core of the prefetched / resume-cache multi-source path.

    Takes an ALREADY-PARSED payload dict — the same shape the UI's
    --multi-source-prefetched file uses AND the shape aio-dl.py persists into
    run_params.json's `multi_source_cache` for resume:
        {"title": str, "year": int|None,
         "alternatives": [{"site", "url", "title"?, "cover"?}, ...]}
    Fetches each alternative's chapter list in parallel, aligns against the
    user's primary chapters, and returns the alternatives_by_chap_num dict
    shape find_alternatives_for_direct_url produces — WITHOUT the expensive
    cross-site title search. build_alternatives_from_prefetched reads a file
    then delegates here; aio-dl.py's resume path passes the persisted cache
    dict straight in (grep _read_multi_source_resume_cache in aio-dl.py).

    `resolved_sources` in the return echoes the (site, url, title, cover) of
    the alternatives that survived filtering, so the caller can re-persist a
    cleaned resume cache.
    """
    _empty_result = {
        "alternatives_by_chap_num": {},
        "consensus_set": None,
        "consensus_max": None,
        "resolved_sources": [],
        "readable_sites": [],
    }

    if not isinstance(payload, dict):
        if on_status:
            on_status(
                "[!] Multi-source: prefetched payload was not a JSON object; "
                "skipping discovery"
            )
        return _empty_result

    alternatives = payload.get("alternatives") or []
    if not isinstance(alternatives, list) or not alternatives:
        if on_status:
            on_status(
                f"[!] Multi-source: prefetched payload had no alternatives; "
                f"skipping discovery"
            )
        return _empty_result

    title = (payload.get("title") or "").strip() or "<prefetched>"
    if on_status:
        on_status(
            f"[*] Multi-source: using {len(alternatives)} prefetched alternative "
            f"source(s) for '{title}' (skipping cross-site search)"
        )

    # Build a synthetic SeriesCandidate from the JSON. We don't have full
    # SourceEntry data (no measured img_quality_score, no title_match score)
    # but _fetch_chapters_for_winner only reads .site and .url, so the
    # other fields can be defaulted. The caller isn't using the candidate
    # for ranking — just for the parallel chapter-list fetch.
    from sites.search_orchestrator import SeriesCandidate, SourceEntry

    # SKIP_MULTI_SOURCE filter: mirrors _filter_and_rank_alt_sources's check
    # (see ~line 761). Required at THIS entry point too because the prefetched
    # JSON path bypasses _filter_and_rank_alt_sources entirely — that filter
    # only runs on the direct-URL multi-source path
    # (find_alternatives_for_direct_url, above). The flag is a generic opt-out
    # for a handler whose chapter-list + per-chapter image fetch is far more
    # expensive than its peers; a flagged site would otherwise be queued for a
    # chapter-list fetch here and dragged into per-chapter fallback. NO handler
    # sets it today (comix used to, but it's now a full multi-source
    # participant — see sites/comix.py); kept so both entry points stay
    # consistent if a future handler needs it.
    # Cross-file: _filter_and_rank_alt_sources in this file does the same
    # check; both filters must agree for SKIP_MULTI_SOURCE to be honored
    # consistently across the direct-URL and prefetched-JSON paths.
    alt_sources = []
    skipped_skip_multi: list = []
    for alt in alternatives:
        site = (alt.get("site") or "").strip()
        url = (alt.get("url") or "").strip()
        if not site or not url:
            continue
        handler_for_alt = get_handler_by_name(site)
        if handler_for_alt is not None and getattr(
            handler_for_alt, "SKIP_MULTI_SOURCE", False,
        ):
            skipped_skip_multi.append(site)
            continue
        alt_sources.append(
            SourceEntry(
                site=site,
                url=url,
                title=alt.get("title") or "",
                cover=alt.get("cover"),
                # Defaults that make _fetch_chapters_for_winner work — none
                # of these are read during chapter-list fetching.
                title_match=1.0,
                seed_quality=0.0,
                composite_score=1.0,
            )
        )

    if skipped_skip_multi and on_status:
        on_status(
            f"[*] Multi-source: dropping {len(skipped_skip_multi)} prefetched "
            f"alternative(s) marked SKIP_MULTI_SOURCE: "
            f"{', '.join(sorted(set(skipped_skip_multi)))}"
        )

    if not alt_sources:
        if on_status:
            on_status(
                "[!] Multi-source: prefetched alternatives had no usable (site, url) "
                "entries; skipping discovery"
            )
        return _empty_result

    alt_candidate = SeriesCandidate(
        canonical_title=title,
        canonical_year=payload.get("year"),
        sources=alt_sources,
    )

    # Per-run fetch memo: nothing repeats WITHIN this call (no probe phase
    # on the prefetched/resume path — chapter lists are fetched exactly
    # once), but it dedupes any (site, url) that appears twice in the
    # payload and keeps this entry point on the same code path as the
    # search-driven ones.
    fetch_memo = FetchMemo()
    source_records = _fetch_chapters_for_winner(
        alt_candidate, args, make_request, on_status=on_status,
        fetch_memo=fetch_memo,
    )

    # Same result as find_alternatives_for_direct_url — primary is the anchor
    # (user picked this URL). resolved_sources echoes the sources that survived
    # the SKIP_MULTI_SOURCE / validity filter, so aio-dl.py re-persists a
    # cleaned resume cache.
    return build_alternatives_from_records(
        source_records, alt_sources, primary_handler, primary_chapters, args,
    )


__all__ = [
    "run_search_mode",
    "take_latest_multi_source_state",
    "find_alternatives_for_direct_url",
    "build_alternatives_from_prefetched",
    "build_alternatives_from_payload",
    "build_alternatives_from_records",
    "alternatives_from_search_state",
]
