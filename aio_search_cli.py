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
from typing import Any, Callable, Optional

# cloudscraper is optional (matches aio-dl.py:362–366)
try:
    import cloudscraper  # type: ignore
except Exception:  # pragma: no cover
    cloudscraper = None  # type: ignore

import requests

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from sites import get_handler_by_name
from sites.base import SearchHit
from sites.chapter_merger import _classify_main_chapters, align_chapter_lists
from sites.search_orchestrator import (
    DEFAULT_MIN_MATCH,
    DEFAULT_PER_SITE_TIMEOUT_S,
    ImageQualityCache,
    ProbeFailureCache,
    search_all,
)


def _try_extract_seed_hit(query: str) -> Optional[SearchHit]:
    """If `query` is a URL we can scrape into a SearchHit, return it.

    Currently MangaFire-only: the user's "URL-mode --search" sidequest exists
    because MangaFire's typeahead is unreliable and they need a way to GUARANTEE
    MangaFire participates in cross-site comparison. Other sites' search is
    reliable enough that URL-mode adds no value there.

    Returns None if `query` isn't a URL or isn't a MangaFire URL — caller
    proceeds with normal title-based search using `query` as-is.

    Raises SystemExit if the URL appears to be MangaFire but the page can't
    be scraped (bad slug, network error, etc.) — the user expected a usable
    seed and we can't deliver, so fail loudly rather than silently swallow.
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
    if not host.endswith("mangafire.to"):
        sys.exit(
            f"--search received a URL but it's not from MangaFire: {host}\n"
            "URL-mode --search is currently MangaFire-only (the typeahead's "
            "intermittency is what motivated this feature). For other sites, "
            "search by title."
        )
    # MangaFire URL → scrape via Playwright bridge.
    try:
        from sites.mangafire_vrf_simple import get_vrf_generator, PLAYWRIGHT_AVAILABLE
    except Exception as exc:
        sys.exit(f"MangaFire bridge unavailable: {exc}")
    if not PLAYWRIGHT_AVAILABLE:
        sys.exit(
            "URL-mode --search requires Patchright. "
            "Install with: pip install patchright && python -m patchright install chromium"
        )
    try:
        meta = get_vrf_generator().capture_series_meta(q)
    except Exception as exc:
        sys.exit(f"Failed to scrape MangaFire URL: {exc}")
    title = (meta or {}).get("title")
    if not title:
        sys.exit(f"MangaFire URL didn't yield a title: {q}")
    return SearchHit(
        site="mangafire",
        title=title,
        url=meta.get("final_url") or q,
        cover=meta.get("cover"),
        alt_titles=[],
        year=None,
        language=None,
        chapter_count_hint=meta.get("chapter_count"),
        actual_chapter_count=None,
        dmca_likely=False,
        # 1.0 because the title came directly from the URL's series page —
        # the user's intent is unambiguous, so seed it as a perfect match.
        raw_score=1.0,
    )


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
    """Mirror of aio-dl.py main()'s scraper creation. Single-source; if you
    change the policy there, change here too."""
    use_cloudscraper = cloudscraper is not None and sys.version_info >= (3, 7)
    if use_cloudscraper:
        try:
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "mobile": False}
            )
        except Exception:
            scraper = requests.Session()
    else:
        scraper = requests.Session()
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
    parallelism: int = 4,
    on_status=None,
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
    """
    if on_status:
        on_status(f"[*] Fetching chapter lists from {len(candidate.sources)} sources...")

    language = (
        getattr(args, "language", None)
        or getattr(args, "search_language", None)
        or "en"
    )

    def _fetch_one(source) -> dict:
        handler = get_handler_by_name(source.site)
        if handler is None:
            return {
                "site": source.site, "url": source.url,
                "chapters": [], "scraper": None, "handler": None, "context": None,
            }
        scraper = _build_scraper(args)
        try:
            handler.configure_session(scraper, args)
        except Exception:
            pass
        try:
            ctx = handler.fetch_comic_context(source.url, scraper, make_request)
        except Exception as exc:
            if on_status:
                on_status(f"  {source.site}: fetch_comic_context failed ({type(exc).__name__})")
            return {
                "site": source.site, "url": source.url,
                "chapters": [], "scraper": scraper, "handler": handler, "context": None,
            }
        try:
            chapters = handler.get_chapters(ctx, scraper, language, make_request)
        except Exception as exc:
            if on_status:
                on_status(f"  {source.site}: get_chapters failed ({type(exc).__name__})")
            return {
                "site": source.site, "url": source.url,
                "chapters": [], "scraper": scraper, "handler": handler, "context": ctx,
            }
        if not isinstance(chapters, list):
            chapters = []
        return {
            "site": source.site, "url": source.url,
            "chapters": chapters, "scraper": scraper, "handler": handler, "context": ctx,
        }

    workers = max(1, min(parallelism, len(candidate.sources)))
    results: list = [None] * len(candidate.sources)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ms-chap") as pool:
        futures = {pool.submit(_fetch_one, src): idx for idx, src in enumerate(candidate.sources)}
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
    return [r for r in results if r is not None]


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

    # Set the ML-rating gate BEFORE the orchestrator imports torch-backed
    # modules. When False (the default), all torch/pyiqa/torchmetrics
    # imports stay deferred — search startup never loads torch. This
    # avoids the Python 3.13 + Windows WMI hang that bricked --search
    # before the 2026-05-20 lazy-import refactor. set_ml_rating_enabled
    # is idempotent so direct-URL multi-source paths can also call it.
    # Cross-file: aio-dl.py argparse defines --enable-ml-rating;
    # UI-source/src/components/SearchTab.jsx writes enableMlRating to
    # settings.searchOpts; UI-source/electron/searcher.js translates that
    # back to --enable-ml-rating on the spawn line — grep enableMlRating.
    from sites.search_orchestrator import set_ml_rating_enabled
    set_ml_rating_enabled(bool(getattr(args, "enable_ml_rating", False)))

    # URL-mode: if the user supplied a MangaFire URL instead of a title text,
    # scrape the series page once via Playwright and turn it into a seed hit.
    # The orchestrator then runs the normal cross-site search using the
    # scraped title, with MangaFire's data already guaranteed to be present.
    seed_hits: list = []
    seed = _try_extract_seed_hit(query)
    if seed is not None:
        seed_hits.append(seed)
        # Use the scraped title as the effective query for other sites — the
        # original URL is meaningless to non-MangaFire handlers.
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
        # Search mode always probes ALL sources (including the seed
        # source, when --search was given a URL) so the JSON output
        # surfaces a real comparable img_quality_score for every
        # candidate. Quality skip is reserved for direct-URL
        # --multi-source (find_alternatives_for_direct_url below),
        # where the primary is already committed and probing it is
        # waste. SKIP_QUALITY_PROBE class-attr handlers (currently
        # comix) opt out unconditionally via search_orchestrator's
        # per-source loop — no plumbing needed here.
        on_status=_status,
        seeded_only=bool(getattr(args, "seeded_only", False)),
    )

    multi_source = bool(getattr(args, "multi_source", False))

    # Phase 4a/b — multi-source: fetch chapter lists for the winner's sources,
    # align them, and (Phase 4b) build a per-chapter alternatives dict that
    # aio-dl.py's _process_chapter_strict consumes for automatic fallback.
    winner_chapter_map = None
    multi_source_alternatives_payload = None  # exposed to aio-dl.py main() via the search-mode return
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
                winner, args, make_request, on_status=_status
            )
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
            # Phase 4b: build per-chapter alternatives indexed by chapter number.
            # Each entry includes the alternative source's handler+scraper+context
            # so _process_chapter_strict can swap them in via nonlocal and call
            # _process_chapter without re-running fetch_comic_context.
            # The site picked as anchor in alignment is what aio-dl.py will use
            # as primary; everything else becomes a fallback.
            anchor_site = next(
                (s for s, d in alignment.merge_diagnostics.items() if d.get("role") == "anchor"),
                None,
            )
            records_by_site = {rec["site"]: rec for rec in source_records}
            alternatives_by_chap_num: dict = {}
            for entry in alignment.chapter_map:
                alts = []
                for site, ch_dict in entry.sources:
                    if site == anchor_site:
                        continue
                    rec = records_by_site.get(site)
                    if not rec or rec["context"] is None or rec["handler"] is None:
                        continue
                    alts.append({
                        "site": site,
                        "url": rec["url"],
                        "chapter": ch_dict,
                        # scraper/handler/context aren't JSON-serializable; the
                        # search-mode runner stashes them on a side channel so
                        # aio-dl.py main() can pick them up. See _MULTI_SOURCE_STATE
                        # below.
                        "_scraper_token": id(rec["scraper"]),
                        "_handler_token": id(rec["handler"]),
                        "_context_token": id(rec["context"]),
                    })
                if alts:
                    alternatives_by_chap_num[entry.chapter_num] = alts
            multi_source_alternatives_payload = {
                "anchor_site": anchor_site,
                "alternatives_by_chap_num": alternatives_by_chap_num,
                "source_records": source_records,  # holds live scraper/handler/context refs
                # consensus_set passed in-memory (real set, not list) so
                # aio-dl.py's group_chapters_for_download call can refine
                # Rule 2 / 3b / 6 with peer-source confirmation. None when
                # alignment.consensus_set is empty (single source / no peer
                # overlap), to keep downstream's "no peer data" check simple.
                "consensus_set": (
                    alignment.consensus_set if alignment.consensus_set else None
                ),
                "consensus_max": alignment.consensus_max,
            }

    if json_output and not auto_pick:
        payload = {
            "query": query,
            "language": language,
            "candidates": [c.to_json() for c in candidates],
        }
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
    if multi_source_alternatives_payload is not None:
        anchor_site = multi_source_alternatives_payload.get("anchor_site")
        if anchor_site:
            anchor_record = next(
                (r for r in multi_source_alternatives_payload["source_records"]
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

    # Phase 4b: stash the multi-source state on a module attribute so
    # aio-dl.py main() can pick it up after run_search_mode returns. We can't
    # JSON-serialize live scrapers/handlers/contexts, so a side-channel is
    # the cleanest pass-through. main() consumes _LATEST_MULTI_SOURCE_STATE
    # immediately after the call.
    global _LATEST_MULTI_SOURCE_STATE
    _LATEST_MULTI_SOURCE_STATE = multi_source_alternatives_payload
    return primary_url


# Side-channel for passing multi-source state from run_search_mode to
# aio-dl.py main() without JSON-serializing live scrapers/handlers/contexts.
# Set inside run_search_mode (--auto-pick path with --multi-source); consumed
# by main() right after the call. None when multi-source is off.
_LATEST_MULTI_SOURCE_STATE = None


def take_latest_multi_source_state():
    """One-shot getter: returns the multi-source state payload from the most
    recent run_search_mode call and clears it. Side-channel for aio-dl.py
    main() to retrieve scrapers/handlers/contexts that can't ride through
    the str return value of run_search_mode."""
    global _LATEST_MULTI_SOURCE_STATE
    val = _LATEST_MULTI_SOURCE_STATE
    _LATEST_MULTI_SOURCE_STATE = None
    return val


def _filter_and_rank_alt_sources(sources, *, primary_host: str = "", quality_min: float = 0.65) -> list:
    """Filter winner candidate sources down to a quality-and-host-clean
    alternatives list, ranked by quality DESC.

    Why filter:
      - Sources with no seed entry default to seed_quality=0.50, which is
        a TIE among most Madara/MangaThemesia extras. Without filtering,
        the parallel-search-completion order picks an arbitrary one as
        first (e.g., klikmanga, an Indonesian site, ranking ahead of
        weebcentral for an English-language download).
      - Quality threshold (default 0.65) excludes unknown-quality sites
        which empirically correlate with foreign-language and parse-quirky
        handlers.
      - User can lower the threshold via --multi-source-quality-min for
        broader fallback coverage when willing to accept language drift.

    Sort key prefers measured img_quality_score over seed_quality, so a
    site that has been probed and scored well still wins even if its seed
    entry is missing. img_quality_score=None (unprobed) yields seed_quality
    as the comparison value; img_quality_score=0.0 (probed but every page
    failed — CDN poisoned) IS used as the rank input so a broken-CDN site
    correctly drops to the bottom.
    """
    eligible = []
    for s in sources:
        if primary_host and urlparse(s.url).netloc.lower() == primary_host:
            continue
        # Handlers that opt out of multi-source merging entirely via the
        # SKIP_MULTI_SOURCE class attribute (currently only comix — see
        # sites/comix.py for the why). These are sites whose chapter-list
        # fetch is wildly more expensive than other handlers in absolute
        # wall-clock terms (comix runs a ~25 s bridge-DOM-scrape per
        # candidate through a single-threaded Patchright worker), so
        # adding them to the alternatives pool delays --multi-source
        # alignment for the user's primary download without buying real
        # fallback coverage — the primary site almost always has the
        # same chapters. Treat the flag as equivalent to primary_host
        # exclusion: drop before the quality_min check so they never
        # reach _fetch_chapters_for_winner.
        handler = get_handler_by_name(s.site)
        if handler is not None and getattr(handler, "SKIP_MULTI_SOURCE", False):
            continue
        # Sources qualify by either explicit seed_quality OR a measured
        # img_quality_score >= 0.4 (rough "ok quality" floor). A measured
        # 0.0 (CDN-poisoned probe) does NOT qualify via the img_quality
        # path — that's the whole point of the multi-page aggregate fix.
        if s.seed_quality >= quality_min:
            eligible.append(s)
        elif s.img_quality_score is not None and s.img_quality_score >= 0.4:
            eligible.append(s)
    # Stable sort, quality DESC. _q: prefer measured over seed (incl. 0.0).
    def _q(s):
        return s.img_quality_score if s.img_quality_score is not None else s.seed_quality
    eligible.sort(key=_q, reverse=True)
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
         context (or fall back to MangaFire's capture_series_meta when
         the cloudscraper-based fetch_comic_context returned a stub).
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
    }
    # Step 1: title.
    title = (
        getattr(primary_context, "title", None)
        or (primary_context.comic.get("title") if getattr(primary_context, "comic", None) else None)
        or ""
    )
    title = (title or "").strip()
    # MangaFire's fetch_comic_context returns "Unknown Title" when CF blocks
    # the page (the JSON wrapper response we couldn't parse). The series-meta
    # bridge bypasses that.
    if (not title or title.lower() == "unknown title") and "mangafire.to" in (primary_url or ""):
        try:
            from sites.mangafire_vrf_simple import get_vrf_generator, PLAYWRIGHT_AVAILABLE
            if PLAYWRIGHT_AVAILABLE:
                meta = get_vrf_generator().capture_series_meta(primary_url)
                title = (meta or {}).get("title") or title
        except Exception as exc:
            if on_status:
                on_status(f"  multi-source: capture_series_meta failed: {type(exc).__name__}")
    if not title:
        if on_status:
            on_status("[!] Multi-source: couldn't determine title from URL; skipping alternatives discovery")
        return _empty_result

    if on_status:
        on_status(f"[*] Multi-source: searching for alternatives to '{title}'...")

    # Direct-URL multi-source enters here, separate from run_search_mode. Set
    # the ML-rating gate here too so the orchestrator's lazy torch checks see
    # the right state. Idempotent — calling twice in the same process is fine.
    from sites.search_orchestrator import set_ml_rating_enabled
    set_ml_rating_enabled(bool(getattr(args, "enable_ml_rating", False)))

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
        # Direct-URL --multi-source: the primary site is committed (we're
        # downloading from primary_url regardless). Skip its image-quality
        # probe — the score wouldn't affect our use of it, only ranking,
        # and the primary_host filter below drops it from alternatives
        # anyway so its score has no effect on the multi-source merge
        # either. On MangaFire (the canonical primary), this cuts ~30 s
        # of chapter-VRF + image-fetch work that the probe would otherwise
        # spend on the source we already chose.
        skip_probe_sites=(
            {primary_handler.name} if primary_handler is not None else None
        ),
        on_status=on_status,
        seeded_only=bool(getattr(args, "seeded_only", False)),
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
    alt_sources = _filter_and_rank_alt_sources(
        winner.sources,
        primary_host=primary_host,
        quality_min=quality_min,
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
        alt_candidate, args, make_request, on_status=on_status
    )

    # Step 5: align primary chapters with alt chapters. Primary is the anchor
    # (the user picked this URL, so its chapter set is canonical for the
    # download; alts are fallbacks for chapters the primary also has).
    primary_site = primary_handler.name
    sources_with_chapters: list = [(primary_site, primary_chapters or [])]
    for rec in source_records:
        sources_with_chapters.append((rec["site"], rec["chapters"]))

    alignment = align_chapter_lists(
        sources_with_chapters,
        collapse_splits=bool(getattr(args, "collapse_splits", False)),
    )

    # Step 6: build alternatives_by_chap_num.
    records_by_site = {rec["site"]: rec for rec in source_records}
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

    # consensus_set surfaced so the download path (group_chapters_for_download
    # at aio-dl.py:6541) can refine Rule 2 / 3b / 6 with peer confirmation.
    # None when alignment found no peer overlap (single eligible alt or all
    # alts diverged via compatibility threshold) — downstream falls through
    # to in-source-only heuristics.
    return {
        "alternatives_by_chap_num": alternatives_by_chap_num,
        "consensus_set": (
            alignment.consensus_set if alignment.consensus_set else None
        ),
        "consensus_max": alignment.consensus_max,
    }


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
    # (see ~line 647). Required at THIS entry point too because the prefetched
    # JSON path bypasses _filter_and_rank_alt_sources entirely — that filter
    # only runs on the direct-URL multi-source path
    # (find_alternatives_for_direct_url, above). Without this hook, comix gets
    # queued for a chapter-list fetch despite SKIP_MULTI_SOURCE=True and the
    # user pays a ~25 s Patchright bridge scrape per multi-source download
    # AND a ~5-minute canvas scrape per chapter when comix is hit as a
    # fallback alt — see sites/comix.py:99 for the why-not-multi-source
    # rationale.
    # Cross-file: SKIP_MULTI_SOURCE is set in sites/comix.py (the only handler
    # using it today). _filter_and_rank_alt_sources in this file does the same
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

    source_records = _fetch_chapters_for_winner(
        alt_candidate, args, make_request, on_status=on_status
    )

    # Same alignment + alts-dict construction as find_alternatives_for_direct_url
    # — primary is the anchor (user picked this URL).
    primary_site = primary_handler.name
    sources_with_chapters: list = [(primary_site, primary_chapters or [])]
    for rec in source_records:
        sources_with_chapters.append((rec["site"], rec["chapters"]))

    alignment = align_chapter_lists(
        sources_with_chapters,
        collapse_splits=bool(getattr(args, "collapse_splits", False)),
    )

    records_by_site = {rec["site"]: rec for rec in source_records}
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

    # Same consensus surfacing as find_alternatives_for_direct_url; consumed
    # by aio-dl.py at the group_chapters_for_download call site.
    return {
        "alternatives_by_chap_num": alternatives_by_chap_num,
        "consensus_set": (
            alignment.consensus_set if alignment.consensus_set else None
        ),
        "consensus_max": alignment.consensus_max,
    }


__all__ = [
    "run_search_mode",
    "take_latest_multi_source_state",
    "find_alternatives_for_direct_url",
    "build_alternatives_from_prefetched",
]
