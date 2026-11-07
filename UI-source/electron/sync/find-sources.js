// ============================================================
// DEVICE SYNC — FIND SOURCES (main process, runner only)
//
// Owns batch find-sources for a target's device-only folders (parent
// "Batch find-sources"): one search per included row, sequential, on its OWN
// Searcher (the Search tab's searcher is never shared), keeping the top
// TOP_CANDIDATES series matches per row and EVERY site of each match (sites
// past the 5th used to be cut, so mangafire/mangakatana/... never reached
// the sheet; plan sync-temp/plans/find-sources-all-sites.md). Rows persist per target in
// userData/sync/targets/<id>/find-sources.json, written after every row, so
// a run is resumable: start() runs every included row that isn't `done`.
// ROWS FOLLOW THE TARGET'S DEVICE-ONLY FOLDERS (UI pass U4): get() and
// start() take the full current list and reconcile against it (a new folder
// gets a `queued` row, a folder no longer device-only loses its row), so
// every folder the Find sources sheet shows has a row it can edit (query,
// pin, skip) before its first search. start()'s `only` narrows what RUNS,
// never what is kept.
// Editing a row's query (update-row) sends it back to `queued`, which is the
// "retry with an edited query" path for rows that errored or found nothing.
//
// NOT HERE (P4 decision, UI pass): "Queue N downloads". The renderer queues
// each accepted row through the existing download IPC and adds the alias
// URL → device folder with sync:config-op add-alias.
//
// WAITS while Library Check All or a Search-tab search runs (isBlocked):
// Check All holds the shared mangafire/comix browser profile, and two
// searches at once double the site load.
//
// SEARCH OPTIONS mirror the Search tab's (useDownloader.runSearch): the
// saved searchOpts, collapseSplits === true, disabledSites, then the
// Resource Limits network cap on searchParallelism (main.js search:run).
//
// LIFECYCLE: cancel() / cancelAndWait() tree-kill the search (proc-kill.js
// through Searcher); cancelNow() is the synchronous kill for shutdownNow().
//
// Row: {folder, query, include, state, candidates[≤5], pickUrl, multiSource,
//       pinnedUrl, skip, error, searchedAt}
//   state: queued | searching | done | error | cancelled
//   candidate: {title, canonicalTitle, year, best, sources:[source, all sites],
//               chapterMap?}
//     one candidate = one SERIES; its sources = the sites carrying it, in the
//     search's rank order (best = sources[0]). title is for display (falls
//     back to the top site's title); canonicalTitle is the search's own
//     canonical_title, kept apart so the fallback payload matches the Search
//     tab's (src/lib/prefetchedAlts.js).
//   chapterMap: the search's `winner_chapter_map`, VERBATIM, on candidate 0
//     only (aio_search_cli.py builds it for candidates[0] alone, and only
//     with --multi-source). Absent = no coverage to show: multi-source off,
//     a row searched before this field, or a search with no candidates. It
//     is the same object the Search tab's "Chapter coverage" panel reads
//     (SearchChapterMap.jsx), stored whole by user decision (2026-10-17,
//     plan sync-temp/plans/find-sources-coverage.md: no compact summary, so
//     one shape feeds both tabs). It holds one entry per chapter per site,
//     so it is most of a row's bytes; it is read, never edited, here.
//   source: {site, url, title, cover, rating, chapters, qualityBasis,
//            qualityReason, edition, titleMatch}
//     rating = the measured image-quality score, else the site's seed prior;
//     the renderer shows it as a number only when qualityBasis says it was
//     measured (src/lib/qualityDisplay.js measuredRating), otherwise a
//     reason label built from qualityBasis + qualityReason
//     (img_quality_metadata.reason, sites/match_session.py SourceVerdict).
//     NOT composite_score, which in the current search IS the title match
//     (search_orchestrator.py "composite = title_match"). Rows saved before
//     this field (they stored `composite`) show no rating until re-searched.
//   pickUrl: the site the user chose as the download source, by URL (not an
//     index: indices went stale across re-searches). null = the default,
//     candidate 0's top site. The match holding it is the FIRST candidate
//     listing that URL. A re-search keeps it only while some candidate still
//     lists it; a query change clears it.
//   multiSource: the multi-source toggle THIS row's search ran with (null
//     until searched). The renderer's queue follows it the way the Search
//     tab's download follows the search's snapshot (findView.rowQueueArgs).
//
// FILE VERSION 2 (2026-10-17). Version 1 rows carried an index `pick` and
// at most 5 sites per match; _load migrates them (pick → pickUrl, and a
// `done` row goes back to `queued` so the next Search re-runs it with every
// site, keeping its old candidates visible until then).
//
// Read by: service.js. Depends on: ../searcher.js (Searcher, injectable),
// ../resource-limits.js, store.js (WriteQueue, readJson), naming.js
// (deriveSearchQuery).
// ============================================================

