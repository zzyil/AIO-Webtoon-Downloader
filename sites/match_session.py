"""Matched-page search ranking: the per-search coordinator.

Owns: fetching the SAME chapter numbers from every source of a search candidate,
matching their pages, comparing matched pages, and turning the comparisons into
one rating per source (`rate_sources`). Also the cache record format those
results persist as (`verdict_to_json` / `edge_to_json`; the cache itself is
`search_orchestrator.ImageQualityCache`, schema 9).
Read by: `search_orchestrator.search_all` (grep MatchSession, rate_sources).
Depends on: `page_ranker` (all pixel math), `strip_match` (webtoon geometry), the
handler task methods `match_readable_versions` / `match_fetch_window`
(sites/base.py), FetchMemo.
Design + evidence: plan `sync-temp/plans/image-ranker-rework.md`, sections
"P2 detailed design (rev 2)" and "P3 detailed design"; user decisions in its
"P2 decisions" / "P3 decisions" bullets.

How one session runs (all times monotonic):
- Workers (the caller's `parallelism`) pull TASKS from one priority queue: a
  `list` task per source (chapter list -> one version per whole number), then
  `window` tasks per (source, chapter). Window tasks are queued only once the
  candidate's targets are frozen, so no worker ever waits holding a slot, and
  every source's first window runs before any source's second (a slow site's
  queued windows must not hold the workers while a fast site's wait).
- Freeze (per candidate): when min(3, probing sources) lists are in, 8 s after
  the first list, or when every list is in. Sinks and sources covering under a
  third of the largest list do not count toward the quorum. Late lists read the
  frozen targets.
- The CALLER's thread is the coordinator and the single compute thread: it
  drains worker events, freezes candidates, assigns pages to clusters (same page
  across sources) and runs one comparison at a time. One thread because the
  comparator is GIL-bound (threads measured 1.05-1.14x), and keeping all cluster
  state on one thread needs no locks.
- Everything stops at the phase deadline, and one source stops at its
  per-source budget (SOURCE_BUDGET_S of time with at least one of ITS tasks
  running; waiting in the shared queue does not count, or a busy pool at the
  UI's 2-3 workers would cut sources that are not slow): no new task starts
  and the session stops WAITING for its running ones (grep _abandon_over_budget;
  the old probe's join gate, so one hung fetch cannot hold the phase to its
  deadline). Whatever was not fetched or compared leaves its source with fewer
  edges and a short cache TTL, and is never recorded as "unmatched" (grep
  _unrun), so the next search re-probes it.
- Chapter per slot: a slot is a chapter NUMBER, but a source fetches the chapter
  whose title carries the slot's episode label when titles have one (Tower of
  God's numbers differ by two across sites, its "Ep. N" labels do not; grep
  _chapter_for).
- Webtoons (grep "webtoon strips"): page matching runs first for every
  candidate. Once the windows seen read as a webtoon (strip_match.is_webtoon),
  each window is also stitched and located against the slot's other windows;
  pairs still unlinked when the slot's page comparisons are done get bands
  cut at the located position (_band_stage), which enter as clusters of
  kind "band" and are compared exactly like pages. Tall slices (h/w > 2) keep
  no full-size arrays and never get page comparisons (grep _registrable).

Rating (user decision 2026-10-18, "relative to best"): per source pair the
median score over shared pages, a ridge least-squares fit to one q per source,
and rating = 0.15 + 0.80*exp(-(q_best - q)/0.5) against the best original-edition
copy in the same connected group, so the best copy found reads ~0.95.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import page_ranker as pr
from . import strip_match as sm

# ---------------------------------------------------------------- tuning
# Page match rule measured in P2.2 (bench/image-ranker/match_eval.py, 9,975 page
# pairs): best thumbnail NCC >= 0.5 within the window, then the pair must
# register (>= 8 tiles, residual <= 1.5 px). 99.6% of registrable windows.
NCC_MIN = 0.5
REG_MIN_TILES = 8
REG_MAX_RMS = 1.5

FREEZE_QUORUM = 3
FREEZE_WAIT_S = 8.0
QUORUM_MIN_COVERAGE = 1 / 3

# Targets per source: the top candidate gets 3 chapters, others 1 (candidates
# are ranked by title match; a sub-series rarely wins the user's pick).
DEPTH_TOP = 3
DEPTH_OTHER = 1
TRIM_ENDS_MIN = 10   # drop the first/last 3 numbers only when >= 10 are covered
TRIM_ENDS = 3

# Comparisons per source pair worth making: the median of 4 matched pages is
# enough for an order; more mostly costs time (0.7 s per comparison).
PAIR_CAP = 4

# "Same scan" (the UI's same-scan note): the median tone-matched PSNR of a pair's
# compared pages at or above this. P1 evidence (bench/image-ranker): one scan
# re-encoded or resized by different hosts 18-39 dB, different scans <= 13.6 dB.
# Identical bytes/pixels are "same file" instead (PairEdge.same), never measured.
SAME_SCAN_DB = 16.0
# Cap on the stored PSNR: identical-after-tone-match pairs measure inf, and the
# value reaches the UI as JSON (JSON.parse rejects Infinity).
_PSNR_CAP = 99.0

# Episode-label pairing reaches this far from the slot's number (measured offsets
# between sites: 0-2, drifting along Tower of God).
LABEL_RANGE = 3

_POLL_S = 0.25

# Default per-source budget from its first task; a handler's
# PROBE_SOURCE_BUDGET_S (sites/base.py) overrides it.
SOURCE_BUDGET_S = 120.0

# Cache TTLs per verdict (D7: failure verdicts used to be dropped on reload, so a
# dead site was re-probed - and re-timed-out - on every search). verdict_ttl
# picks one.
TTL_SERVED_S = 30 * 24 * 3600
TTL_TRUNCATED_S = 7 * 24 * 3600
TTL_UNREACHABLE_S = 24 * 3600
TTL_PENDING_S = 2 * 3600


# ---------------------------------------------------------------- records

@dataclass
class _Page:
    src: str            # source key
    slot: int           # target chapter number this page answers for
    chap: int           # chapter number actually fetched (slot, or slot+-1 on retry)
    idx: int
    n_total: int
    copy: Optional[pr.PageCopy]


@dataclass
class _Cluster:
    """One page as served by several sources. Members are grouped into copy
    classes (byte- or pixel-identical copies), and only one member per class is
    ever compared: identical copies tie exactly."""
    slot: int
    members: Dict[str, _Page] = field(default_factory=dict)
    cls_of: Dict[str, int] = field(default_factory=dict)       # src -> class id
    classes: Dict[int, List[str]] = field(default_factory=dict)
    hub: Optional[int] = None                                  # class id of the widest copy
    edges: Dict[Tuple[int, int], Dict[str, float]] = field(default_factory=dict)  # (cls a, cls b) -> parts in a's favour
    failed: Set[int] = field(default_factory=set)               # classes whose match failed registration
    # "page": one served page per member, matched by thumbnail. "band": a piece of a
    # webtoon strip cut from each member's stitched window at one located position
    # (grep _band_stage); never matched against new pages.
    kind: str = "page"

    def rep(self, cid: int) -> _Page:
        return self.members[self.classes[cid][0]]

    def ref_copy(self) -> pr.PageCopy:
        return self.rep(self.hub).copy


@dataclass
class _Source:
    key: str
    site: str
    url: str
    cand: int
    entry: Any                       # SourceEntry (search_orchestrator)
    hit: Any                         # SearchHit
    handler: Any
    sink: bool = False
    list_state: str = "queued"       # queued | ok | failed | timeout | empty
    versions: Dict[int, Dict] = field(default_factory=dict)
    assigned: List[Tuple[int, int]] = field(default_factory=list)   # (chapter, slot)
    windows_open: int = 0
    windows_queued: int = 0          # ordinal of its next window task (fair queue order)
    windows_skipped: int = 0         # never ran or abandoned (deadline / budget): no evidence
    attempted: int = 0
    failed: int = 0
    timed_out: int = 0
    pages: List[_Page] = field(default_factory=list)
    busy_s: float = 0.0
    retried: Set[int] = field(default_factory=set)
    # budget clock = time with >= 1 of its tasks running (grep _active_s);
    # these three are touched only under MatchSession._inflight_lock
    running: int = 0
    active_since: float = 0.0
    active_s: float = 0.0
    over_budget: bool = False        # tasks skipped after handler.PROBE_SOURCE_BUDGET_S
    windows_ran: int = 0             # window results that came back (any outcome)
    windows_dead: int = 0            # ... that served nothing, with failures and no timeout
    retry_impossible: bool = False   # a dead window found no other chapter to retry
    fail_retried: bool = False       # one spare-chapter retry after a window served nothing

    @property
    def succeeded(self) -> int:
        return len(self.pages)


@dataclass
class _Cand:
    idx: int
    depth: int
    sources: List[_Source]
    frozen: bool = False
    first_list_t: Optional[float] = None
    targets: List[int] = field(default_factory=list)
    spares: List[int] = field(default_factory=list)
    clusters: Dict[int, List[_Cluster]] = field(default_factory=dict)
    slot_open: Dict[int, int] = field(default_factory=dict)      # slot -> windows still outstanding
    # --- webtoon strips (P3; sites/strip_match.py). Every served window is kept
    # here so strips can be built for it if the candidate turns out a webtoon later.
    windows: Dict[int, List[Tuple[str, int, List[_Page]]]] = field(default_factory=dict)
    webtoon: bool = False            # sticky; sm.is_webtoon over every window so far
    seen: List[int] = field(default_factory=lambda: [0, 0, 0, 0])   # pages, tall, continuing cuts, informative cuts
    strips: Dict[int, List[sm.StripWindow]] = field(default_factory=dict)
    links: Dict[int, List[Tuple[int, int, sm.Located]]] = field(default_factory=dict)  # accepted, indices into strips[slot]
    strip_tried: Set[str] = field(default_factory=set)    # sources with a locatable window that met a peer's
    strip_linked: Set[str] = field(default_factory=set)   # ... located against at least one peer
    band_pending: Set[int] = field(default_factory=set)   # slots whose band stage has not run since they completed
    band_cut: Set[Tuple[str, str]] = field(default_factory=set)    # pairs that got at least one band
    band_short: Set[Tuple[str, str]] = field(default_factory=set)  # located pairs no band could be cut for
    ref_labels: Dict[int, int] = field(default_factory=dict)       # slot -> episode label the slot stands for


# ---------------------------------------------------------------- results

@dataclass
class SourceVerdict:
    """What one source showed, independent of which peers are present. This is
    what the cache stores per source; `rate_sources` turns verdicts + pair edges
    into ratings for whichever set of sources a search has."""
    key: str
    status: str                      # served | unreachable | pending
    reason: str = ""
    edition: str = "undecided"       # original | alternate | undecided
    depth: int = 0                   # targets the probe asked for
    coverage: int = 0                # whole chapter numbers readable on this source
    attempted: int = 0
    succeeded: int = 0
    timed_out: int = 0
    chroma: float = 0.0              # median chroma share of served pages
    unmatched_against: List[str] = field(default_factory=list)
    truncated: bool = False          # the phase deadline or this source's budget cut it short
    chapters: List[int] = field(default_factory=list)


@dataclass
class PairEdge:
    """Median comparison of two sources over the pages they share, in a's favour
    (log-ratio units). parts = the sharp/res/blk terms of the median page(s), so
    the parts add up to `score` exactly. same = every shared page was byte- or
    pixel-identical: the two are one copy (rate_sources fits them as one node).
    psnr = median tone-matched PSNR over the pages that were compared (None when
    every shared page was identical, or for a cache record written before it
    existed); >= SAME_SCAN_DB reads as one scan re-encoded."""
    a: str
    b: str
    score: float
    n: int
    parts: Dict[str, float]
    at: float = 0.0
    same: bool = False
    psnr: Optional[float] = None

    def flipped(self) -> "PairEdge":
        return PairEdge(self.b, self.a, -self.score, self.n,
                        {k: -v for k, v in self.parts.items()}, self.at, self.same, self.psnr)


@dataclass
class Rating:
    basis: str                       # matched | partial | solo | unmatched | unreachable | pending
    rating: Optional[float] = None
    q: Optional[float] = None
    components: Dict[str, float] = field(default_factory=dict)
    tie_group: Optional[int] = None
    group: Optional[int] = None      # connected-group id (0 = the main group)
    edition: str = "undecided"
    peers: int = 0                   # distinct sources this one was compared with
    same_file: List[str] = field(default_factory=list)   # source keys serving identical copies
    same_scan: List[str] = field(default_factory=list)   # same scan re-encoded (not same_file)


# ---------------------------------------------------------------- the session

class MatchSession:
    """One search's matched-page probe over several candidates. Construct, then
    `run()` on the caller's thread; read `verdicts` / `edges` after.

    candidates: list of (candidate rank, depth, [ (SourceEntry, SearchHit,
    handler, sink) ]). get_scraper(entry, handler) builds or reuses a scraper.
    """

    def __init__(
        self,
        candidates: Sequence[Tuple[int, int, Sequence[Tuple[Any, Any, Any, bool]]]],
        *,
        get_scraper: Callable[[Any, Any], Any],
        make_request: Callable,
        fetch_memo=None,
        parallelism: int = 6,
        deadline_s: float = 240.0,
        on_status: Optional[Callable[[str], None]] = None,
        on_task: Optional[Callable[[str, str, Optional[float]], None]] = None,
    ) -> None:
        self.get_scraper = get_scraper
        self.make_request = make_request
        self.fetch_memo = fetch_memo
        self.parallelism = max(1, int(parallelism))
        self.deadline_s = float(deadline_s)
        self.on_status = on_status
        # on_task(site, url, started_at or None when finished): lets the caller
        # keep its in-flight map for the "still running at the deadline" line.
        self.on_task = on_task
        self.cands: List[_Cand] = []
        self.by_key: Dict[str, _Source] = {}
        for rank, depth, members in candidates:
            srcs = []
            for entry, hit, handler, sink in members:
                key = f"{entry.site}:{entry.url}"
                if key in self.by_key:
                    continue
                s = _Source(key=key, site=entry.site, url=entry.url, cand=len(self.cands),
                            entry=entry, hit=hit, handler=handler, sink=bool(sink))
                self.by_key[key] = s
                srcs.append(s)
            if srcs:
                self.cands.append(_Cand(idx=rank, depth=depth, sources=srcs))
        self._tasks: "queue.PriorityQueue" = queue.PriorityQueue()
        self._events: "queue.Queue" = queue.Queue()
        self._seq = 0
        self._outstanding = 0
        # seq -> (payload, started, thread) for every task a worker is running.
        # Shared with the workers under _inflight_lock: a worker adds its task
        # before running it and removes it when done; the coordinator removes
        # one to abandon it (grep _abandon_over_budget). Whoever removes the
        # entry owns the task's outcome, so a result is never both posted and
        # abandoned, and a worker that finds its entry gone exits.
        self._inflight: Dict[int, Tuple[Any, float, threading.Thread]] = {}
        self._inflight_lock = threading.Lock()
        # threads still blocked in an abandoned task: not pool capacity
        # (grep _top_up_workers); each exits once its fetch returns
        self._stuck: Set[threading.Thread] = set()
        self._jobs: List[Tuple[_Cand, _Cluster, int, int]] = []
        self._pair_done: Dict[Tuple[str, str], int] = {}
        self._src_done: Dict[str, int] = {}
        self._truncated = False
        # sources with a comparison that never ran (deadline, freed pixels, an
        # error): their missing edges say nothing about matching (grep _unrun)
        self._unrun: Set[str] = set()
        self.compare_s = 0.0
        self.compares = 0
        self.verdicts: Dict[str, SourceVerdict] = {}
        self.edges: List[PairEdge] = []

    # ------------------------------------------------------------ tasks
    def _put(self, cand_rank: int, kind: int, payload, ordinal: int = 0) -> None:
        # (candidate rank, kind, ordinal) ordering: the top candidate's windows
        # run before the next candidate's lists; within one candidate lists
        # first, then every source's window #0 before anyone's window #1.
        self._seq += 1
        self._outstanding += 1
        self._tasks.put((cand_rank, kind, ordinal, self._seq, payload))

    def _worker(self) -> None:
        from .crawlee_utils import interactive_solving
        while True:
            try:
                _, kind, _, seq, payload = self._tasks.get_nowait()
            except queue.Empty:
                return
            src = payload[0]
            t0 = time.monotonic()
            if t0 >= self._deadline:
                self._events.put(("skip", seq, payload))
                continue
            # Per-source budget (handler.PROBE_SOURCE_BUDGET_S, 120 s by
            # default, the old probe's): one slow site must not hold the whole
            # phase open. Its unrun windows are skipped like the deadline's -
            # slowness is never a quality verdict.
            with self._inflight_lock:
                skip = kind == 1 and (src.over_budget or
                                      _active_s(src, t0) >= _source_budget(src.handler))
                if skip:
                    src.over_budget = True
                else:
                    _start_running(src, t0)
                    self._inflight[seq] = (payload, t0, threading.current_thread())
            if skip:
                self._events.put(("skip", seq, payload))
                continue
            if self.on_task:
                self.on_task(src.site, src.url, t0)
            try:
                # Never open a browser for a series the user has not picked;
                # entered per task because worker threads start with an empty
                # context (memory contextvar-worker-thread-trap).
                with interactive_solving(False):
                    result = self._run_list(src) if kind == 0 else self._run_window(*payload)
            except Exception as exc:  # a task must never kill its worker
                result = ("error", repr(exc))
            dt = time.monotonic() - t0
            if self.on_task:
                self.on_task(src.site, src.url, None)
            with self._inflight_lock:
                abandoned = self._inflight.pop(seq, None) is None
                if not abandoned:
                    _stop_running(src, time.monotonic())
            if abandoned:
                return   # settled as a skip at its source's budget; its slot was refilled
            self._events.put(("list" if kind == 0 else "window", seq, payload, result, dt))

    def _scraper(self, src: _Source):
        return self.get_scraper(src.entry, src.handler)

    def _run_list(self, src: _Source):
        from .base import _PROBE_TIMEOUT_EXCEPTIONS, SiteBusyError
        try:
            scraper = self._scraper(src)
            if self.fetch_memo is not None:
                chapters = self.fetch_memo.get_chapters(src.handler, src.url, "en", scraper, self.make_request)
            else:
                ctx = src.handler.fetch_comic_context(src.url, scraper, self.make_request)
                chapters = src.handler.get_chapters(ctx, scraper, "en", self.make_request) if ctx is not None else []
        except (_PROBE_TIMEOUT_EXCEPTIONS, SiteBusyError):
            # SiteBusyError: this machine (a held browser profile), not the
            # site, failed the call - no verdict about the site
            return ("timeout", None)
        except Exception as exc:
            return ("failed", repr(exc))
        versions = src.handler.match_readable_versions(chapters)
        return ("ok" if versions else "empty", versions)

    def _run_window(self, src: _Source, chap: int, slot: int):
        scraper = self._scraper(src)
        res = src.handler.match_fetch_window(src.versions[chap], scraper, self.make_request)
        pages = []
        for idx, blob in res["pages"]:
            try:
                copy = pr.prepare(blob)
            except Exception:
                res["failed"] += 1
                continue
            pages.append(_Page(src=src.key, slot=slot, chap=chap, idx=idx, n_total=res["n_total"], copy=copy))
        res["pages"] = pages
        return ("ok", res)

    # ------------------------------------------------------------ run
    def run(self) -> None:
        t_start = time.monotonic()
        self._deadline = t_start + self.deadline_s
        for c in self.cands:
            for s in c.sources:
                self._put(c.idx, 0, (s,))
        workers = []
        for _ in range(min(self.parallelism, max(1, len(self.by_key)))):
            t = threading.Thread(target=self._worker, name="match-probe", daemon=True)
            t.start()
            workers.append(t)
        self._workers = workers
        while True:
            now = time.monotonic()
            if now >= self._deadline:
                self._truncated = self._truncated or self._outstanding > 0 or bool(self._jobs)
                break
            self._drain()
            self._abandon_over_budget(now)
            self._check_freezes()
            # Workers exit when the queue is momentarily empty; a freeze can
            # add tasks later, so keep the pool topped up every turn (also
            # while comparisons run, or new windows would sit unfetched).
            self._top_up_workers()
            ready = self._band_ready()
            if ready is not None:
                self._band_stage(*ready)
                continue
            if self._jobs:
                self._compute_one()
                continue
            if self._outstanding <= 0:
                break
            try:
                ev = self._events.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            self._handle(ev)
        self._drain()
        self._finalize()

    def _top_up_workers(self) -> None:
        # A thread blocked in an abandoned task is not capacity: counting it
        # would leave queued tasks waiting for a fetch nobody waits for.
        self._stuck = {t for t in self._stuck if t.is_alive()}
        alive = [t for t in self._workers if t.is_alive() and t not in self._stuck]
        need = min(self.parallelism, self._tasks.qsize()) - len(alive)
        for _ in range(max(0, need)):
            t = threading.Thread(target=self._worker, name="match-probe", daemon=True)
            t.start()
            alive.append(t)
        self._workers = alive

    def _drain(self) -> None:
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            self._handle(ev)

    def _abandon_over_budget(self, now: float) -> None:
        """Stop waiting for the running tasks of a source past its budget: each
        is settled now as a skip (a list as "timeout", a window as unfetched),
        and its worker drops the real result when the fetch finally returns.
        The thread itself runs on until then (daemon; a fetch cannot be
        interrupted) and is replaced in the pool now."""
        gone = []
        with self._inflight_lock:
            for seq, (payload, t0, thread) in list(self._inflight.items()):
                src = payload[0]
                if _active_s(src, now) < _source_budget(src.handler):
                    continue
                del self._inflight[seq]
                _stop_running(src, now)
                self._stuck.add(thread)
                gone.append((seq, payload, t0))
        for seq, payload, t0 in gone:
            src = payload[0]
            src.over_budget = True
            # its result (and dt) never arrives; the time spent so far still
            # counts, or a hung site reads as fast in probe_times' health signal
            src.busy_s += now - t0
            self._handle(("skip", seq, payload))

    def _handle(self, ev) -> None:
        kind = ev[0]
        self._outstanding -= 1
        if kind == "skip":
            payload = ev[2]
            src = payload[0]
            if len(payload) == 1:
                src.list_state = "timeout"
                self._on_list_done(src)
            else:
                src.windows_skipped += 1
                self._on_window_done(src, payload[2], None)
            return
        _, _, payload, result, dt = ev
        src = payload[0]
        src.busy_s += dt
        if kind == "list":
            state, data = result
            if state == "ok":
                src.versions = data
            src.list_state = state if state in ("ok", "failed", "timeout", "empty") else "failed"
            self._on_list_done(src)
            return
        state, res = result
        src.windows_ran += 1
        if state != "ok":
            src.attempted += 1
            src.failed += 1
            src.windows_dead += 1
            self._on_window_done(src, payload[2], None, failed=True)
            return
        src.attempted += res["attempted"]
        src.failed += res["failed"]
        src.timed_out += res["timed_out"]
        src.pages.extend(res["pages"])
        dead = not res["pages"] and res["failed"] > 0 and not res["timed_out"]
        src.windows_dead += dead
        self._on_window_done(src, payload[2], res["pages"], failed=dead)

    # ------------------------------------------------------------ freeze + targets
    def _cand_of(self, src: _Source) -> _Cand:
        return self.cands[src.cand]

    def _on_list_done(self, src: _Source) -> None:
        c = self._cand_of(src)
        if c.first_list_t is None and src.list_state == "ok":
            c.first_list_t = time.monotonic()
        if c.frozen:
            if src.list_state == "ok":
                self._assign(c, src)
            for slot in list(c.clusters):
                self._release_singletons(c, slot)
            return
        self._check_freeze(c)

    def _check_freezes(self) -> None:
        for c in self.cands:
            if not c.frozen:
                self._check_freeze(c)

    def _check_freeze(self, c: _Cand) -> None:
        listed = [s for s in c.sources if s.list_state == "ok"]
        done = all(s.list_state != "queued" for s in c.sources)
        if not listed:
            if done:
                c.frozen = True
            return
        biggest = max(len(s.versions) for s in listed)
        counting = [s for s in listed if not s.sink and len(s.versions) >= QUORUM_MIN_COVERAGE * biggest]
        probing = [s for s in c.sources if not s.sink] or c.sources
        quorum = min(FREEZE_QUORUM, len(probing))
        timed = c.first_list_t is not None and time.monotonic() - c.first_list_t >= FREEZE_WAIT_S
        if done or len(counting) >= quorum or timed:
            self._freeze(c)

    def _freeze(self, c: _Cand) -> None:
        c.frozen = True
        listed = [s for s in c.sources if s.list_state == "ok"]
        c.targets, c.spares = choose_targets([set(s.versions) for s in listed], c.depth)
        c.ref_labels = _reference_labels(listed, list(c.targets) + list(c.spares))
        for s in listed:
            self._assign(c, s)

    def _chapter_for(self, c: _Cand, src: _Source, slot: int) -> Optional[int]:
        """The chapter of src that holds the slot's content: the one whose title
        names the slot's reference episode (unique within +-LABEL_RANGE numbers;
        Tower of God: atsu "Ep. 164" at 243 = LINE "[S2] Ep. 164" at 245), else
        the slot's own number. User decision 2026-10-20 (episode labels: yes).
        Titles without an episode number (asura, weebcentral) keep the number."""
        own = slot if slot in src.versions else None
        ref = c.ref_labels.get(slot)
        if ref is None:
            return own
        if own is not None and sm.episode_number(src.versions[own].get("title")) in (ref, None):
            return own
        hits = [n for n in range(slot - LABEL_RANGE, slot + LABEL_RANGE + 1)
                if n in src.versions and sm.episode_number(src.versions[n].get("title")) == ref]
        return hits[0] if len(hits) == 1 else own

    def _assign(self, c: _Cand, src: _Source) -> None:
        """Queue this source's windows: the frozen targets it has, and for each
        target it lacks the next spare it has. A spare only helps if a peer
        fetches it too, so the peer with the longest list that has it joins.
        Each slot is fetched at the chapter `_chapter_for` names."""
        picks = []
        for t in c.targets:
            ch = self._chapter_for(c, src, t)
            if ch is not None:
                picks.append((ch, t))
        missing = len(c.targets) - len(picks)
        if missing:
            for sp in c.spares:
                if missing <= 0:
                    break
                ch = self._chapter_for(c, src, sp)
                if ch is not None:
                    picks.append((ch, sp))
                    missing -= 1
                    self._ensure_peer_fetches(c, src, sp)
        for chap, slot in picks:
            self._queue_window(c, src, chap, slot)

    def _ensure_peer_fetches(self, c: _Cand, src: _Source, slot: int) -> None:
        if any(sl == slot for s in c.sources if s is not src for _, sl in s.assigned):
            return
        peers = [s for s in c.sources if s is not src and s.list_state == "ok" and not s.sink
                 and self._chapter_for(c, s, slot) is not None]
        if not peers:
            return
        peer = max(peers, key=lambda s: (len(s.versions), s.key))
        self._queue_window(c, peer, self._chapter_for(c, peer, slot), slot)

    def _queue_window(self, c: _Cand, src: _Source, chap: int, slot: int) -> None:
        if (chap, slot) in src.assigned:
            return
        src.assigned.append((chap, slot))
        src.windows_open += 1
        c.slot_open[slot] = c.slot_open.get(slot, 0) + 1
        self._put(c.idx, 1, (src, chap, slot), ordinal=src.windows_queued)
        src.windows_queued += 1

    # ------------------------------------------------------------ matching
    def _on_window_done(self, src: _Source, slot: int, pages: Optional[List[_Page]],
                        failed: bool = False) -> None:
        c = self._cand_of(src)
        src.windows_open -= 1
        c.slot_open[slot] = c.slot_open.get(slot, 1) - 1
        if pages:
            self._add_pages(c, slot, src, pages)
            self._strip_window(c, slot, src, pages)
        if failed and src.succeeded == 0 and not src.fail_retried:
            self._retry_failed(c, src)
        if c.slot_open[slot] <= 0:
            self._slot_complete(c, slot)

    def _retry_failed(self, c: _Cand, src: _Source) -> None:
        """A source whose window served nothing (every fetch failed, none timed
        out) and that has served nothing yet tries ONE other chapter before
        the session calls it unreachable: one locked or broken chapter must
        not sink a site for a day. The retry chapter is the first spare (or
        unused target) it has; a peer fetches it too."""
        src.fail_retried = True
        used = {ch for ch, _ in src.assigned}
        for sp in list(c.spares) + list(c.targets):
            if sp in src.versions and sp not in used:
                self._ensure_peer_fetches(c, src, sp)
                self._queue_window(c, src, sp, sp)
                return
        src.retry_impossible = True

    def _add_pages(self, c: _Cand, slot: int, src: _Source, pages: List[_Page]) -> None:
        """Assign a window's pages to this chapter's clusters: each cluster takes
        the window page with the best thumbnail NCC against its widest copy
        (>= NCC_MIN), greedily by NCC, one page per source per cluster. Pages
        that match nothing start clusters of their own."""
        clusters = c.clusters.setdefault(slot, [])
        cand_pairs = []
        for ci, cl in enumerate(clusters):
            if src.key in cl.members or cl.kind != "page":
                continue
            ref = cl.ref_copy()
            for pi, p in enumerate(pages):
                if p.copy.tall or ref.thumb is None:
                    # a strip slice has no thumbnail; it joins only an identical copy
                    if any(m.copy is not None and (m.copy.sha1 == p.copy.sha1 or m.copy.pixhash == p.copy.pixhash)
                           for m in cl.members.values()):
                        cand_pairs.append((2.0, ci, pi))
                    continue
                v = pr.ncc(ref, p.copy)
                if v >= NCC_MIN:
                    cand_pairs.append((v, ci, pi))
        cand_pairs.sort(key=lambda t: -t[0])
        used_c: Set[int] = set()
        used_p: Set[int] = set()
        for v, ci, pi in cand_pairs:
            if ci in used_c or pi in used_p:
                continue
            used_c.add(ci)
            used_p.add(pi)
            self._join(c, clusters[ci], pages[pi])
        for pi, p in enumerate(pages):
            if pi not in used_p:
                cl = _Cluster(slot=slot)
                clusters.append(cl)
                self._join(c, cl, p)

    def _join(self, c: _Cand, cl: _Cluster, p: _Page) -> None:
        cl.members[p.src] = p
        for cid, keys in cl.classes.items():
            other = cl.members[keys[0]].copy
            if other.sha1 == p.copy.sha1 or other.pixhash == p.copy.pixhash:
                keys.append(p.src)
                cl.cls_of[p.src] = cid
                return
        cid = len(cl.classes)
        cl.classes[cid] = [p.src]
        cl.cls_of[p.src] = cid
        if cl.hub is None:
            cl.hub = cid
            return
        old_hub = cl.hub
        if pr._orient_key(p.copy) > pr._orient_key(cl.ref_copy()):
            cl.hub = cid
        # compare the new class with the hub it found on arrival; when it becomes
        # the hub itself, with the old hub (the edges form a tree, which is all
        # the latent fit needs to connect the copies)
        if not _registrable(p.copy, cl.rep(old_hub).copy):
            # settled exactly as the comparison would end, without paying for
            # it (a 14 Mpx slice cost ~670 MB in measure)
            cl.failed.add(cid)
            return
        self._jobs.append((c, cl, cid, old_hub))

    def _slot_complete(self, c: _Cand, slot: int) -> None:
        """Every window of this chapter is in. A source none of whose pages met
        another source's here gets one retry at the neighbouring chapter numbers
        (numbering offsets by one between sites are common: a prologue counted as
        chapter 1). Single-copy clusters release their pixels."""
        clusters = c.clusters.get(slot, [])
        matched = {k for cl in clusters if len(cl.members) > 1 for k in cl.members}
        # a strip located against a peer's is matched too: its bands come later
        # (_band_stage), and retrying it would fetch two chapters for nothing
        strips = c.strips.get(slot, [])
        matched |= {strips[i].src for i, j, _ in c.links.get(slot, ())} | \
                   {strips[j].src for i, j, _ in c.links.get(slot, ())}
        others = {k for cl in clusters for k in cl.members}
        if len(others) >= 2:
            for s in c.sources:
                if s.key in matched or s.list_state != "ok" or slot in s.retried:
                    continue
                if not any(sl == slot for _, sl in s.assigned):
                    continue
                s.retried.add(slot)
                for alt in (slot - 1, slot + 1):
                    if alt in s.versions and (alt, slot) not in s.assigned:
                        self._queue_window(c, s, alt, slot)
        if c.webtoon:
            c.band_pending.add(slot)
        elif c.slot_open.get(slot, 0) <= 0:
            self._release_blobs(c, slot)
        self._release_singletons(c, slot)

    def _release_singletons(self, c: _Cand, slot: int) -> None:
        """Free the pixels of pages no other source matched in a finished
        chapter. Only once every list of the candidate is in: a list arriving
        after the freeze can still queue windows here, and its pages would match
        by thumbnail but could not be compared."""
        if c.slot_open.get(slot, 0) > 0 or any(s.list_state == "queued" for s in c.sources):
            return
        for cl in c.clusters.get(slot, []):
            if len(cl.members) == 1:
                for p in cl.members.values():
                    if p.copy is not None:
                        p.copy.gray = None  # type: ignore[assignment]

    # ------------------------------------------------------------ webtoon strips
    # P3 (plan "P3 detailed design"): page matching stays the first path for every
    # candidate. On a candidate whose windows read as a webtoon (sm.is_webtoon),
    # each window is also stitched into a strip and located against the slot's
    # other strips; pairs page matching left unlinked are then compared on bands
    # cut at the located position, as ordinary clusters (kind="band"). Manga
    # candidates never enter this path (user decision 2026-10-20: manga results
    # stay as they are; the detector was 111/111 on the user's library).

    def _strip_window(self, c: _Cand, slot: int, src: _Source, pages: List[_Page]) -> None:
        chap = pages[0].chap
        c.windows.setdefault(slot, []).append((src.key, chap, pages))
        co, inf = sm.seam_stats(pages)
        c.seen[0] += len(pages)
        c.seen[1] += sum(1 for p in pages if p.copy is not None and p.copy.tall)
        c.seen[2] += co
        c.seen[3] += inf
        if c.webtoon:
            self._locate_window(c, slot, src.key, chap, pages)
            return
        if not sm.is_webtoon(*c.seen):
            if c.seen[0] >= sm.MIN_PAGES:
                # judged on enough pages and not a webtoon: the served bytes kept
                # for a later band cut are dead weight on a manga search (peaks
                # there are set by P2's comparisons, so this is not a peak fix).
                # Should it still flip later, these slots' pairs end unrun
                # (cut_band finds no blob), never "unmatched".
                for wins in c.windows.values():
                    for _k, _ch, pg in wins:
                        for p in pg:
                            if p.copy is not None:
                                p.copy.blob = None
            return
        c.webtoon = True
        # windows that arrived before the call: strips for all of them, and
        # their completed slots get a band stage (blobs of slots already
        # released leave those pairs unrun, never "unmatched")
        for sl, wins in c.windows.items():
            for key, ch, pg in wins:
                self._locate_window(c, sl, key, ch, pg)
            if c.slot_open.get(sl, 0) <= 0:
                c.band_pending.add(sl)

    def _locate_window(self, c: _Cand, slot: int, key: str, chap: int, pages: List[_Page]) -> None:
        win = sm.build_window(key, chap, slot, pages)
        if win is None:
            return
        strips = c.strips.setdefault(slot, [])
        links = c.links.setdefault(slot, [])
        j = len(strips)
        strips.append(win)
        for i, other in enumerate(strips[:-1]):
            if other.src == key:
                continue
            c.strip_tried.update((key, other.src))
            try:
                loc = sm.snap_identical(other, win, sm.locate(other, win))
            except Exception:
                # a bug or odd input, not a verdict: the pair is unrun, and
                # one strip must not end the session (as _compute_job)
                self._unrun.update((key, other.src))
                continue
            if loc.accepted:
                links.append((i, j, loc))
                c.strip_linked.update((key, other.src))

    def strip_note(self) -> str:
        """Suffix for the orchestrator's probe status line (grep strip_note): how many
        candidates took the webtoon path and how many band clusters were cut; empty
        when none did, so a manga search's line is P2's."""
        web = [c for c in self.cands if c.webtoon]
        if not web:
            return ""
        bands = sum(1 for c in web for cls in c.clusters.values() for cl in cls if cl.kind == "band")
        return f"; webtoon path on {len(web)} candidate(s), {bands} band(s)"

    def _page_linked(self, c: _Cand) -> Set[Tuple[str, str]]:
        """Source pairs the page path already links in this candidate: an identical
        copy class or a registered edge in any page cluster."""
        out: Set[Tuple[str, str]] = set()
        for clusters in c.clusters.values():
            for cl in clusters:
                if cl.kind != "page":
                    continue
                for (a, b), _parts in _cluster_source_pairs(cl):
                    out.add((min(a, b), max(a, b)))
        return out

    def _band_ready(self) -> Optional[Tuple[_Cand, int]]:
        """A slot whose band stage can run: all its windows in, and none of its
        page comparisons still queued (they decide which pairs still need bands)."""
        for c in self.cands:
            for slot in sorted(c.band_pending):
                if c.slot_open.get(slot, 0) > 0:
                    continue
                if any(jc is c and jcl.slot == slot and jcl.kind == "page" for jc, jcl, _, _ in self._jobs):
                    continue
                return c, slot
        return None

    def _band_stage(self, c: _Cand, slot: int) -> None:
        """Cut bands for the slot's located strip pairs that no page path links and
        turn each into a cluster (copy classes, hub jobs, PAIR_CAP: P2 as is).
        At most sm.PAIR_BANDS bands per pair here, so one chapter cannot decide a
        pair. Located pairs no band could be cut for (overlap too short, too
        little texture, pixels released) are unrun: located is not compared."""
        c.band_pending.discard(slot)
        strips = c.strips.get(slot, [])
        links = c.links.get(slot, [])
        linked = self._page_linked(c)
        need: Dict[Tuple[str, str], int] = {}
        for i, j, _ in links:
            pair = tuple(sorted((strips[i].src, strips[j].src)))
            if pair not in linked:
                need[pair] = 0
        if need:
            try:
                self._cut_bands(c, slot, strips, links, need)
            except Exception:
                # a bad decode or a bug: the pairs left without a band end unrun
                # below, never "unmatched", and the session goes on (as _compute_job)
                pass
            for pair in need:
                if pair not in c.band_cut:
                    c.band_short.add(pair)
        if c.slot_open.get(slot, 0) <= 0:
            self._release_blobs(c, slot)

    def _cut_bands(self, c: _Cand, slot: int, strips, links, need: Dict[Tuple[str, str], int]) -> None:
        """The cutting half of _band_stage: one band cluster per chosen band with
        >= 2 textured pieces from the needed pairs' sources; records cut pairs in
        c.band_cut. Pieces are full-width native rows (sm.cut_band), so they meet
        through the same measure as pages."""
        decoded: Dict[int, Any] = {}
        n_band = sum(1 for cl in c.clusters.get(slot, []) if cl.kind == "band")
        srcs = {k for pair in need for k in pair}
        for group in sm.place(strips, links):
            for band in sm.choose_bands(group, need):
                pieces = []
                for pl in band.members:
                    if pl.win.src not in srcs or any(pc[0].win.src == pl.win.src for pc in pieces):
                        continue
                    arr = sm.cut_band(pl, band.f0, band.f1, decoded)
                    if arr is None or pr.textured_tiles(arr) < sm.MIN_TEXTURED:
                        continue
                    pieces.append((pl, arr))
                if len(pieces) < 2:
                    continue
                n_band += 1
                cl = _Cluster(slot=slot, kind="band")
                c.clusters.setdefault(slot, []).append(cl)
                for pl, arr in pieces:
                    # the edition check reads chroma: the band's own pages decide it
                    y0, y1 = sm.native_rows(pl, band.f0, band.f1)
                    spanned = sorted(p.copy.chroma for p, ys in zip(pl.win.pages, pl.win.y0)
                                     if p.copy is not None and ys < y1 and ys + p.copy.native_h > y0)
                    chroma = spanned[len(spanned) // 2] if spanned else 0.0
                    page = _Page(src=pl.win.src, slot=slot, chap=pl.win.chap, idx=-n_band,
                                 n_total=0, copy=pr.piece_copy(arr, chroma))
                    self._join(c, cl, page)
                keys = sorted(pl.win.src for pl, _ in pieces)
                for a_i, a in enumerate(keys):
                    for b in keys[a_i + 1:]:
                        c.band_cut.add((a, b))

    def _release_blobs(self, c: _Cand, slot: int) -> None:
        """Drop the served bytes of a finished slot's pages once no band can need
        them (manga: at slot completion; webtoon: after its band stage)."""
        for _key, _ch, pages in c.windows.get(slot, ()):
            for p in pages:
                if p.copy is not None:
                    p.copy.blob = None

    def _release_band(self, cl: _Cluster) -> None:
        """A band cluster with no comparison left frees its pieces (up to 4 Mpx each)."""
        if any(jcl is cl for _, jcl, _, _ in self._jobs):
            return
        for p in cl.members.values():
            if p.copy is not None:
                p.copy.gray = None

    # ------------------------------------------------------------ compute
    def _job_priority(self, job) -> Tuple[int, int, int]:
        c, cl, a, b = job
        sa, sb = cl.classes[a][0], cl.classes[b][0]
        pair = tuple(sorted((sa, sb)))
        # each source's first edge before anyone's second, then the thinnest pair
        return (min(self._src_done.get(sa, 0), self._src_done.get(sb, 0)),
                self._pair_done.get(pair, 0), c.idx)

    def _compute_one(self) -> None:
        i = min(range(len(self._jobs)), key=lambda k: self._job_priority(self._jobs[k]))
        c, cl, a, b = self._jobs.pop(i)
        try:
            self._compute_job(c, cl, a, b)
        finally:
            if cl.kind == "band":
                self._release_band(cl)

    def _compute_job(self, c: _Cand, cl: _Cluster, a: int, b: int) -> None:
        sa, sb = cl.classes[a][0], cl.classes[b][0]
        pair = tuple(sorted((sa, sb)))
        if self._pair_done.get(pair, 0) >= PAIR_CAP:
            return
        pa, pb = cl.rep(a), cl.rep(b)
        if pa.copy is None or pb.copy is None or pa.copy.gray is None or pb.copy.gray is None:
            self._unrun.update((sa, sb))
            return
        t0 = time.monotonic()
        try:
            m = pr.measure(pa.copy, pb.copy)
            ok = registered(m)
        except ValueError:
            # measure's own "cannot register these" signal: a real non-match
            m, ok = None, False
        except Exception:
            # any other failure is a bug or a bad decode, not a verdict about
            # the pair; it must not end the session (and every rating with it)
            self._unrun.update((sa, sb))
            self.compare_s += time.monotonic() - t0
            return
        self.compare_s += time.monotonic() - t0
        self.compares += 1
        if not ok:
            cl.failed.add(a)
            return
        parts = pr.score_parts(m)
        # "_"-keys ride along with the score terms but are not terms: never
        # summed, never negated on flip (grep "_psnr", median_edge)
        p = m.get("psnr_tone")
        if p is not None and p == p:
            parts["_psnr"] = min(float(p), _PSNR_CAP)
        cl.edges[(a, b)] = parts
        self._pair_done[pair] = self._pair_done.get(pair, 0) + 1
        for k in (sa, sb):
            self._src_done[k] = self._src_done.get(k, 0) + 1

    # ------------------------------------------------------------ finalize
    def _finalize(self) -> None:
        now = time.time()
        per_pair: Dict[Tuple[str, str], List[Dict[str, float]]] = {}
        for c in self.cands:
            for clusters in c.clusters.values():
                for cl in clusters:
                    for (ka, kb), parts in _cluster_source_pairs(cl):
                        if ka > kb:
                            ka, kb = kb, ka
                            parts = {k: (v if k.startswith("_") else -v) for k, v in parts.items()}
                        per_pair.setdefault((ka, kb), []).append(parts)
        self.edges = [median_edge(a, b, lst, now) for (a, b), lst in per_pair.items()]
        for _c, cl, a, b in self._jobs:   # left over at the deadline
            self._unrun.update((cl.classes[a][0], cl.classes[b][0]))
        for c in self.cands:
            # located pairs never compared: band stage cut by the deadline, or no
            # band could be cut (too short an overlap, too little texture, pixels
            # already released). Located is not compared, so no "unmatched".
            linked = self._page_linked(c)
            for slot in c.band_pending:
                strips = c.strips.get(slot, [])
                for i, j, _ in c.links.get(slot, ()):
                    pair = tuple(sorted((strips[i].src, strips[j].src)))
                    if pair not in linked:
                        self._unrun.update(pair)
            for pair in c.band_short - c.band_cut:
                self._unrun.update(pair)
        editions = self._editions()
        for c in self.cands:
            peers_served = [s.key for s in c.sources if s.succeeded > 0]
            linked = {k for e in self.edges for k in (e.a, e.b)}
            for s in c.sources:
                v = SourceVerdict(key=s.key, status="pending", depth=c.depth,
                                  coverage=len(s.versions), attempted=s.attempted,
                                  succeeded=s.succeeded, timed_out=s.timed_out,
                                  truncated=self._truncated or s.over_budget,
                                  chapters=sorted({ch for ch, _ in s.assigned}))
                if s.list_state == "failed":
                    v.status, v.reason = "unreachable", "list_failed"
                elif s.list_state == "empty":
                    v.status, v.reason = "unreachable", "no_readable"
                elif s.list_state in ("timeout", "queued"):
                    v.status, v.reason = "pending", "list_" + s.list_state
                elif s.succeeded > 0:
                    v.status = "served"
                elif (s.windows_ran and s.windows_dead == s.windows_ran and not s.windows_open
                      and not s.windows_skipped and (s.windows_dead >= 2 or s.retry_impossible)):
                    # the dead-CDN signal (cached a day): every window it was
                    # given RAN and served nothing, with failures and no
                    # timeout, on two chapters (the _retry_failed spare) unless
                    # it had no other. A window that timed out or was skipped
                    # (the retry, cut by the budget) leaves it pending: one
                    # failed chapter plus slowness is not a dead CDN.
                    v.status, v.reason = "unreachable", "pages_failed"
                elif not s.assigned and c.frozen:
                    v.reason = "no_shared_chapter"
                else:
                    v.reason = ("pages_timeout" if s.timed_out
                                else "pages_cut" if s.windows_skipped and s.attempted
                                else "not_run")
                if s.pages:
                    ch = sorted(p.copy.chroma for p in s.pages if p.copy is not None)
                    v.chroma = ch[len(ch) // 2] if ch else 0.0
                v.edition = editions.get(s.key, "undecided")
                # "unmatched" is cached (no re-probe against these peers), so it
                # needs every comparison and fetch to have run; otherwise the
                # source stays unlinked and _needs_probe re-probes it
                if (v.status == "served" and s.key not in linked and not s.over_budget
                        and not self._truncated and s.key not in self._unrun):
                    v.unmatched_against = [k for k in peers_served if k != s.key]
                    if c.webtoon:
                        # why a webtoon copy stayed unlinked; these cache for
                        # a week, not a month (verdict_ttl): new chapters may
                        # locate where these did not
                        v.reason = ("strip_unregistered" if s.key in c.strip_linked
                                    else "strip_unaligned" if s.key in c.strip_tried
                                    else "strip_short")
                self.verdicts[s.key] = v
        # release pixels: the session object may outlive the search (tests)
        for s in self.by_key.values():
            for p in s.pages:
                p.copy = None

    def _editions(self) -> Dict[str, str]:
        """Colorized-edition call with votes per COPY CLASS (identical mirrors
        vote once), on pages held by >= 3 classes; a source takes its class's
        verdict on each page. Majority rule of page_ranker.classify_editions."""
        dis: Dict[str, int] = {}
        seen: Dict[str, int] = {}
        for c in self.cands:
            for clusters in c.clusters.values():
                for cl in clusters:
                    if len(cl.classes) < 3:
                        continue
                    calls = {cid: cl.rep(cid).copy.chroma >= pr.COLOR_T for cid in cl.classes}
                    n, n_color = len(calls), sum(calls.values())
                    if 2 * n_color == n:
                        continue
                    major = 2 * n_color > n
                    for cid, call in calls.items():
                        for k in cl.classes[cid]:
                            seen[k] = seen.get(k, 0) + 1
                            dis[k] = dis.get(k, 0) + (call != major)
        return {k: "alternate" if 2 * dis.get(k, 0) >= seen[k] else "original" for k in seen}


# ---------------------------------------------------------------- pure helpers

def _active_s(src: _Source, now: float) -> float:
    """Seconds this source has had at least one task running (its budget
    clock). Caller holds MatchSession._inflight_lock."""
    return src.active_s + (now - src.active_since if src.running else 0.0)


def _start_running(src: _Source, now: float) -> None:
    if src.running == 0:
        src.active_since = now
    src.running += 1


def _stop_running(src: _Source, now: float) -> None:
    src.running -= 1
    if src.running == 0:
        src.active_s += now - src.active_since


def no_page_evidence(v: SourceVerdict) -> bool:
    """A verdict that says nothing about the pages a source serves: timed out
    / cut (`pending`), or its chapter list failed (`list_failed`, as often this
    machine as the site, see verdict_ttl). It must never replace a fresh
    `served` record (search_orchestrator: put_records and the per-candidate
    merge in _run_match_phase), or one reset connection on a re-probed anchor
    would sink a measured source and throw away its pairs."""
    return v.status == "pending" or (v.status == "unreachable" and v.reason == "list_failed")


def _source_budget(handler) -> float:
    try:
        return float(getattr(handler, "PROBE_SOURCE_BUDGET_S", SOURCE_BUDGET_S) or SOURCE_BUDGET_S)
    except (TypeError, ValueError):
        return SOURCE_BUDGET_S


def _registrable(a: pr.PageCopy, b: pr.PageCopy) -> bool:
    """False when the pair can never pass `registered`: a strip slice (compared
    through bands instead), or a reference whose tile grid holds fewer than
    REG_MIN_TILES tiles (`reg_n` never exceeds the grid; a 700x630 trimmed slice
    has 6). Outcome-identical to running the comparison."""
    if a.tall or b.tall:
        return False
    big = a if pr._orient_key(a) >= pr._orient_key(b) else b
    return pr.tile_capacity(big.height, big.width) >= REG_MIN_TILES


def registered(m: Dict[str, float]) -> bool:
    """P2.2 match gate on a measured pair: enough registration tiles and a small
    residual. A false NCC match or a different crop fails here."""
    rms = m.get("reg_rms", float("nan"))
    return int(m.get("reg_n", 0)) >= REG_MIN_TILES and rms == rms and rms <= REG_MAX_RMS


def choose_targets(lists: Sequence[Set[int]], depth: int) -> Tuple[List[int], List[int]]:
    """Target chapter numbers for one candidate: numbers covered by >= 2 lists
    (the union, not the intersection: one source missing a chapter must not
    empty the set), most-covered first, spread evenly over the covered range.
    The first/last TRIM_ENDS numbers are dropped when >= TRIM_ENDS_MIN are
    covered (a first chapter often has a different cover treatment, the newest
    may be partial). A single list uses its own numbers. Returns (targets,
    spares), spares ordered by coverage then spread."""
    if not lists:
        return [], []
    count: Dict[int, int] = {}
    for ls in lists:
        for n in ls:
            count[n] = count.get(n, 0) + 1
    floor = 2 if len(lists) >= 2 else 1
    covered = sorted(n for n, k in count.items() if k >= floor)
    if not covered:
        covered = sorted(count)
    if len(covered) >= TRIM_ENDS_MIN:
        covered = covered[TRIM_ENDS:-TRIM_ENDS]
    best = max(count[n] for n in covered)
    targets: List[int] = []
    level = best
    while len(targets) < depth and level >= 1:
        pool = [n for n in covered if count[n] == level and n not in targets]
        targets.extend(_spread(pool, depth - len(targets)))
        level -= 1
    targets.sort()
    rest = [n for n in covered if n not in targets]
    spares: List[int] = []
    for level in sorted({count[n] for n in rest}, reverse=True):
        pool = [n for n in rest if count[n] == level]
        spares.extend(_spread(pool, len(pool)))
    return targets, spares


def _reference_labels(listed: Sequence[_Source], slots: Sequence[int]) -> Dict[int, int]:
    """slot -> the episode label that slot stands for: the label at that number on
    the listed source with the longest list that names one (which source is the
    reference does not matter, only that every source then fetches the SAME
    content; site numbering offsets drift 0-2 along Tower of God). Slots no source
    labels are absent (the number is used as is)."""
    out: Dict[int, int] = {}
    order = sorted(listed, key=lambda s: (-len(s.versions), s.key))
    for slot in slots:
        for s in order:
            ch = s.versions.get(slot)
            ep = sm.episode_number(ch.get("title")) if ch else None
            if ep is not None:
                out[slot] = ep
                break
    return out


def _spread(pool: Sequence[int], k: int) -> List[int]:
    """k items of a sorted pool at bucket centres, then the rest in the order
    that keeps filling the largest gaps (deterministic)."""
    if k <= 0 or not pool:
        return []
    if k >= len(pool):
        idx = list(range(len(pool)))
        mid = (len(pool) - 1) / 2
        idx.sort(key=lambda i: (abs(i - mid), i))
        return [pool[i] for i in idx]
    step = len(pool) / k
    out = []
    for i in range(k):
        j = min(int(step * (i + 0.5)), len(pool) - 1)
        if pool[j] not in out:
            out.append(pool[j])
    return out


def _cluster_source_pairs(cl: _Cluster):
    """(source a, source b) -> score parts in a's favour, for every pair this
    cluster can vouch for: identical copies tie exactly (all parts 0), measured
    class edges apply to their representatives. Members of failed classes are
    left out entirely (their match did not register)."""
    zero = {"sharp": 0.0, "res": 0.0, "blk": 0.0, "_same": 1.0}
    for cid, keys in cl.classes.items():
        if cid in cl.failed:
            continue
        for k in keys[1:]:
            yield (keys[0], k), dict(zero)
    for (a, b), parts in cl.edges.items():
        if a in cl.failed or b in cl.failed:
            continue
        yield (cl.classes[a][0], cl.classes[b][0]), dict(parts)


def median_edge(a: str, b: str, lst: Sequence[Dict[str, float]], at: float) -> PairEdge:
    """Median over pages; the parts are those of the median page (the mean of
    the two middle pages for an even count), so they add up to the score."""
    tot = sorted(lst, key=lambda p: p["sharp"] + p["res"] + p["blk"])
    n = len(tot)
    mids = [tot[n // 2]] if n % 2 else [tot[n // 2 - 1], tot[n // 2]]
    parts = {k: sum(m[k] for m in mids) / len(mids) for k in ("sharp", "res", "blk")}
    same = all("_same" in p for p in lst)
    ps = sorted(p["_psnr"] for p in lst if "_psnr" in p)
    psnr = None
    if ps:
        m = len(ps)
        psnr = ps[m // 2] if m % 2 else (ps[m // 2 - 1] + ps[m // 2]) / 2
    return PairEdge(a, b, sum(parts.values()), n, parts, at, same, psnr)


def rate_sources(
    verdicts: Dict[str, SourceVerdict],
    edges: Sequence[PairEdge],
) -> Dict[str, Rating]:
    """Ratings for the sources present in one candidate, from their verdicts and
    the pair edges among them (from this run or the cache).

    Basis:
      matched     served, and in the largest connected group of compared sources
      partial     served and compared, but in a smaller group (no edge path to
                  the main group, so its rating is relative to its own group)
      solo        served, and the only source that served pages
      unmatched   served, but no page matched any present peer
      unreachable / pending   from the verdict
    same_file / same_scan (matched/partial only): the other present sources of
    the same edition serving identical copies, or the same scan re-encoded
    (edge psnr >= SAME_SCAN_DB, transitively). Both are pair facts,
    independent of rating (the fit still treats identical copies as one node
    across editions).
    Rating (matched/partial): relative to the best original-edition source of
    its group; tie groups (within page_ranker.TIE of the group's head, greedy
    from the best) mark copies whose order is noise."""
    present = set(verdicts)
    use = [e for e in edges if e.a in present and e.b in present and e.a != e.b
           and verdicts[e.a].status == "served" and verdicts[e.b].status == "served"]
    # Identical copies are ONE node in the fit: as separate nodes the ridge
    # pulls the less-connected one toward the group mean, so a byte-identical
    # mirror would rate below its original.
    node = {k: k for e in use for k in (e.a, e.b)}

    def root(x):
        while node[x] != x:
            node[x] = node[node[x]]
            x = node[x]
        return x
    for e in use:
        if e.same:
            ra, rb = root(e.a), root(e.b)
            if ra != rb:
                node[max(ra, rb)] = min(ra, rb)
    fit_edges = [(root(e.a), root(e.b), e) for e in use if root(e.a) != root(e.b)]
    # same-scan families: connected components over same-file OR same-scan edges
    # between sources of one edition. PSNR is measured on gray tone-matched
    # pixels, so a copy colored over the same line art reads as the same scan
    # (a tinted stub: 69 dB); the edition check is what separates those.
    fam = {k: k for k in node}

    def froot(x):
        while fam[x] != x:
            fam[x] = fam[fam[x]]
            x = fam[x]
        return x
    for e in use:
        if verdicts[e.a].edition != verdicts[e.b].edition:
            continue
        if e.same or (e.psnr is not None and e.psnr >= SAME_SCAN_DB):
            ra, rb = froot(e.a), froot(e.b)
            if ra != rb:
                fam[max(ra, rb)] = min(ra, rb)
    q_node = pr.fit_latent([(a, b, e.score, float(e.n)) for a, b, e in fit_edges])
    comps_node = {part: pr.fit_latent([(a, b, e.parts.get(part, 0.0), float(e.n)) for a, b, e in fit_edges])
                  for part in ("sharp", "res", "blk")}
    zero = {root(k): 0.0 for k in node}   # a group of identical copies with no other peer
    q = {k: q_node.get(root(k), zero[root(k)]) for k in node}
    comps = {part: {k: c.get(root(k), 0.0) for k in node} for part, c in comps_node.items()}
    # connected groups
    parent = {k: k for k in q}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    peers: Dict[str, Set[str]] = {}
    for e in use:
        parent[find(e.a)] = find(e.b)
        peers.setdefault(e.a, set()).add(e.b)
        peers.setdefault(e.b, set()).add(e.a)
    groups: Dict[str, List[str]] = {}
    for k in q:
        groups.setdefault(find(k), []).append(k)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), sorted(g)))
    served = [k for k, v in verdicts.items() if v.status == "served"]
    out: Dict[str, Rating] = {}
    for i, g in enumerate(ordered):
        orig = [k for k in g if verdicts[k].edition != "alternate"] or g
        q_best = max(q[k] for k in orig)
        tie = _tie_groups({k: q[k] for k in orig})
        for k in g:
            out[k] = Rating(
                basis="matched" if i == 0 else "partial",
                rating=pr.rating_from_deficit(q_best - q[k]),
                q=q[k],
                components={p: comps[p].get(k, 0.0) for p in comps},
                tie_group=tie.get(k),
                group=i,
                edition=verdicts[k].edition,
                peers=len(peers.get(k, ())),
                # same edition only, like the families: a site mixing colored and
                # B&W chapters (mangakatana, Chainsaw Man) can serve identical
                # pages on the compared chapters yet be voted alternate overall
                same_file=sorted(x for x in node if x != k and root(x) == root(k)
                                 and verdicts[x].edition == verdicts[k].edition),
                same_scan=sorted(x for x in node if froot(x) == froot(k) and root(x) != root(k)),
            )
    for k, v in verdicts.items():
        if k in out:
            continue
        if v.status == "served":
            basis = "solo" if len(served) == 1 else "unmatched"
        else:
            basis = v.status
        out[k] = Rating(basis=basis, rating=0.0 if basis == "unreachable" else None, edition=v.edition)
    return out


def _tie_groups(q: Dict[str, float]) -> Dict[str, int]:
    """Greedy from the best: a source within TIE of the current group's head
    joins it; the first one further away heads the next group."""
    out: Dict[str, int] = {}
    head = None
    g = -1
    for k in sorted(q, key=lambda k: (-q[k], k)):
        if head is None or q[head] - q[k] >= pr.TIE:
            head = k
            g += 1
        out[k] = g
    return out


# ---------------------------------------------------------------- cache records

def verdict_ttl(v: SourceVerdict) -> float:
    """Cache lifetime of a verdict. A failed chapter-list call sinks the source
    for this search but is cached only as long as `pending`: it is as often
    this machine (a held browser profile, a reset connection) as the site.
    Pages that failed on two chapters (`pages_failed`) are the dead-CDN signal
    and keep the full day; `no_readable` is a fact about the list."""
    if v.status == "unreachable":
        if v.reason == "list_failed":
            return TTL_PENDING_S
        return TTL_UNREACHABLE_S
    if v.status == "pending":
        return TTL_PENDING_S
    if v.reason.startswith("strip_"):
        return TTL_TRUNCATED_S
    return TTL_TRUNCATED_S if v.truncated else TTL_SERVED_S


def verdict_to_json(v: SourceVerdict) -> Dict[str, Any]:
    return {
        "status": v.status, "reason": v.reason, "edition": v.edition, "depth": v.depth,
        "coverage": v.coverage, "attempted": v.attempted, "succeeded": v.succeeded,
        "timed_out": v.timed_out, "chroma": round(v.chroma, 4),
        "unmatched_against": list(v.unmatched_against), "truncated": v.truncated,
        "chapters": list(v.chapters),
    }


def verdict_from_json(key: str, d: Dict[str, Any]) -> SourceVerdict:
    return SourceVerdict(
        key=key, status=str(d.get("status") or "pending"), reason=str(d.get("reason") or ""),
        edition=str(d.get("edition") or "undecided"), depth=int(d.get("depth") or 0),
        coverage=int(d.get("coverage") or 0), attempted=int(d.get("attempted") or 0),
        succeeded=int(d.get("succeeded") or 0), timed_out=int(d.get("timed_out") or 0),
        chroma=float(d.get("chroma") or 0.0),
        unmatched_against=[str(x) for x in (d.get("unmatched_against") or [])],
        truncated=bool(d.get("truncated")), chapters=[int(x) for x in (d.get("chapters") or [])],
    )


def edge_to_json(e: PairEdge) -> Dict[str, Any]:
    """Stored on BOTH sources' entries, oriented from the owner (score in the
    owner's favour), keyed by the peer's cache key."""
    return {"score": e.score, "n": e.n, "parts": dict(e.parts), "at": e.at, "same": e.same,
            "psnr": None if e.psnr is None else round(e.psnr, 2)}


def edge_from_json(owner: str, peer: str, d: Dict[str, Any]) -> PairEdge:
    parts = {k: float(v) for k, v in (d.get("parts") or {}).items()}
    return PairEdge(owner, peer, float(d.get("score") or 0.0), int(d.get("n") or 0), parts,
                    float(d.get("at") or 0.0), bool(d.get("same")),
                    None if d.get("psnr") is None else float(d["psnr"]))


def quality_metadata(v: SourceVerdict, r: Rating) -> Dict[str, Any]:
    """The `img_quality_metadata` dict a source carries to the UI tooltip and the
    JSON output. samples_* keep their old meaning (page fetches this probe
    tried / served / timed out); cdn_reliability = served share of the
    non-timeout fetches. same_file / same_scan = site names of the present
    sources serving identical copies / the same scan re-encoded (Rating)."""
    tried = v.attempted - v.timed_out
    return {
        "basis": r.basis,
        "samples_attempted": v.attempted,
        "samples_succeeded": v.succeeded,
        "samples_timed_out": v.timed_out,
        "cdn_reliability": round(v.succeeded / tried, 3) if tried > 0 else None,
        "edition": r.edition,
        # which way an alternate edition differs (the UI's "Other edition" tooltip)
        "colored": v.chroma >= pr.COLOR_T,
        "tie_group": r.tie_group,
        "match_group": r.group,
        "peers": r.peers,
        "q": round(r.q, 4) if r.q is not None else None,
        "components": {k: round(x, 4) for k, x in r.components.items()},
        "chapters": list(v.chapters),
        "reason": v.reason or None,
        "truncated": v.truncated,
        "same_file": _sites_of(r.same_file),
        "same_scan": _sites_of(r.same_scan),
    }


def _sites_of(keys: Sequence[str]) -> List[str]:
    """Site names of source keys, deduped in order. Keys are "site:url"
    (search_orchestrator._src_key, ImageQualityCache._key); the UI shows sites."""
    out: List[str] = []
    for k in keys:
        site = k.split(":", 1)[0]
        if site not in out:
            out.append(site)
    return out


__all__ = [
    "MatchSession", "SourceVerdict", "PairEdge", "Rating", "rate_sources",
    "choose_targets", "registered", "median_edge", "verdict_ttl", "no_page_evidence",
    "verdict_to_json", "verdict_from_json", "edge_to_json", "edge_from_json",
    "quality_metadata",
]
