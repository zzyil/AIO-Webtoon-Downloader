"""Per-run fetch memoization for the search / multi-source pipeline.

Owns: FetchMemo — a thread-safe, in-process cache of (site, url) → scraper /
comic context / chapter list, shared across the up-to-four phases of one
search or multi-source operation that all need the SAME data:

    matched-page probe (sites/match_session.py: chapter lists, scrapers via
      search_orchestrator._run_match_phase)
        → winner chapter fetch (aio_search_cli._fetch_chapters_for_winner)

Before 2026-07-12 each phase re-fetched fetch_comic_context + get_chapters
from scratch — the winner fetch was the 2nd-3rd network round trip for
identical data, and for comix (browser-driven chapter scrape, 5-50s) each
repeat cost real wall-clock. One FetchMemo per operation (built in
aio_search_cli: run_search_mode / find_alternatives_for_direct_url /
build_alternatives_from_payload) makes phases 2..n in-memory hits.

Deliberate non-goals (user decisions, 2026-07-12):
  - NO cross-process persistence. Chapter lists are the freshest signal we
    have and the user rejected a persistent chapter cache; download spawns
    and resumes re-fetch. This module lives and dies with one process run.
  - NO negative caching. A failed fetch propagates its exception and stores
    NOTHING, so a later phase with a different retry policy (winner fetch's
    bounded-retry shim) gets a genuine fresh attempt instead of a poisoned
    blank. Empty chapter lists likewise aren't stored.

Concurrency model: phases are sequential (the probe phase ends before the
winner fetch) and the probe phase dedupes (site, url) pairs across workers,
so same-key racing is rare; when it happens both threads fetch and
setdefault keeps the first result — a harmless duplicate fetch, never a
corrupt entry. The lock only guards dict access; network calls run outside
it so a slow fetch can't serialize unrelated keys.

Mutation isolation: get_chapters returns a DEEP COPY on every access.
Downstream mutates chapter dicts freely (aio_search_cli strips `_locked`
placeholder entries for alt sources, alignment/downloads stash keys like
`_aux_assets` on them) and those mutations must never leak between phases
or between sources. Contexts are returned by reference — they're treated as
read-mostly by every consumer and sharing them (same object the probe used)
is exactly the reuse we want.

Separate module (not in search_orchestrator) because match_session needs it
too and the orchestrator imports match_session; a leaf module keeps the
dependency graph acyclic and the class unit-testable without dragging in the
orchestrator.

Cross-file (grep fetch_memo):
  - sites/match_session.py — MatchSession(fetch_memo=) reads chapter lists
    through it (saves comix's 5-50s browser chapter scrape on reuse).
  - sites/search_orchestrator.py — search_all(fetch_memo=) threads it into
    _run_match_phase (scrapers + the session).
  - aio_search_cli.py — builds one per operation, passes it to search_all +
    _fetch_chapters_for_winner; _try_extract_seed_hit pre-populates it via
    put_context/put_chapters (the URL-seed path already paid for that data).
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple


def _copy_chapters(chapters: List[Dict]) -> List[Dict]:
    """Deep-copy a chapter list; degrade to per-dict shallow copies if some
    handler ever stashes a non-deepcopyable object on a chapter dict (none
    does today — chapter dicts are JSON-ish). The degraded copy still
    isolates the LIST and each dict's top level, which covers every known
    mutation site (`_locked` strip, `_aux_assets` stash)."""
    try:
        return copy.deepcopy(chapters)
    except Exception:
        return [dict(c) if isinstance(c, dict) else c for c in chapters]


class FetchMemo:
    """Per-run (site, url[, language]) → scraper / context / chapters memo.

    See the module docstring for scope, non-goals, and the concurrency
    model. All getters take the fetch inputs so a miss can populate the
    entry itself; exceptions from the underlying handler calls PROPAGATE
    (no negative caching)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Keyed per (site, url), NOT per site: two same-site urls can be
        # probed concurrently and cloudscraper sessions carry per-series CF
        # cookies; sharing one session per site would also funnel unrelated
        # probes through one connection pool.
        self._scrapers: Dict[Tuple[str, str], Any] = {}
        self._contexts: Dict[Tuple[str, str], Any] = {}
        self._chapters: Dict[Tuple[str, str, str], List[Dict]] = {}
        # Counters for the stats_line() diagnostic: X_hits = served from the
        # memo, X_fetches = went to the network (or handler) this run.
        self._ctx_hits = 0
        self._ctx_fetches = 0
        self._chap_hits = 0
        self._chap_fetches = 0

    # ------------------------------------------------------------ scrapers
    def get_scraper(self, site: str, url: str, builder: Callable[[], Any]) -> Any:
        """Return the scraper registered for (site, url), building one via
        ``builder()`` on first use. Reusing the probe phase's scraper in the
        winner fetch keeps CF clearances / cookies warm. The builder
        runs OUTSIDE the lock; a same-key race wastes one build and keeps
        the first registered instance."""
        key = (site, url)
        with self._lock:
            existing = self._scrapers.get(key)
        if existing is not None:
            return existing
        built = builder()
        if built is None:
            return None
        with self._lock:
            return self._scrapers.setdefault(key, built)

    # ------------------------------------------------------------ contexts
    def get_context(self, handler, url: str, scraper, make_request) -> Any:
        """Return the memoized SiteComicContext for (handler.name, url),
        fetching via handler.fetch_comic_context on a miss. Returned BY
        REFERENCE (read-mostly by all consumers). Exceptions propagate;
        a None result is returned but not stored."""
        key = (handler.name, url)
        with self._lock:
            ctx = self._contexts.get(key)
            if ctx is not None:
                self._ctx_hits += 1
                return ctx
        ctx = handler.fetch_comic_context(url, scraper, make_request)
        with self._lock:
            self._ctx_fetches += 1
            if ctx is not None:
                return self._contexts.setdefault(key, ctx)
        return ctx

    def put_context(self, site: str, url: str, context: Any) -> None:
        """Pre-register an already-fetched context (URL-seed path — the seed
        resolution in aio_search_cli._try_extract_seed_hit fetched it before
        the memo's phases run). No-op on None/empty inputs."""
        if not site or not url or context is None:
            return
        with self._lock:
            self._contexts.setdefault((site, url), context)

    # ------------------------------------------------------------ chapters
    def get_chapters(
        self, handler, url: str, language: str, scraper, make_request,
    ) -> List[Dict]:
        """Return a DEEP COPY of the memoized chapter list for
        (handler.name, url, language), fetching context + chapters on a
        miss. Language is part of the key because get_chapters filters by
        it (the probe hardcodes "en"; the winner fetch passes the run's
        language — a non-en run correctly misses the "en" entries and
        fetches its own). Non-empty results only are stored (no negative
        caching); exceptions propagate."""
        lang = language or "en"
        key = (handler.name, url, lang)
        with self._lock:
            cached = self._chapters.get(key)
            if cached is not None:
                self._chap_hits += 1
        if cached is not None:
            # Copy OUTSIDE the lock — deep-copying a 1000-chapter list under
            # the lock would serialize every other key behind it.
            return _copy_chapters(cached)
        ctx = self.get_context(handler, url, scraper, make_request)
        chapters = handler.get_chapters(ctx, scraper, lang, make_request)
        if not isinstance(chapters, list):
            chapters = []
        with self._lock:
            self._chap_fetches += 1
            if chapters:
                # Store a private copy so the copy handed back below can be
                # mutated freely without corrupting the memo.
                self._chapters.setdefault(key, _copy_chapters(chapters))
        return chapters

    def put_chapters(
        self, site: str, url: str, language: str, chapters: List[Dict],
    ) -> None:
        """Pre-register an already-fetched chapter list (URL-seed path).
        Stores a private copy; empty lists are ignored (same no-negative-
        caching rule as get_chapters)."""
        if not site or not url or not chapters:
            return
        with self._lock:
            self._chapters.setdefault(
                (site, url, language or "en"), _copy_chapters(chapters),
            )

    # ----------------------------------------------------------- reporting
    def stats_line(self) -> str:
        """One-line reuse summary for on_status output, e.g.
        ``[*] fetch-memo: contexts 6 reused / 8 fetched · chapter lists 7
        reused / 8 fetched``. Reused counts ≈ network round trips saved."""
        with self._lock:
            return (
                f"[*] fetch-memo: contexts {self._ctx_hits} reused / "
                f"{self._ctx_fetches} fetched · chapter lists "
                f"{self._chap_hits} reused / {self._chap_fetches} fetched"
            )


__all__ = ["FetchMemo"]