const path = require("path");
const { WriteQueue, readJson } = require("./store");
const { deriveSearchQuery } = require("./naming");

const TOP_CANDIDATES = 5;
const BLOCKED_POLL_MS = 2000;
const FILE = "find-sources.json";
const FILE_VERSION = 2;
const ROW_PATCH_KEYS = Object.freeze(["query", "pickUrl", "pinnedUrl", "skip", "include"]);

/**
 * The Search tab's options for one batch row, from the SAVED settings only:
 * the sheet has no per-run override (it links to Settings → Search). Every
 * searchOpts default equals aio-dl.py's argparse default, so an absent key
 * may stay absent, except seededOnly: the renderer defaults it ON
 * (SettingsTab DEFAULT_SEARCH_OPTS, SearchTab DEFAULT_OPTS) while the CLI's
 * default is off, so a user who never saved search settings would otherwise
 * search every site here and only curated ones on the Search tab.
 */
function _searchOptions(settings) {
  const s = settings || {};
  const so = s.searchOpts || {};
  let parallelism;
  try {
    const { searchParallelismForLevel } = require("../resource-limits");
    parallelism = searchParallelismForLevel(s.searchOpts ? s.searchOpts.searchParallelism : undefined, s.networkLimit);
  } catch (_) {
    parallelism = s.searchOpts ? s.searchOpts.searchParallelism : undefined;
  }
  return {
    ...so,
    seededOnly: so.seededOnly !== false,
    collapseSplits: s.collapseSplits === true,
    ...(Array.isArray(s.disabledSites) && s.disabledSites.length ? { disabledSites: s.disabledSites } : {}),
    searchParallelism: parallelism,
  };
}

function _num(v) {
  return Number.isFinite(v) ? v : null;
}

function _source(s) {
  return {
    site: s.site || null,
    url: s.url || null,
    title: s.title || null,
    cover: s.cover || null,
    rating: _num(s.img_quality_score) != null ? s.img_quality_score : _num(s.seed_quality),
    chapters: _num(s.actual_chapter_count) != null ? s.actual_chapter_count : _num(s.chapter_count_hint),
    qualityBasis: s.quality_basis || null,
    qualityReason: (s.img_quality_metadata && s.img_quality_metadata.reason) || null,
    // fallback funnel (src/lib/prefetchedAlts.js) keeps only the primary's edition
    edition: s.edition || null,
    titleMatch: _num(s.title_match),
  };
}

/** The payload's winner_chapter_map when it has the shape the renderer reads, else null. */
function _chapterMap(json) {
  const m = json && json.winner_chapter_map;
  return m && typeof m === "object" && !Array.isArray(m) && Array.isArray(m.chapters) ? m : null;
}

/**
 * --search-json payload → at most TOP_CANDIDATES compact series matches, each
 * with all its sites; candidate 0 also carries the payload's chapter map.
 */
function compactCandidates(json) {
  const list = json && Array.isArray(json.candidates) ? json.candidates : [];
  const map = _chapterMap(json);
  return list.slice(0, TOP_CANDIDATES).map((c, i) => {
    const sources = (Array.isArray(c.sources) ? c.sources : []).map(_source);
    return {
      title: c.canonical_title || (sources[0] && sources[0].title) || null,
      canonicalTitle: c.canonical_title || null,
      year: _num(c.canonical_year),
      best: sources[0] || null,
      sources,
      ...(i === 0 && map ? { chapterMap: map } : {}),
    };
  });
}

/** True when some candidate of `candidates` lists `url` as a site. */
function _listsUrl(candidates, url) {
  return (candidates || []).some((c) => (c.sources || []).some((s) => s && s.url === url));
}

/**
 * A stored row → the current shape. Version-1 rows: index `pick` → pickUrl
 * (pick 0 / none = the default, null), and `done` → `queued` (their site
 * lists were cut at 5 and nothing else re-runs a finished row whose query is
 * unchanged). A run killed mid-search left the row `searching`: back to
 * `queued` in every version.
 */
function _migrateRow(row, version) {
  const out = { ...row };
  if (version < 2) {
    const c = Number.isInteger(row.pick) && row.pick > 0 && Array.isArray(row.candidates) ? row.candidates[row.pick] : null;
    out.pickUrl = (c && c.best && c.best.url) || null;
    if (out.state === "done") out.state = "queued";
  }
  delete out.pick;
  if (!("pickUrl" in out)) out.pickUrl = null;
  if (typeof out.multiSource !== "boolean") out.multiSource = null;
  if (out.state === "searching") out.state = "queued";
  return out;
}

class FindSources {
  /**
   * @param {object} d
   * @param {string} d.targetsDir        userData/sync/targets
   * @param {() => object} d.getSettings
   * @param {() => {pythonCmd, scriptPath, workingDir}} d.spawnPaths
   * @param {() => (string|null)} d.isBlocked  why a search must wait, or null
   * @param {(ev:object) => void} d.emit       `find` events (service adds kind)
   * @param {() => object} d.makeSearcher      a Searcher-like {runSearch, cancel, cancelAndWait, cancelNow}
   * @param {WriteQueue} [d.queue]
   * @param {object} [d.timers]
   * @param {() => number} [d.now]
   */
  constructor(d) {
    this.targetsDir = d.targetsDir;
    this.getSettings = d.getSettings;
    this.spawnPaths = d.spawnPaths;
    this.isBlocked = d.isBlocked || (() => null);
    this.emit = d.emit || (() => {});
    this.makeSearcher = d.makeSearcher;
    this.queue = d.queue || new WriteQueue();
    this.timers = d.timers || { setTimeout, clearTimeout };
    this.now = d.now || Date.now;
    this.rows = new Map(); // targetId → Map(folder → row)
    this.run = null; // {targetId, cancelled, done, total, waiting}
    this.searcher = null;
  }

  _file(targetId) {
    return path.join(this.targetsDir, targetId, FILE);
  }

  async _load(targetId) {
    if (this.rows.has(targetId)) return this.rows.get(targetId);
    const r = await readJson(this._file(targetId));
    const m = new Map();
    const list = r.state === "ok" && r.value && Array.isArray(r.value.rows) ? r.value.rows : [];
    const version = r.state === "ok" && r.value && Number.isInteger(r.value.version) ? r.value.version : 1;
    for (const row of list) {
      if (!row || typeof row.folder !== "string") continue;
      m.set(row.folder, _migrateRow(row, version));
    }
    this.rows.set(targetId, m);
    return m;
  }

  _save(targetId) {
    const m = this.rows.get(targetId);
    return this.queue.write(this._file(targetId), { version: FILE_VERSION, rows: [...(m ? m.values() : [])] });
  }

  _newRow(folder, query) {
    return { folder, query: query || deriveSearchQuery(folder), include: true, state: "queued", candidates: [], pickUrl: null, multiSource: null, pinnedUrl: null, skip: false, error: null, searchedAt: null };
  }

  isRunning() {
    return !!this.run;
  }

  /**
   * Make the target's rows match `folders` (its current device-only folder
   * names): new folders get a queued row, rows of folders that left are
   * dropped. Saves only when something changed.
   */
  async _reconcile(targetId, folders) {
    const m = await this._load(targetId);
    const want = new Set(folders || []);
    let changed = false;
    for (const f of want) {
      if (!m.has(f)) {
        m.set(f, this._newRow(f));
        changed = true;
      }
    }
    for (const f of [...m.keys()]) {
      if (!want.has(f)) {
        m.delete(f);
        changed = true;
      }
    }
    if (changed) await this._save(targetId).catch(() => {});
    return m;
  }

  /**
   * {running, targetId, waiting, done, total, rows} for one target (rows in
   * folder order). With `folders` (the target's device-only folders) the
   * rows are reconciled first; without (no plan yet) they are as stored.
   */
  async get(targetId, folders) {
    const m = Array.isArray(folders) ? await this._reconcile(targetId, folders) : await this._load(targetId);
    const r = this.run && this.run.targetId === targetId ? this.run : null;
    return {
      ok: true,
      running: !!r,
      targetId,
      waiting: r ? r.waiting : null,
      done: r ? r.done : 0,
      total: r ? r.total : 0,
      rows: [...m.values()],
    };
  }

  /** Snapshot for get-state: the running batch, or null. */
  status() {
    const r = this.run;
    return r ? { targetId: r.targetId, waiting: r.waiting, done: r.done, total: r.total } : null;
  }

  /**
   * Reconcile the rows with `folders` (all of the target's device-only
   * folders), then run every included, unskipped row that isn't done,
   * limited to `only` when given. Resolves once the run has started; the
   * service doesn't await the run (find events carry the progress).
   * @param {{targetId:string, folders:string[], only?:string[]}} a
   * @returns {Promise<{ok:true, started:boolean}|{ok:false, code:string, message:string}>}
   */
  async start({ targetId, folders, only }) {
    if (this.run) return { ok: false, code: "busy", message: "a find-sources run is already going" };
    const m = await this._reconcile(targetId, folders);
    const pick = Array.isArray(only) ? new Set(only) : null;
    const todo = [...m.values()].filter((r) => (!pick || pick.has(r.folder)) && r.include && !r.skip && r.state !== "done");
    if (!todo.length) {
      this.emit({ targetId, state: "done", done: 0, total: 0 });
      return { ok: true, started: false };
    }
    const run = { targetId, cancelled: false, done: 0, total: todo.length, waiting: null };
    this.run = run;
    this.searcher = this.makeSearcher();
    this.emit({ targetId, state: "running", done: 0, total: run.total });
    this._loop(run, todo).catch(() => {});
    return { ok: true, started: true };
  }

  async _loop(run, todo) {
    try {
      for (const row of todo) {
        if (run.cancelled) break;
        await this._waitUnblocked(run);
        if (run.cancelled) break;
        row.state = "searching";
        row.error = null;
        this.emit({ targetId: run.targetId, state: "running", done: run.done, total: run.total, row: { ...row } });
        try {
          const settings = this.getSettings() || {};
          const opts = _searchOptions(settings);
          const json = await this.searcher.runSearch({ ...this.spawnPaths(settings), query: row.query, opts });
          row.candidates = compactCandidates(json);
          row.multiSource = opts.multiSource === true;
          row.state = "done";
          if (row.pickUrl && !_listsUrl(row.candidates, row.pickUrl)) row.pickUrl = null;
        } catch (e) {
          if (e && e.cancelled) {
            row.state = "queued";
            run.cancelled = true;
          } else {
            row.state = "error";
            row.error = String((e && e.message) || e).slice(0, 500);
          }
        }
        row.searchedAt = row.state === "queued" ? row.searchedAt : this.now();
        if (row.state !== "queued") run.done += 1;
        await this._save(run.targetId).catch(() => {});
        this.emit({ targetId: run.targetId, state: "running", done: run.done, total: run.total, row: { ...row } });
      }
    } finally {
      this.run = null;
      this.searcher = null;
      this.emit({ targetId: run.targetId, state: run.cancelled ? "cancelled" : "done", done: run.done, total: run.total });
    }
  }

  async _waitUnblocked(run) {
    for (;;) {
      let why = null;
      try {
        why = this.isBlocked();
      } catch (_) {
        why = null;
      }
      if (!why || run.cancelled) {
        if (run.waiting) {
          run.waiting = null;
          this.emit({ targetId: run.targetId, state: "running", done: run.done, total: run.total, waiting: null });
        }
        return;
      }
      if (run.waiting !== why) {
        run.waiting = why;
        this.emit({ targetId: run.targetId, state: "waiting", done: run.done, total: run.total, waiting: why });
      }
      await new Promise((r) => {
        run.wake = r;
        this.timers.setTimeout(r, BLOCKED_POLL_MS);
      });
    }
  }

  /** Patch a row's editable fields. A changed query re-queues the row. */
  async updateRow({ targetId, folder, patch }) {
    const m = await this._load(targetId);
    const row = m.get(folder);
    if (!row) return { ok: false, code: "invalid", message: "unknown row" };
    if (this.run && this.run.targetId === targetId && row.state === "searching") {
      return { ok: false, code: "busy", message: "that row is being searched" };
    }
    const p = patch || {};
    for (const k of Object.keys(p)) if (!ROW_PATCH_KEYS.includes(k)) return { ok: false, code: "invalid", message: `can't edit ${k}` };
    if ("query" in p) {
      const q = String(p.query == null ? "" : p.query).trim();
      if (!q) return { ok: false, code: "invalid", message: "empty query" };
      if (q !== row.query) {
        row.query = q;
        row.state = "queued";
        row.candidates = [];
        row.pickUrl = null;
        row.multiSource = null;
        row.error = null;
      }
    }
    if ("pickUrl" in p) {
      if (p.pickUrl != null && !(typeof p.pickUrl === "string" && _listsUrl(row.candidates, p.pickUrl))) {
        return { ok: false, code: "invalid", message: "that site isn't one of this row's results" };
      }
      row.pickUrl = p.pickUrl == null ? null : p.pickUrl;
    }
    if ("pinnedUrl" in p) {
      if (p.pinnedUrl != null && !/^https?:\/\/\S+$/i.test(String(p.pinnedUrl))) return { ok: false, code: "invalid", message: "not an http(s) URL" };
      row.pinnedUrl = p.pinnedUrl == null ? null : String(p.pinnedUrl);
    }
    if ("skip" in p) row.skip = !!p.skip;
    if ("include" in p) row.include = !!p.include;
    await this._save(targetId);
    this.emit({ targetId, state: this.run && this.run.targetId === targetId ? "running" : "idle", row: { ...row } });
    return { ok: true, row: { ...row } };
  }

  /** Stop the batch. Returns {wasRunning}; the search is tree-killed. */
  cancel() {
    const run = this.run;
    if (!run) return { wasRunning: false };
    run.cancelled = true;
    if (run.wake) run.wake();
    if (this.searcher) this.searcher.cancel();
    return { wasRunning: true };
  }

  /** cancel(), then wait for the search process tree to close (≤ ~6 s). */
  async cancelAndWait() {
    const run = this.run;
    if (!run) return { wasRunning: false };
    run.cancelled = true;
    if (run.wake) run.wake();
    if (this.searcher && this.searcher.cancelAndWait) await this.searcher.cancelAndWait();
    return { wasRunning: true };
  }

  /** Synchronous kill for shutdownNow(). */
  cancelNow() {
    const run = this.run;
    if (!run) return false;
    run.cancelled = true;
    try {
      return this.searcher && this.searcher.cancelNow ? this.searcher.cancelNow() : false;
    } catch (_) {
      return false;
    }
  }

  /** Drop a target's rows (forget-record / remove-target). */
  async forget(targetId) {
    this.rows.delete(targetId);
  }
}

module.exports = { FindSources, compactCandidates, TOP_CANDIDATES, FILE_VERSION, BLOCKED_POLL_MS, FIND_SOURCES_FILE: FILE };
