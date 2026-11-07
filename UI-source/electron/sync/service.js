// ============================================================
// DEVICE SYNC — SERVICE (main process entry point)
//
// Owns the feature's runtime: initDeviceSync(deps) registers every sync:*
// handler and returns the facade main.js calls from its hooks. It wires the
// engine (prepare.js, executor.js, store.js, hash-pool.js, transports.js),
// the monitor (monitor.js), find-sources and the job lane (job-record.js).
//
// FAILURE ISOLATION (review #2; second review #1, #18):
//   * initDeviceSync validates `deps` and THROWS LOUDLY on a missing or
//     mistyped dependency (main.js catches and logs it; sync stays off).
//   * After that nothing escapes: every facade method and every handler
//     catches, logs and resolves. A handler fault answers
//     refuse('invalid', 'internal error: …', {internal:true}).
//
// OFF MEANS INERT. While syncEnabled is false: no socket (port 5037 is
// never touched, get-state included), no read or write under userData/sync,
// no timers, and every channel answers `disabled` except get-state,
// label-preview and both cancels (contract.js CHANNELS_WHILE_DISABLED).
// Turning sync off mid-job cancels the job and the find-sources run first,
// then stops the monitor (applySettings).
//
// JOBS. One lane (job-record.js): plan, verify, apply, prune, rename,
// refresh. A
// job-starting handler answers {ok, runId} at once, or a refusal it can
// decide without device I/O (busy, disconnected, library-missing, blocked
// against the reviewed plan, invalid). Refusals that need the device
// (record-mismatch, device-listing-suspect, needs-mode, a `blocked` found
// on the fresh re-plan) end the job `failed` with summary.refusal set, and
// get-state's plans[targetId].refusal keeps it (P4 call: a plan can take
// minutes, so its device refusals can't be the invoke's answer). Earlier
// than that, every job that prepares a plan sends ONE immediate progress
// line once the device checks are done: progress.step 'device-ready' with
// progress.preflight {kind, caps, canonicalRoot, caseInsensitive,
// caseMeasured, canHash, mayNeedMode, refusal|null} (deviation 14; grep
// preflightSent, prepare.js deviceReady).
//
// CONNECT FLOW (rule 10): a device or folder appearing → one automatic
// plan per target, ALWAYS (UI-pass U3-1), queued FIFO behind any running job
// (job-record enqueueAuto). With prompt-on-connect on and no honored mark,
// PromptMachine also opens a card: planning (the renderer's Preparing card,
// from `pending`) → promptCard over the plans that landed plus the targets
// refused or failed (`attention`) → shown now if focused, else on the next
// focus → withdrawn by main once nothing is left to do (U3-6). The prompt
// offers Review or Not now, never a one-click apply (approved design D2):
// every apply goes through Review's Start. The renderer draws the card's
// numbers from the live plans, not the frozen card (U3-9).
//
// LIBRARY CHANGES (notifyLibraryChanged, debounced) re-plan in full only
// the targets whose plan read a different library than a fresh walk finds
// (grep _afterLibraryChange): a scan that changed nothing (the Sync tab's
// cover scan on open, a Library refresh) leaves the plan and any decision
// in flight alone.
//
// DECISIONS WITHOUT FULL RE-PLANS (plan sync-temp/plans/
// device-sync-cheap-replans.md). A review decision (every config op but an
// identity edit, forget-record and add-target; a Link; a rebind; the
// planning settings but hash mode) re-derives the plan from the last
// prepare's cached facts (prepare.js derivePlan, no I/O; grep _derive). A
// fact the decision newly needs (a stat, a hash, a verify of a newly bound
// folder) is topped up by an automatic `refresh` job (prepare.js
// refreshPlan; grep _startRefresh), never a full prepare. While a job of the
// target runs, the derive waits for its end (rt.rederive, _afterJob); while a
// full plan is owed (rt.stale), nothing is derived: that plan reads the new
// config itself. Apply is unaffected: it always prepares afresh and
// reconciles (_startApply).
//
// LINK: sync:link {targetId, suggestionIds} QUEUES a bind (rt.links) and
// answers {ok, queued:true}; refused `busy` only while an apply / prune /
// rename of that target runs. Binds are record writes, so they run only when
// no job of the target runs: in _derive, or at the start of every job before
// its first store.load (_runJob → _bindLinks). The folder is then verified by
// the refresh the derive asks for.
//
// PRE-WARM (P4 decision): hashes what the targets mirror PREWARM_DELAY_MS
// after launch (or at once when sync is switched on), and pauses while a
// download, Check All, a Search-tab search or a sync job runs.
// Throttle (deviation 3): one hash worker while a download or Check All runs.
//
// SERVICE LOG (deviation 15; diag-log.js): userData/sync/logs/service.log
// gets a `start`, `prewarm` (done / error), `stop` or `quit-now` line, a
// `job-start` / `job-step` / `job-end` line per job (grep _jobLog), and a
// copy of every [deviceSync] stderr line, while sync is on and
// syncDiagnosticLog is true. The installed app has no console; this is
// where its sync faults can be read, and P5's packaged smoke reads it as
// its marker. grep: slog
//
// SELECTION. The op choices live in memory per target and are written to
// selection.json on every change. A target never applied to gets its
// identity-only record header written first, so the selection has an epoch
// to belong to (deviation 14; grep _persistSelection).
//
// UI-PASS CONTRACT (U0, plan sync-temp/plans/device-sync-ui-pass.md):
//   * PUSHED, NOT RE-READ. Every change to a target's plan state calls
//     _planChanged(id) → one coalesced `plans` event per target per tick
//     {targetId, summary, refusal, stale, updating, links, lastRun, gen,
//     selSeq, lastPlan, removed?} (field meanings: contract.js EVENT_KINDS). `adb`
//     and `settings` events cover the rest. get-state is the first read only.
//   * GEN IS THE REVIEW CONTRACT. Every adopted plan gets a new gen
//     (summary.gen). apply {gen} must name the gen the user looked at; a
//     mismatch refuses `blocked` with
//     blocking [{kind:'plan-changed'}]. Without it, an automatic re-plan that
//     landed while the review was open ran its own pre-selections unseen.
//   * PLANNING OPTIONS ARE TARGET FIELDS OR ONE-SHOT (approved design D4/D5,
//     sync-temp/design/approved/spec.md). The removal policy is the target's
//     own `deletePolicy` field, and its stored `verifyMode` (the needs-mode
//     answer) is its baseline: both reach EVERY prepare of it, apply's
//     included (grep _planOpts). Re-check's Re-hash / Read back are one-shot
//     inputs to that one plan or verify job; there is no runtime override
//     state a later automatic re-plan could silently reuse or lose.
//   * LAST RUN: each verify/apply/prune/rename leaves rt.lastRun, so a result
//     outlives the re-plan its own job queues.
//   * DOWNLOAD GUARD: the executor skips a series folder a running download
//     writes (grep _busyCheck).
//
// SYNC TAB CONTRACT (U2, same plan, section "Phase U2 design"):
//   * AUTO-PLAN IS DECIDED HERE: plan {ifNeeded:true} plans only when the
//     target needs it (grep _planIfNeeded); a cancelled or failed plan sets
//     rt.autoHold until _replanSoon / a connect / a manual plan / an identity
//     edit (_identityClear) clears it, so a renderer that asks on every tab
//     open can't loop.
//   * ACKS ARE WHAT WAS SHOWN: apply's ackMassDelete / ackMassRepush are the
//     counts the review displayed (grep _ackArgs); set-selection takes the
//     plan `gen` the click was made on and echoes its `seq` as the plan
//     view's selSeq.
//   * SETTLING RE-PLANS ITSELF at settleAt (grep _armSettle).
//
// HANDOFF OBLIGATIONS FOR THE UI PASS (non-UI plan, Integration edits):
//   * render payload.sync in ConfirmQuitDialog (a sync-only close would say
//     "0 downloads are still running", ConfirmQuitDialog.jsx);
//   * mount useDeviceSync in App.jsx;
//   * choose which presets to expose;
//   * draw the name choice for a re-pushed gone folder (rebind,
//     removedOnDevice; decision 10);
//   * add the sync keys to get-settings together with DEFAULT_SETTINGS and
//     the settings-twin test;
//   * "Queue N downloads" for find-sources rows: the existing download IPC,
//     plus config-op add-alias (URL → device folder) per accepted row;
//   * the dev and installed builds can no longer run side by side
//     (single-instance lock): memory electron-app-local-e2e-testing.
//
// Read by: main.js (grep deviceSync). Depends on: everything in sync/,
// ../searcher.js (find-sources' own Searcher), ../resource-limits.js.
// ============================================================

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const contract = require("./contract");
const { CHANNELS, CHANNEL_NAMES, CHANNELS_WHILE_DISABLED, EVENT_CHANNEL, VERIFY_MODES, QUIT_ASK_PHASES, refuse } = contract;
const { createJobRecord } = require("./job-record");
const { resolveSyncSettings, SYNC_SETTING_SPECS } = require("./sync-settings");
const profiles = require("./profiles");
const planner = require("./planner");
const { loadRecordView } = require("./provenance");
const { labelPreview } = require("./chapter-labels");
const { segmentProblem, AdbTransport, FolderTransport, shq } = require("./transports");
const { simulateRun } = require("./space");
const { WriteQueue, RecordStore, readJson, sweepTmp, newShardId } = require("./store");
const { HashPool, workerCountFor } = require("./hash-pool");
const { walkLibrary, pcSignature } = require("./pc-inventory");
const { preparePlan, derivePlan, refreshPlan, loadView, verifyKeyOf } = require("./prepare");
const { Executor, RunStop, policyFor, RESERVE_BYTES } = require("./executor");
const { normalizeSeriesUrl } = require("../library");
const mon = require("./monitor");
const { FindSources } = require("./find-sources");
const { AdbClient } = require("./adb/client");
const locate = require("./adb/locate");
const { ServiceLog, insideAsar } = require("./diag-log");

const PREWARM_DELAY_MS = 30 * 1000;
const REPLAN_DEBOUNCE_MS = 10 * 1000;
// _armSettle fires this long after a plan's settleAt, so the file that set
// it is past the settle age when the re-plan's listing reads it.
const SETTLE_REPLAN_SLACK_MS = 1000;
const SETTLE_TIMER_MAX_MS = 10 * 60 * 1000;
// R1: list-devices (the Add-target picker, the connect card's re-check)
// keeps the device tracker running this long with no adb target, so the
// FIRST adb target can be picked from a live list; the picker renews it.
const DISCOVER_MS = 2 * 60 * 1000;
const BUSY_POLL_MS = 5000;
const SHUTDOWN_MS = 5000;
const COVER_MAX_BYTES = 8 * 1024 * 1024;
const CONFIG_FILE = "sync-targets.json";
const HASH_CACHE_FILE = "pc-hash-cache.json";
const BROWSE_PRESETS = Object.freeze([
  { label: "Komikku local source", path: "/sdcard/Komikku/local" },
  { label: "Mihon local source", path: "/sdcard/Mihon/local" },
  { label: "Downloads", path: "/sdcard/Download" },
  { label: "Internal storage", path: "/sdcard" },
]);
// Stored per-target fields add-target / update-target accept (profiles.js
// resolveTargetConfig reads them; null = profile default).
const TARGET_FIELDS = Object.freeze([
  "name",
  "kind",
  "serial",
  "root",
  "profile",
  "formats",
  "sidecars",
  "nomedia",
  "namingPolicy",
  "renameToMatch",
  "cleanupNames",
  "selection",
  "only",
  "verifyMode", // the needs-mode answer (profiles.js STORED_VERIFY_MODES)
  "deletePolicy", // Removals (profiles.js DELETE_POLICIES; add-target stamps the global)
]);
// Settings whose change alters what a plan offers: applySettings re-derives
// every planned target when one of them changes (syncHashMode: a full plan).
const PLANNING_SETTINGS = Object.freeze(["syncDeletePolicy", "syncHashMode", "syncChapterPatterns", "syncSizeWarnPct"]);
const DEPS = Object.freeze({
  ipcMain: "object",
  send: "function",
  userDataDir: "string",
  getSettings: "function",
  getLibraryRoot: "function",
  getRunningDownloads: "function",
  isCheckAllRunning: "function",
  isSearchRunning: "function",
  resolveSpawnPaths: "function",
  extraEnv: "object",
  onFocus: "function",
  isFocused: "function",
  getWindow: "function",
  showSaveDialog: "function",
});

/** Throws a TypeError naming every missing or mistyped dependency. */
function validateDeps(deps) {
  const d = deps || {};
  const bad = [];
  for (const [k, type] of Object.entries(DEPS)) {
    const v = d[k];
    if (type === "object" ? !v || typeof v !== "object" : typeof v !== type) bad.push(`${k} (${type})`);
  }
  if (d.ipcMain && typeof d.ipcMain.handle !== "function") bad.push("ipcMain.handle (function)");
  if (bad.length) throw new TypeError(`initDeviceSync: missing or mistyped deps: ${bad.join(", ")}`);
}

function _isObj(v) {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

function _stderr(...a) {
  try {
    console.error("[deviceSync]", ...a);
  } catch (_) {
    // stderr closed during quit
  }
}

/**
 * @param {object} deps  see DEPS; `_test` (optional) injects
 *   {clientOpts, timers, now, bootEpoch, makeSearcher, prewarmDelayMs,
 *    replanDebounceMs, busyPollMs, platform, locateAdb, ensureServer,
 *    connectWaitMs, executorDeps}
 * @returns {object} the facade (never throws, never rejects)
 */
function initDeviceSync(deps) {
  validateDeps(deps);
  const T = deps._test || {};
  const now = T.now || Date.now;
  const root = deps.userDataDir;
  const configPath = path.join(root, CONFIG_FILE);
  const initAt = now();

  const emit = (kind, payload) => {
    try {
      deps.send(EVENT_CHANNEL, { kind, ...(payload || {}) });
    } catch (e) {
      _log("send failed", e && e.message);
    }
  };
  const jobs = createJobRecord({ send: (ch, p) => {
    try {
      deps.send(ch, p);
    } catch (_) {
      // a destroyed window
    }
  }, now });

  // PLANS PUSH. Callers mark a target dirty; one `plans` event per target
  // goes out on the next tick with the whole per-target view (_planView),
  // so a burst (adopt + lastRun + stale mark in one job end) is one event
  // and the renderer only ever overwrites. grep: _planChanged
  const planDirty = new Set();
  let planFlush = null;
  function _planChanged(targetId) {
    if (!targetId) return;
    planDirty.add(targetId);
    if (planFlush) return;
    planFlush = setImmediate(() => {
      planFlush = null;
      const ids = [...planDirty];
      planDirty.clear();
      for (const id of ids) emit("plans", { targetId: id, ..._planView(id) });
    });
  }
  // Monotonic for the process (start/stop cycles included), so a gen the
  // renderer kept from before a restart of the service can't match a new plan.
  let genSeq = 0;

  let settings = resolveSyncSettings(_safe(() => deps.getSettings(), {}));
  let rawSettings = _safe(() => deps.getSettings(), {}) || {};
  let enabled = settings.syncEnabled;
  let started = false;
  let startP = null;
  let stopP = null;
  let shut = false;

  // The service log (diag-log.js): open from the start's mkdir to the end of
  // a stop or shutdownNow, so nothing is written while sync is off; the
  // closing line is written before it shuts. syncDiagnosticLog gates every
  // line. _log mirrors each stderr line into it.
  let slogOpen = false;
  const slog = new ServiceLog({ dir: path.join(root, "logs"), now, active: () => slogOpen && settings.syncDiagnosticLog !== false });
  function _log(...a) {
    _stderr(...a);
    slog.error(a);
  }
  // Live only while started:
  let queue = null;
  let config = null;
  let hashPool = null;
  let marks = null;
  let prompts = null;
  let tracker = null;
  let watcher = null;
  let findSources = null;
  let client = null;
  let devices = [];
  let adbInfo = { binary: null, serverVersion: null, error: null, candidates: null, at: null };
  let current = null; // {runId, phase, targetId, ac, promise, transport}
  let busyTimer = null;
  let prewarmTimer = null;
  let replanTimer = null;
  // notifyLibraryChanged reasons since the debounce began (service log only).
  const libReasons = new Set();
  let discoverUntil = 0;
  let discoverTimer = null;
  let prewarm = { state: "idle", done: 0, total: 0, bytesDone: 0, bytesTotal: 0, at: null };
  let prewarmAc = null;
  let libStatus = null;
  // targetId → {id, store, prep, plan, gen, selOps, selRebinds, selEpoch,
  //   selDeferred, summary, refusal, stale, lastRun}
  const rts = new Map();
  const autoBatches = new Map(); // connKey → {serial, waiting:Set<targetId>, summaries:[]}
  const folderTransports = new Map(); // targetId → FolderTransport (presence only)
  const connected = { adb: new Set(), folder: new Set() }; // connKeys

  function _safe(fn, fallback) {
    try {
      return fn();
    } catch (_) {
      return fallback;
    }
  }

  // ---------------------------------------------------------- config

  function _targets() {
    return (config && config.targets) || [];
  }

  function _target(id) {
    return _targets().find((t) => t.id === id) || null;
  }

  function _cfg(t) {
    return profiles.resolveTargetConfig(t);
  }

  function _rt(id) {
    let r = rts.get(id);
    if (!r) {
      r = {
        id,
        store: new RecordStore({ root, targetId: id, queue, now }),
        prep: null,
        plan: null,
        gen: null,
        selOps: null,
        selRebinds: {},
        selEpoch: null,
        selDeferred: false,
        summary: null,
        refusal: null,
        stale: false,
        lastRun: null,
        // UI-pass U2-3: the last set-selection `seq` applied, echoed in the
        // plan view so the renderer's pending marks clear on the push that
        // carries the change, whatever order reply and event arrive in.
        selSeq: null,
        // U2-1: the last plan job's outcome, and whether `plan {ifNeeded}`
        // must hold off (a failed/cancelled plan, until something changes).
        lastPlan: null,
        autoHold: false,
        settleTimer: null,
        // Cheap re-plans (grep _derive): Links not yet in the plan
        // ({suggestionId, seriesKey, deviceFolder, binding, rebindShardId,
        // dropShardId, bound}); a bind landed that rt.prep.view doesn't show; a derive
        // owed when the running job ends; derives in flight, serialized.
        links: [],
        bindP: null,
        recordChanged: false,
        rederive: false,
        deriving: 0,
        deriveChain: Promise.resolve(),
        // A needs-mode refusal's facts (_stripFacts): choosing a mode tops
        // them up instead of preparing again.
        needsModeFacts: null,
      };
      rts.set(id, r);
    }
    return r;
  }

  /** What `plans` events and get-state's plans[targetId] carry. */
  function _planView(id) {
    const empty = { summary: null, refusal: null, stale: false, updating: false, links: [], lastRun: null, gen: null, selSeq: null, lastPlan: null };
    if (!_target(id)) return { ...empty, removed: true };
    const rt = rts.get(id);
    if (!rt) return empty;
    const updating = _updating(rt);
    return {
      summary: rt.summary,
      refusal: rt.refusal,
      stale: rt.stale || updating,
      updating: updating && !rt.stale,
      links: rt.links.map((l) => ({ suggestionId: l.suggestionId, seriesKey: l.seriesKey, deviceFolder: l.deviceFolder, bound: !!l.bound })),
      lastRun: rt.lastRun,
      gen: rt.summary ? rt.gen : null,
      selSeq: rt.selSeq,
      lastPlan: rt.lastPlan,
    };
  }

  /**
   * A review decision is on its way into the plan: a derive owed or running,
   * a Link not yet planned, a refresh queued or running. rt.stale (a full
   * plan owed) is separate; the plan view's `stale` is either.
   */
  function _updating(rt) {
    if (rt.rederive || rt.deriving > 0 || rt.links.length) return true;
    if (current && current.targetId === rt.id && current.phase === "refresh") return true;
    return jobs.snapshot().queue.some((q) => q.targetId === rt.id && q.kind === "refresh");
  }

  async function _loadConfig() {
    const r = await readJson(configPath);
    const v = r.state === "ok" && _isObj(r.value) ? r.value : {};
    const targets = Array.isArray(v.targets) ? v.targets.filter((t) => _isObj(t) && typeof t.id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(t.id)) : [];
    if (r.state === "corrupt") _log("sync-targets.json unreadable; starting empty:", r.error);
    return { version: Number.isInteger(v.version) ? v.version : 0, adbPath: typeof v.adbPath === "string" ? v.adbPath : null, targets };
  }

  /**
   * Runs one config read-modify-write at a time. Every op reads `config`,
   * awaits (validation, the write, an identity clear), then saves a `next`
   * built from what it read, so two ops in flight together lost the first
   * one's change (found by the R12 test: two chip clicks sent at once kept
   * only the second). The renderer's queue (useDeviceSync configOp) only
   * orders ONE renderer's calls; this covers every caller. Every writer of
   * `config` goes through here: H["config-op"], _clearEvictionMarks.
   */
  let configChain = Promise.resolve();
  function _configTxn(fn) {
    const run = configChain.then(fn);
    configChain = run.catch(() => {});
    return run;
  }

  // The `config` event is NOT sent here: config-op sends it after the op's
  // `after` step (an identity clear), so a renderer reacting to it never
  // sees the new config beside the old record's plan.
  async function _saveConfig(next) {
    const body = { ...next, version: (config.version || 0) + 1 };
    await queue.write(configPath, body);
    config = body;
    _syncWatchers();
    // A new or edited target may mirror files the pre-warm hasn't hashed.
    if (prewarm.state !== "running") _schedulePrewarm(Math.max(0, initAt + (T.prewarmDelayMs != null ? T.prewarmDelayMs : PREWARM_DELAY_MS) - now()));
    return config;
  }

  function _publicConfig() {
    if (!config) return null;
    return { version: config.version, adbPath: config.adbPath, targets: config.targets.map((t) => ({ ...t })) };
  }

  // ---------------------------------------------------------- start / stop

  function _ensureStarted() {
    if (!enabled || shut) return Promise.resolve(false);
    if (started) return Promise.resolve(true);
    if (!startP) {
      startP = _start().then(
        () => {
          startP = null;
          return started;
        },
        (e) => {
          startP = null;
          _log("start failed", e && e.stack);
          return false;
        },
      );
    }
    return startP;
  }

  async function _start() {
    if (stopP) await stopP;
    queue = new WriteQueue();
    await fs.promises.mkdir(root, { recursive: true });
    slogOpen = true;
    await sweepTmp(root).catch(() => 0);
    config = await _loadConfig();
    hashPool = new HashPool({ cachePath: path.join(root, HASH_CACHE_FILE), workers: _workers(), queue });
    await hashPool.load();
    marks = new mon.PromptMarks({ dir: root, now, bootEpoch: T.bootEpoch, queue });
    await marks.load();
    prompts = new mon.PromptMachine({
      marks,
      now,
      isFocused: () => deps.isFocused(),
      onPrompt: (ev) => emit("prompt", { action: ev.action, prompt: ev.prompt, ...(ev.reason ? { reason: ev.reason } : null), prompts: prompts.shown(), pending: prompts.pending() }),
      // U3-3: the planning list behind the renderer's Preparing card.
      onPending: (pending) => emit("prompt", { action: "pending", prompts: prompts.shown(), pending }),
    });
    client = new AdbClient({ ...(T.clientOpts || {}) });
    findSources = new FindSources({
      targetsDir: path.join(root, "targets"),
      getSettings: () => _safe(() => deps.getSettings(), {}),
      spawnPaths: (s) => deps.resolveSpawnPaths(s),
      isBlocked: () => (_safe(() => deps.isCheckAllRunning(), false) ? "check-all" : _safe(() => deps.isSearchRunning(), false) ? "search" : null),
      emit: (ev) => emit("find", ev),
      makeSearcher: T.makeSearcher || (() => new (require("../searcher").Searcher)({ extraEnv: deps.extraEnv, onLog: () => {} })),
      queue,
      now,
    });
    started = true;
    if (!enabled || shut) {
      // Switched off (or quitting) while starting: undo at once.
      await _stop({ reason: shut ? "shutdown" : "disabled" });
      return;
    }
    // P5's marker reads `module` / `inAsar` (the packaged build loads sync/
    // from inside app.asar) and `workers` (the pre-warm's pool size).
    slog.event("start", {
      module: __filename,
      inAsar: insideAsar(__filename),
      electron: process.versions.electron || null,
      node: process.versions.node,
      platform: process.platform,
      pid: process.pid,
      workers: hashPool.limit,
      targets: _targets().length,
    });
    _syncWatchers();
    const busyMs = T.busyPollMs || BUSY_POLL_MS;
    busyTimer = setInterval(() => _busyTick(), busyMs);
    if (busyTimer.unref) busyTimer.unref();
    const delay = T.prewarmDelayMs != null ? T.prewarmDelayMs : PREWARM_DELAY_MS;
    _schedulePrewarm(Math.max(0, initAt + delay - now()));
    emit("config", { config: _publicConfig() });
  }

  function _workers() {
    try {
      const { cpuPercentForLevel } = require("../resource-limits");
      return workerCountFor(cpuPercentForLevel(rawSettings.cpuLimit));
    } catch (_) {
      return 1;
    }
  }

  /**
   * Stop everything; the job and find-sources are cancelled FIRST.
   * `reason` ('disabled' | 'shutdown') goes into the service log's stop line.
   */
  async function _stop({ timeoutMs = SHUTDOWN_MS, reason = "disabled" } = {}) {
    if (!started) return;
    started = false;
    const t0 = now();
    const hadJob = current ? current.phase : null;
    const hadFind = !!(findSources && _safe(() => findSources.isRunning(), false));
    const waits = [];
    if (current) {
      current.ac.abort();
      waits.push(current.promise);
    }
    if (findSources) waits.push(findSources.cancelAndWait());
    const settled = await _within(Promise.allSettled(waits).then(() => true), timeoutMs);
    const hadTracker = !!tracker;
    if (tracker) tracker.stop();
    tracker = null;
    if (hadTracker) _emitAdb();
    if (watcher) watcher.stop();
    watcher = null;
    for (const t of folderTransports.values()) t.destroy();
    folderTransports.clear();
    clearInterval(busyTimer);
    clearTimeout(prewarmTimer);
    clearTimeout(replanTimer);
    clearTimeout(discoverTimer);
    busyTimer = prewarmTimer = replanTimer = discoverTimer = null;
    discoverUntil = 0;
    for (const rt of rts.values()) {
      clearTimeout(rt.settleTimer);
      rt.settleTimer = null;
    }
    if (prewarmAc) prewarmAc.abort();
    if (prompts) for (const p of prompts.shown()) emit("prompt", { action: "withdraw", prompt: p, prompts: [], pending: [] });
    if (prompts && prompts.pending().length) emit("prompt", { action: "pending", prompts: [], pending: [] });
    for (const k of ["adb", "folder"]) connected[k].clear();
    autoBatches.clear();
    for (const q of jobs.snapshot().queue) jobs.dropTarget(q.targetId);
    devices = [];
    // The library may change while sync is off (notifyLibraryChanged is a
    // no-op then), so a status cached before the stop is stale after it.
    libStatus = null;
    if (hashPool) {
      await hashPool.flush().catch(() => {});
      hashPool.close();
    }
    const flushed = queue ? !!(await _within(queue.whenIdle().then(() => true), timeoutMs)) : true;
    hashPool = null;
    emit("device", _devicePayload());
    // The clean-quit evidence P5 reads: the job and the search ended inside
    // the wait (`settled`) and every record write landed (`flushed`).
    slog.event("stop", { reason, ms: now() - t0, hadJob, hadFind, settled: !!settled, flushed });
    slogOpen = false;
  }

  function _within(p, ms) {
    let t;
    return Promise.race([p, new Promise((r) => (t = setTimeout(r, ms)))]).finally(() => clearTimeout(t));
  }

  // ---------------------------------------------------------- monitors

  function _syncWatchers() {
    if (!started) return;
    const adbTargets = _targets().filter((t) => t.kind === "adb");
    // The tracker runs while an adb target exists OR a discovery lease from
    // list-devices is live (R1). Without the lease nothing listed devices
    // until an adb target existed, so the first one could never be added.
    const wantTracker = adbTargets.length > 0 || discoverUntil > now();
    if (wantTracker && !tracker) {
      tracker = new mon.DeviceTracker({
        client,
        timers: T.timers,
        ensureServer: () => _ensureServer(),
        onDevices: (rows, info) => _onDevices(rows, info),
        onState: () => _emitAdb(),
      });
      tracker.start();
      _emitAdb();
    } else if (!wantTracker && tracker) {
      tracker.stop();
      tracker = null;
      _onDevices([], { source: "lost" });
      _emitAdb();
    }
    const folderTargets = _targets().filter((t) => t.kind === "folder");
    for (const [id, tp] of [...folderTransports]) {
      const t = _target(id);
      if (!t || t.kind !== "folder" || t.root !== tp.root) {
        tp.destroy();
        folderTransports.delete(id);
      }
    }
    for (const t of folderTargets) if (!folderTransports.has(t.id)) folderTransports.set(t.id, new FolderTransport({ root: t.root }));
    if (!watcher && folderTargets.length) watcher = new mon.FolderWatcher({ onChange: (id, present) => _onFolder(id, present), timers: T.timers });
    if (watcher) watcher.setTargets(folderTargets.map((t) => ({ targetId: t.id, transport: folderTransports.get(t.id) })));
    // An adb target edited onto another device: its connection is re-derived.
    _onDevices(devices, { source: "config" });
  }

  function _devicePayload() {
    return { devices: devices.map((d) => ({ serial: d.serial, state: d.state, stateKind: d.stateKind, model: d.model, product: d.product, transportId: d.transportId, targetable: d.targetable })), folderPresence: watcher ? watcher.presence() : {} };
  }

  function _onDevices(rows, info) {
    if (!started) return;
    if (info.source === "lost") {
      prompts.lostAdb();
      for (const k of connected.adb) autoBatches.delete(k);
      connected.adb.clear();
      for (const d of devices) _dropQueued((q) => q.serial === d.serial, () => jobs.dropDevice(d.serial));
    }
    devices = rows || [];
    const live = new Map();
    for (const r of devices) {
      if (r.state !== "device" || !r.targetable) continue;
      const tids = _targets().filter((t) => t.kind === "adb" && t.serial === r.serial).map((t) => t.id);
      if (tids.length) live.set(mon.adbConnKey(r), { serial: r.serial, tids });
    }
    for (const k of [...connected.adb]) {
      if (live.has(k)) continue;
      connected.adb.delete(k);
      autoBatches.delete(k);
      prompts.disconnected(k);
      const serial = k.slice(0, k.lastIndexOf("#"));
      _dropQueued((q) => q.serial === serial, () => jobs.dropDevice(serial));
    }
    for (const [k, v] of live) {
      if (connected.adb.has(k)) continue;
      connected.adb.add(k);
      _connected(k, v.serial, v.tids);
    }
    emit("device", _devicePayload());
  }

  function _onFolder(id, present) {
    if (!started) return;
    const k = mon.folderConnKey(id);
    if (present && !connected.folder.has(k)) {
      connected.folder.add(k);
      _connected(k, null, [id]);
    } else if (!present && connected.folder.has(k)) {
      connected.folder.delete(k);
      autoBatches.delete(k);
      prompts.disconnected(k);
      _dropQueued((q) => q.targetId === id, () => jobs.dropTarget(id));
    }
    emit("device", _devicePayload());
  }

  /**
   * A disconnect drops queued jobs. A dropped refresh leaves a decision the
   * plan doesn't show yet: the target is owed a full plan (rt.stale), which
   * the reconnect's automatic plan delivers. Either way its `updating` changed.
   */
  function _dropQueued(pred, drop) {
    const lost = jobs.snapshot().queue.filter(pred);
    drop();
    for (const q of lost) {
      const rt = rts.get(q.targetId);
      if (!rt) continue;
      if (q.kind === "refresh") rt.stale = true;
      _planChanged(q.targetId);
    }
  }

  /**
   * A connection appeared. Every target on it gets an automatic, read-only
   * plan (UI-pass U3-1, user decision: the rail count needs one), also with
   * prompt-on-connect off or a mark honored; the prompt rules only decide
   * whether a card follows. A prompting connection collects its targets'
   * plan outcomes in autoBatches until each has ended (_afterJob) or been
   * dropped (_batchDrop).
   */
  function _connected(connKey, serial, targetIds) {
    for (const id of targetIds) {
      const crt = rts.get(id);
      if (crt) crt.autoHold = false;
    }
    if (settings.syncPromptOnConnect && prompts.connected({ connKey, serial, targetIds })) {
      autoBatches.set(connKey, { serial, waiting: new Set(targetIds), summaries: [], attention: [] });
    }
    for (const id of targetIds) jobs.enqueueAuto({ targetId: id, serial });
    _pump();
  }

  /** Every target of the batch has reported: hand the card to the machine. */
  function _batchSettle(connKey, batch) {
    autoBatches.delete(connKey);
    prompts.planned(connKey, planner.promptCard(batch.serial, batch.summaries, batch.attention));
  }

  /**
   * U3-5: a batch waits for a plan that will never run when its target is
   * removed, edited off the device, or skipped by _pump. Stop waiting for
   * it, so the rest of the connection's card still arrives.
   */
  function _batchDrop(targetId) {
    for (const [k, batch] of [...autoBatches]) {
      if (!batch.waiting.delete(targetId)) continue;
      if (!batch.waiting.size) _batchSettle(k, batch);
    }
  }

  /**
   * The card's entry for a connect plan that didn't land (U3-4): the job's
   * OWN refusal (rt.refusal may already belong to a later plan), or
   * `failed` with its reason. A cancelled plan (the user's Cancel, a
   * disconnect) asks nothing.
   */
  function _attentionOf(targetId, status, summary) {
    const t = _target(targetId);
    if (!t) return null;
    const r = summary && summary.refusal;
    if (r) return { targetId, targetName: t.name || null, code: r.code, message: r.message || null, cause: r.cause || null, folder: r.folder || null, firstSync: r.firstSync == null ? null : !!r.firstSync, folders: r.folders || null, bytes: r.bytes == null ? null : r.bytes };
    if (status === "failed") return { targetId, targetName: t.name || null, code: "failed", message: (summary && (summary.message || summary.reason)) || null, reason: (summary && summary.reason) || null };
    return null;
  }

  /**
   * The target's current plan offers nothing to do and holds no refusal.
   * A target whose LAST plan job didn't complete (failed internally with no
   * refusal, so rt.plan is still the older plan) has something to say: the
   * card lists that failure, so it must not be withdrawn over the old plan.
   */
  function _nothingToDo(targetId) {
    const rt = rts.get(targetId);
    if (!rt || !rt.plan || rt.refusal) return false;
    if (rt.lastPlan && rt.lastPlan.status === "failed") return false;
    return planner.promptSummary(rt.plan, _selection(rt), rt.prep && rt.prep.device).nothingToDo;
  }

  /**
   * U3-6: after a plan lands, an open card whose every target has nothing
   * left to do (the user synced from the tab, a download settled into
   * place) is withdrawn by main; the renderer never answers for the user.
   */
  function _resolvePrompts(targetId) {
    for (const k of prompts.openFor(targetId)) {
      const p = prompts.promptOf(k);
      if (p && p.targetIds.every((id) => !_target(id) || _nothingToDo(id))) prompts.resolve(k, "nothing-to-do");
    }
  }

  /** Is this target's device or folder attached now? */
  function _isConnected(t) {
    if (!t) return false;
    if (t.kind === "adb") return devices.some((d) => d.serial === t.serial && d.state === "device");
    return connected.folder.has(mon.folderConnKey(t.id));
  }

  function _connKeyOf(t) {
    if (t.kind === "folder") return mon.folderConnKey(t.id);
    const d = devices.find((x) => x.serial === t.serial && x.state === "device");
    return d ? mon.adbConnKey(d) : null;
  }

  async function _ensureServer() {
    const bin = await _adbBinary();
    const fn = T.ensureServer || ((c, b) => locate.ensureServer(c, b));
    const r = await fn(client, bin);
    const next = { serverVersion: r.ok ? r.version : null, error: r.ok ? null : String((r.error && r.error.message) || r.error || "no adb server") };
    const changed = next.serverVersion !== adbInfo.serverVersion || next.error !== adbInfo.error;
    adbInfo = { ...adbInfo, ...next };
    if (changed) _emitAdb();
    return r;
  }

  async function _adbBinary({ rescan = false } = {}) {
    if (adbInfo.candidates && !rescan) return adbInfo.binary;
    const fn = T.locateAdb || ((o) => locate.locateAdb(o));
    const r = await fn({ configured: config ? config.adbPath : null });
    adbInfo = { ...adbInfo, binary: r.resolved, candidates: r.candidates, at: now() };
    _emitAdb();
    return r.resolved;
  }

  /** get-state's `adb` and the `adb` event's payload. */
  function _adbView() {
    return {
      binary: adbInfo.binary,
      serverVersion: adbInfo.serverVersion,
      error: adbInfo.error,
      candidates: adbInfo.candidates,
      tracking: tracker ? tracker.mode : "stopped",
      trackingError: tracker ? tracker.error || null : null,
      at: adbInfo.at,
    };
  }

  // Deduplicated: the tracker reports its state on every reconnect attempt.
  let lastAdbSig = null;
  function _emitAdb() {
    const adb = _adbView();
    const sig = JSON.stringify(adb);
    if (sig === lastAdbSig) return;
    lastAdbSig = sig;
    emit("adb", { adb });
  }

  // ---------------------------------------------------------- busy / throttle / pre-warm

  function _externallyBusy() {
    const dl = (_safe(() => deps.getRunningDownloads(), []) || []).length > 0;
    const ca = !!_safe(() => deps.isCheckAllRunning(), false);
    const se = !!_safe(() => deps.isSearchRunning(), false);
    return { heavy: dl || ca, any: dl || ca || se, why: dl ? "download" : ca ? "check-all" : se ? "search" : null };
  }

  function _busyTick() {
    if (!started || !hashPool) return;
    const b = _externallyBusy();
    hashPool.setConcurrency(b.heavy ? 1 : _workers());
    if ((b.any || current) && prewarmAc) prewarmAc.abort();
  }

  function _schedulePrewarm(ms) {
    if (!started) return;
    clearTimeout(prewarmTimer);
    prewarmTimer = setTimeout(() => {
      prewarmTimer = null;
      _prewarm().catch((e) => _log("prewarm", e && e.message));
    }, ms);
    if (prewarmTimer.unref) prewarmTimer.unref();
  }

  function _prewarmEmit(patch) {
    prewarm = { ...prewarm, ...patch, at: now() };
    emit("prewarm", { prewarm });
  }

  async function _prewarm() {
    if (!started || prewarmAc) return;
    const cfgs = _targets().map(_cfg);
    if (!cfgs.length) return _prewarmEmit({ state: "idle", done: 0, total: 0 });
    const b = _externallyBusy();
    if (b.any || current) {
      _prewarmEmit({ state: "paused", reason: b.why || "sync-job" });
      return _schedulePrewarm(T.busyPollMs || BUSY_POLL_MS);
    }
    const ac = new AbortController();
    prewarmAc = ac;
    const t0 = now();
    const s0 = { ...hashPool.stats };
    // P5's marker. `run` is THIS pre-warm's share of the pool's counters (a
    // plan may have hashed before it): run.hashed − run.mainThread ran in the
    // eval'd workers. `spawned` is the pool's lifetime worker count.
    const logEnd = (state, files, reason) => {
      const s = hashPool ? hashPool.stats : s0;
      const run = {};
      for (const k of ["hashed", "hits", "mainThread", "workerDeaths"]) run[k] = s[k] - s0[k];
      slog.event("prewarm", { state, files, ms: now() - t0, reason: reason || null, workers: hashPool ? hashPool.limit : null, fallback: hashPool ? hashPool.fallback : null, spawned: s.spawned, run });
    };
    try {
      const pc = await walkLibrary(deps.getLibraryRoot());
      if (!pc.ok) {
        logEnd("error", 0, pc.error);
        return _prewarmEmit({ state: "error", reason: pc.error });
      }
      const files = [];
      const seen = new Set();
      for (const s of pc.series) {
        if (s.imageOnly) continue;
        for (const f of s.files) {
          if (seen.has(f.path)) continue;
          if (cfgs.some((c) => planner.isIncluded(s, c) && profiles.isMirrored(f.name, c))) {
            seen.add(f.path);
            files.push(f);
          }
        }
      }
      _prewarmEmit({ state: "running", done: 0, total: files.length, reason: null });
      let last = 0;
      await hashPool.hashFiles(files, {
        signal: ac.signal,
        onProgress: (done, total, bytesDone, bytesTotal) => {
          if (now() - last < 500 && done < total) return;
          last = now();
          _prewarmEmit({ state: "running", done, total, bytesDone, bytesTotal });
        },
      });
      await hashPool.flush().catch(() => {});
      logEnd("done", files.length);
      _prewarmEmit({ state: "done" });
    } catch (e) {
      // A pause isn't logged: it repeats every busy poll while a download runs.
      if (ac.signal.aborted) {
        _prewarmEmit({ state: "paused", reason: _externallyBusy().why || "sync-job" });
        _schedulePrewarm(T.busyPollMs || BUSY_POLL_MS);
      } else {
        logEnd("error", null, String((e && e.message) || e));
        _prewarmEmit({ state: "error", reason: String((e && e.message) || e) });
      }
    } finally {
      if (prewarmAc === ac) prewarmAc = null;
    }
  }

  // ---------------------------------------------------------- jobs

  function _transportFor(cfg) {
    if (cfg.kind === "adb") return new AdbTransport({ client, serial: cfg.serial, root: cfg.root });
    return new FolderTransport({ root: cfg.root, hashPool });
  }

  // ---- Job lines in the service log (diag-log.js; settings.syncDiagnosticLog)
  // job-start / job-end bracket every job; job-step closes each progress step
  // (probe, listing, hashing, planning, verify, apply…) with its duration and
  // its last counts, so a slow first check or a stalled plan can be read off
  // a user's log. Steps come from _progressOf; a step that repeats (a verify
  // job prepares twice) logs once per run of it. Target NAMES stay out (the
  // log is meant to be attached to a bug report): id and kind only.
  const stepClocks = new Map(); // runId → {step, at, last}

  function _counts(p) {
    const o = {};
    if (p && p.filesTotal != null) Object.assign(o, { done: p.filesDone || 0, total: p.filesTotal });
    if (p && p.bytesTotal > 0) Object.assign(o, { bytesDone: p.bytesDone || 0, bytesTotal: p.bytesTotal });
    return o;
  }
  function _stepMark(runId, p) {
    // A late event after the job ended (its end line closed the last step)
    // must not open a clock nothing will close.
    if (!current || current.runId !== runId) return;
    const c = stepClocks.get(runId);
    if (c && c.step === p.step) {
      c.last = p;
      return;
    }
    const t = now();
    if (c) slog.event("job-step", { runId, step: c.step, ms: t - c.at, ..._counts(c.last) });
    stepClocks.set(runId, { step: p.step, at: t, last: p });
  }
  function _jobLog(ev, job, extra) {
    const t = _target(job.targetId);
    slog.event(ev, { runId: job.runId, phase: job.phase, targetId: job.targetId, kind: t ? t.kind : null, auto: !!job.auto, ...extra });
  }
  /** job-end's fields from a job's status and summary (any phase's shape). */
  function _jobEndFields(job, status, summary, t0) {
    const s = summary || {};
    const o = { status, ms: now() - t0 };
    const refusal = s.refusal && s.refusal.code;
    if (refusal) o.refusal = refusal;
    if (s.reason) o.reason = s.reason;
    if (s.filesTotal != null || s.filesDone != null) Object.assign(o, { done: s.filesDone || 0, total: s.filesTotal || 0, bytesDone: s.bytesDone || 0, bytesTotal: s.bytesTotal || 0 });
    if (s.charged) o.charged = s.charged;
    if (Array.isArray(s.skipped) && s.skipped.length) o.skipped = s.skipped.length;
    if (Array.isArray(s.failed) && s.failed.length) o.failed = s.failed.length;
    // A plan's result: what it offers (planner.planSummary totals).
    const pt = s.plan && s.plan.totals;
    if (pt) {
      o.plan = {
        gen: s.plan.gen != null ? s.plan.gen : null,
        files: pt.files || 0,
        bytes: pt.bytes || 0,
        deletes: (pt.delete && pt.delete.count) || 0,
        renames: (pt.rename && pt.rename.count) || 0,
        needsVerify: s.plan.needsVerify || 0,
      };
    }
    return o;
  }

  /**
   * Run `fn(runId, signal, transport)` as the lane's one job. Answers
   * {ok, runId} at once; the job ends with status completed / cancelled /
   * failed / disconnected and `summary`.
   */
  function _runJob(spec, fn) {
    if (prewarmAc) prewarmAc.abort();
    const b = jobs.begin(spec);
    if (!b.ok) return b;
    const runId = b.runId;
    const ac = new AbortController();
    const t = _target(spec.targetId);
    const cfg = _cfg(t);
    const transport = _transportFor(cfg);
    const job = { runId, phase: spec.phase, targetId: spec.targetId, ac, transport, promise: null, auto: !!spec.auto };
    current = job;
    const t0 = now();
    _jobLog("job-start", job);
    job.promise = (async () => {
      let status = "completed";
      let summary = null;
      try {
        // Every job binds the target's queued Links before its first
        // store.load, so a bind never races the job's own record reads and
        // writes (grep _bindLinks).
        const brt = rts.get(spec.targetId);
        if (brt) await _bindLinks(brt);
        const out = await fn(runId, ac.signal, transport, cfg);
        status = (out && out.status) || "completed";
        summary = (out && out.summary) || null;
      } catch (e) {
        const pol = policyFor(e);
        status = pol.action === "stop" ? pol.status : "failed";
        summary = { reason: pol.reason || (e && e.kind) || "failed", message: e && e.message };
        if (pol.reason === "internal") _log(`${spec.phase} job`, e && e.stack);
      } finally {
        try {
          await transport.close();
        } catch (_) {
          // the device left
        }
        transport.destroy();
      }
      if (ac.signal.aborted && status === "completed") status = "cancelled";
      const sc = stepClocks.get(runId);
      if (sc) slog.event("job-step", { runId, step: sc.step, ms: now() - sc.at, ..._counts(sc.last) });
      stepClocks.delete(runId);
      _jobLog("job-end", job, _jobEndFields(job, status, summary, t0));
      jobs.finish(runId, { status, summary });
      if (current === job) current = null;
      _afterJob(job, status, summary);
    })().catch((e) => _log("job wrapper", e && e.stack));
    return { ok: true, runId };
  }

  function _afterJob(job, status, summary) {
    const lrt = rts.get(job.targetId);
    // A refresh tops up the current plan: the last apply/verify card stays.
    if (lrt && job.phase !== "plan" && job.phase !== "refresh") {
      // The result outlives the re-plan this job queued (the job event's
      // summary is overwritten by the next begin). A verify's adopted plan
      // already went out as the summary; lastRun doesn't repeat it.
      const { plan: _omit, ...rest } = summary || {};
      lrt.lastRun = { runId: job.runId, phase: job.phase, status, auto: !!job.auto, at: now(), summary: rest };
    }
    if (lrt && job.phase === "plan") {
      lrt.lastPlan = { runId: job.runId, status, auto: !!job.auto, reason: (summary && (summary.reason || (summary.refusal && summary.refusal.code))) || null, at: now() };
      // A plan that didn't land holds `plan {ifNeeded}` off until something
      // changes (_replanSoon / a connect / a manual plan): otherwise the
      // Sync tab re-asks forever after a Cancel or an internal failure.
      lrt.autoHold = status !== "completed";
    }
    if (lrt && job.phase === "refresh") {
      if (status === "completed" && summary && summary.plan) {
        // A plan is current again (a Link resolved, a needs-mode answered).
        lrt.lastPlan = { runId: job.runId, status, auto: true, reason: null, at: now() };
        lrt.autoHold = false;
      } else if (status !== "completed" && !(summary && summary.refusal)) {
        // The decision didn't land: a full plan is owed. A failure queues it
        // (a failed plan then holds itself: autoHold, no loop); a cancel or
        // a disconnect only marks it (Re-check, the reconnect's plan).
        if (status === "failed") _replanSoon(job.targetId);
        else {
          lrt.stale = true;
          if (status === "cancelled") lrt.autoHold = true;
        }
      }
    }
    _planChanged(job.targetId);
    // Prompt bookkeeping for automatic connect plans. Any plan of a waiting
    // target counts (a manual Re-check that ran first included).
    if (job.phase === "plan") {
      for (const [k, batch] of [...autoBatches]) {
        if (!batch.waiting.has(job.targetId)) continue;
        batch.waiting.delete(job.targetId);
        const rt = rts.get(job.targetId);
        if (status === "completed" && rt && rt.plan) {
          batch.summaries.push({ ...planner.promptSummary(rt.plan, _selection(rt), rt.prep && rt.prep.device), gen: rt.gen });
        } else {
          const a = _attentionOf(job.targetId, status, summary);
          if (a) batch.attention.push(a);
        }
        if (!batch.waiting.size) _batchSettle(k, batch);
      }
      if (status === "completed" && prompts) _resolvePrompts(job.targetId);
      _refreshLibraryStatus();
    }
    if (job.phase === "refresh" && status === "completed") {
      if (prompts) _resolvePrompts(job.targetId);
      _refreshLibraryStatus();
    }
    // A decision made while this job ran (config op, Link, setting) is
    // derived now from whatever the job left; _deriveNow returns early when
    // a full plan is owed (an apply/prune/rename queues one).
    if (lrt && (lrt.rederive || lrt.links.some((l) => !l.bound))) {
      lrt.rederive = false;
      _derive(job.targetId);
    }
    // A selection change made during this job on a header-less target.
    const srt = rts.get(job.targetId);
    if (srt && srt.selDeferred) _persistSelection(srt, job.targetId).catch((e) => _log("selection write", e && e.message));
    // A pre-warm that a job paused resumes once the lane is free.
    if (prewarm.state === "paused") _schedulePrewarm(0);
    setImmediate(_pump);
  }

  function _pump() {
    if (!started || current) return;
    for (;;) {
      const next = jobs.takeNextAuto();
      if (!next) return;
      const t = _target(next.targetId);
      if (!t || !_isConnected(t)) {
        _batchDrop(next.targetId);
        // Like _dropQueued: a decision that never got its refresh is owed a full plan.
        const drt = next.kind === "refresh" ? rts.get(next.targetId) : null;
        if (drt) {
          drt.stale = true;
          _planChanged(next.targetId);
        }
        continue;
      }
      const r = next.kind === "refresh" ? _startRefresh(next.targetId) : _startPlan(next.targetId, { auto: true });
      if (r.ok) return;
      _batchDrop(next.targetId);
    }
  }

  function _selection(rt) {
    return planner.effectiveSelection(rt.plan, rt.selOps || {});
  }

  // PERSIST ALWAYS (deviation 14, user decision 2026-10-10). selection.json
  // is tied to the record's epoch, and a target never applied to has no
  // header, so the first selection change writes the identity-only header
  // (sync:link does the same before binding). First-sync rules key on
  // header.firstApplyAt, not on the header existing, so this changes nothing
  // else. While a job runs on this target the header write is deferred
  // (selDeferred, flushed by _afterJob): that job's prepare reloads the store,
  // and a load racing the header write could drop the new epoch.
  async function _persistSelection(rt, targetId) {
    if (!rt.store.epoch) await rt.store.load();
    if (!rt.store.epoch) {
      if (current && current.targetId === targetId) {
        rt.selDeferred = true;
        return false;
      }
      const t = _target(targetId);
      if (!t || !rt.prep) return false;
      const ex = new Executor({ transport: { kind: t.kind }, store: rt.store, target: rt.prep.target, settings, probe: rt.prep.probe, view: rt.prep.view, now });
      await ex.ensureHeader();
      rt.selEpoch = rt.store.epoch;
    }
    rt.selDeferred = false;
    await rt.store.writeSelection({ ops: rt.selOps || {}, rebinds: rt.selRebinds || {} });
    return true;
  }

  // update-target changed deletePolicy: rt.plan still carries the OLD
  // policy's pre-selection, which is what tells a user's flip from a stored
  // default (planner.js pruneDefaultChoices). No plan yet = nothing to tell
  // them apart by, so the stored choices stay.
  async function _prunePolicyChoices(targetId) {
    const rt = rts.get(targetId);
    if (!rt || !rt.plan || !rt.selOps) return;
    const ops = planner.pruneDefaultChoices(rt.plan, rt.selOps);
    if (ops === rt.selOps) return;
    rt.selOps = ops;
    await _persistSelection(rt, targetId);
  }

  // The device-ready line (prepare.js deviceReady; deviation 14) goes out
  // once per job, immediately, as progress.preflight. A job can prepare
  // twice (a verify job, a plan with verifyFirst); only the first counts.
  // It stays in job.progress, so a renderer re-adopting the job through
  // get-state still sees it.
  let preflightSent = 0; // runId

  function _progressOf(runId) {
    return (ev) => {
      if (ev.phase === "device-ready") {
        if (preflightSent === runId) return;
        preflightSent = runId;
        jobs.progress(runId, { step: "device-ready", preflight: ev.preflight || null }, { immediate: true });
        return;
      }
      // `at` is main's clock, so the renderer's throughput/ETA (U2-7) don't
      // measure IPC jitter; `lane` = this series' landed writes (apply only).
      const p = { step: ev.phase || null, at: now() };
      if (ev.filesDone != null) Object.assign(p, { filesDone: ev.filesDone, filesTotal: ev.filesTotal, bytesDone: ev.bytesDone, bytesTotal: ev.bytesTotal, current: ev.current || null, lane: ev.lane || null });
      else if (ev.done != null) Object.assign(p, { filesDone: ev.done, filesTotal: ev.total, bytesDone: ev.bytesDone || 0, bytesTotal: ev.bytesTotal || 0, current: ev.folder || null });
      _stepMark(runId, p);
      jobs.progress(runId, p);
    };
  }

  /**
   * The planning options one prepare uses: the target's stored verifyMode
   * (its needs-mode answer), with `extra` (this call's one-shot choice: a
   * Verify's mode, a Re-check's rehash) winning where it is set. Undefined
   * falls through to settings inside prepare. The removal policy isn't an
   * option: buildPlan reads it off the target config.
   */
  function _planOpts(cfg, extra) {
    const out = { hashMode: undefined, verifyMode: cfg.verifyMode || undefined };
    for (const [k, v] of Object.entries(extra || {})) if (v !== undefined) out[k] = v;
    return out;
  }

  async function _prepare(rt, cfg, transport, signal, runId, extra) {
    const opts = _planOpts(cfg, extra);
    const prep = await preparePlan({
      transport,
      store: rt.store,
      hashPool,
      target: cfg,
      settings,
      libraryRoot: deps.getLibraryRoot(),
      opts,
      signal,
      onProgress: _progressOf(runId),
      runId,
      now,
      connectWaitMs: T.connectWaitMs,
      executorDeps: T.executorDeps,
    });
    if (prep && prep.ok) prep.usedOpts = opts;
    return _stripFacts(rt, prep, opts);
  }

  /**
   * A needs-mode refusal carries the prepare's facts (prepare.js, `prep`) so
   * the chooser's answer can be a verify-only refresh. They stay here: a
   * refusal goes to the renderer (rt.refusal, summary.refusal) and the facts
   * hold Maps of every listing. grep needsModeFacts for the reader.
   */
  function _stripFacts(rt, r, opts) {
    if (!r || r.ok !== false || !r.prep) return r;
    const { prep: facts, ...refusal } = r;
    rt.needsModeFacts = { ...facts, usedOpts: opts };
    return refusal;
  }

  /**
   * What a summary was built with: Review's read-only "Planned with…" line
   * (design D6). hashMode/verifyMode are what THIS plan's prepare used, so a
   * one-shot Re-hash shows on its own plan and is gone from the next.
   */
  function _policyOf(rt) {
    const used = (rt.prep && rt.prep.usedOpts) || {};
    return {
      deletePolicy: rt.plan ? rt.plan.deletePolicy : null,
      hashMode: used.hashMode || settings.syncHashMode || "cached",
      verifyMode: used.verifyMode || null,
    };
  }

  /**
   * Capacity for the gauge (user decision 2) and Review's fit line. `free`
   * is prepare's one reading; the rest is space.js simulateRun over the
   * selection, the same model and series order the executor admits and
   * runs, so the gauge can't promise a fit the executor refuses (grep:
   * simulateRun). Fields:
   *   needed   how far free space dips during the run (Review's "needs")
   *   lowest   free space at that tightest moment
   *   shortBy / shortAt  bytes missing above the reserve, and the first
   *            series that overruns (0 / null when it fits)
   *   after    free once the run is done; only removals the plan can prove
   *            count (a gated delete whose copy may not land is not credited)
   *   freeable every ticked removal's bytes, proven or not
   *   fits     the executor's admission test; null while free is unknown
   */
  function _capacity(rt, sel) {
    const ops = planner.selectedOps(rt.plan, sel);
    const kind = rt.prep && rt.prep.target ? rt.prep.target.kind : "folder";
    const fsr = (rt.prep && rt.prep.freeSpace) || { bytes: null, asOf: null };
    const free = fsr.bytes;
    const sim = simulateRun(kind, ops, { free, reserve: RESERVE_BYTES });
    return {
      free,
      // UI-pass U2-5: the gauge's "used" part; null when the transport
      // can't say (then the bar is drawn against free space alone).
      total: fsr.total == null ? null : fsr.total,
      needed: sim.needed,
      lowest: sim.lowest,
      reserve: RESERVE_BYTES,
      freeable: sim.freeable,
      after: sim.after,
      shortBy: sim.shortBy,
      shortAt: sim.shortAt,
      fits: sim.fits,
      asOf: fsr.asOf,
    };
  }

  /**
   * The target's summary under the current selection: the planner's ledger
   * plus gen, capacity, policy and every warning (prepare's AND the
   * planner's own plan.warnings, which never reached the renderer before).
   * The one builder: adopt, set-selection and a rebind all call it.
   */
  function _summarize(rt) {
    const sel = _selection(rt);
    const s = planner.planSummary(rt.plan, sel);
    s.gen = rt.gen;
    s.warnings = [...((rt.prep && rt.prep.warnings) || []), ...(rt.plan.warnings || [])];
    s.capacity = _capacity(rt, sel);
    s.policy = _policyOf(rt);
    return s;
  }

  /** Adopt a full prepare as the target's current plan (a new gen). */
  function _adopt(rt, prep) {
    return _adoptPlan(rt, prep, { full: true, freshView: true });
  }

  /**
   * The one adopt path, full prepares and derived plans alike. Gen ALWAYS
   * bumps: the series sheet and the review re-read only on a gen change
   * (SeriesSheet.jsx seriesDetail effect, ReviewDialog.jsx), and the review
   * hides its "plan changed" notice when the content is equal (reviewSig).
   *   full       the plan measured everything: a full plan owed is paid
   *              (rt.stale). A derived or refreshed plan never clears it.
   *   freshView  prep.view was read from the store after the target's last
   *              bind: the selection epoch rule applies, and the Links it
   *              shows leave rt.links. A derived plan's view is the cached
   *              one with the in-memory selection (_selView), so the
   *              selection stays as it is.
   */
  function _adoptPlan(rt, prep, { full = false, freshView = false } = {}) {
    rt.prep = prep;
    rt.plan = prep.plan;
    rt.gen = ++genSeq;
    rt.refusal = null;
    rt.needsModeFacts = null;
    if (full) rt.stale = false;
    if (freshView) {
      const epoch = prep.view.epoch;
      if (rt.selOps == null || rt.selEpoch !== epoch) rt.selOps = { ...(prep.view.selection.ops || {}) };
      rt.selRebinds = { ...(prep.view.selection.rebinds || {}) };
      rt.selEpoch = epoch;
      rt.recordChanged = false;
      rt.links = rt.links.filter((l) => !l.bound);
    }
    rt.summary = _summarize(rt);
    _armSettle(rt);
    _planChanged(rt.id);
    return rt.summary;
  }

  /**
   * UI-pass U2-6: files younger than the settle age are held (planner
   * SETTLE_MS) and the summary counts down to `settleAt`; nothing else
   * re-plans then, so the countdown would reach 0 and stop. One timer per
   * target, replaced by every adopt, cleared on stop.
   */
  function _armSettle(rt) {
    if (rt.settleTimer) clearTimeout(rt.settleTimer);
    rt.settleTimer = null;
    const at = rt.plan && rt.plan.settleAt;
    if (at == null) return;
    // Clamped: the planner bounds settleAt to now + 2 x SETTLE_MS, but a
    // setTimeout delay past 2^31-1 ms fires after 1 ms, and that loop
    // (adopt → arm → fire → re-plan → adopt) never ends (review R4).
    const ms = Math.min(Math.max(0, at - now()) + SETTLE_REPLAN_SLACK_MS, SETTLE_TIMER_MAX_MS);
    rt.settleTimer = setTimeout(() => {
      rt.settleTimer = null;
      if (started && rts.get(rt.id) === rt) _replanSoon(rt.id);
    }, ms);
    if (rt.settleTimer.unref) rt.settleTimer.unref();
  }

  /**
   * needs-mode for a Verify-first on a device that can't hash: the bound
   * folders a read-back would read, with their bytes, like prepare.js's
   * own needs-mode (UI-pass U3-4), so every chooser can name the cost.
   */
  function _needsMode(prep) {
    const folders = prep && prep.plan ? prep.plan.series.filter((sp) => sp.shardId && sp.deviceFolder && sp.state !== "gone" && sp.state !== "held").map((sp) => sp.deviceFolder) : [];
    return refuse("needs-mode", "this device can't hash files: choose how to verify", {
      modes: ["read-back", "adopt-size"],
      folders,
      bytes: prep && prep.device ? planner.verifyBytesOf(prep.device, folders) : null,
      firstSync: !!(prep && prep.plan && prep.plan.firstSync),
    });
  }

  function _refused(rt, r) {
    rt.refusal = r;
    rt.summary = null;
    _planChanged(rt.id);
    return { status: "failed", summary: { refusal: r } };
  }

  /** overrides: one-shot {hashMode, verifyMode, verifyFirst} for this plan only. */
  function _startPlan(targetId, { auto = false, overrides = {} } = {}) {
    const t = _target(targetId);
    if (!auto && rts.get(targetId)) rts.get(targetId).autoHold = false;
    return _runJob({ phase: "plan", targetId, serial: t.serial, auto }, async (runId, signal, transport, cfg) => {
      const rt = _rt(targetId);
      const opts = { hashMode: overrides.hashMode, verifyMode: overrides.verifyMode };
      let prep = await _prepare(rt, cfg, transport, signal, runId, opts);
      if (!prep.ok) return _refused(rt, prep);
      if (overrides.verifyFirst) {
        const v = await _verifyFolders(rt, prep, transport, signal, runId, overrides.verifyMode || cfg.verifyMode, null);
        // needs-mode is a refusal the Sync tab answers (U2-1), not a failed run.
        if (v.status === "failed" && v.reason === "needs-mode") return _refused(rt, _needsMode(prep));
        if (v.status !== "completed") return { status: v.status, summary: { reason: v.reason } };
        prep = await _replanAfterVerify(rt, prep, v, { transport, signal, runId, cfg, verifyMode: opts.verifyMode });
        if (!prep.ok) return _refused(rt, prep);
      }
      return { status: "completed", summary: { plan: _adopt(rt, prep) } };
    });
  }

  function _executor(rt, prep, transport, signal, runId) {
    return new Executor({
      transport,
      store: rt.store,
      hashPool,
      target: prep.target,
      settings,
      probe: prep.probe,
      view: prep.view,
      signal,
      now,
      connectWaitMs: T.connectWaitMs,
      onProgress: _progressOf(runId),
      isPathBusy: _busyCheck(prep),
      ...(T.executorDeps || {}),
    });
  }

  // Folder identity for the busy check: resolved, and case-folded on win32
  // (NTFS), so "D:\AIO\manga\X" and "d:/aio/manga/X" are one folder.
  function _pathKey(p) {
    const r = path.resolve(String(p));
    return (T.platform || process.platform) === "win32" ? r.toLowerCase() : r;
  }

  /**
   * DOWNLOAD GUARD (UI-pass design decision 11): the executor's isPathBusy.
   * A series folder is busy while a running download writes it: matched by
   * the download's --series-dir (update-check downloads name the folder),
   * else by its URL against the series' .aio_series.json url through
   * library.js normalizeSeriesUrl. A brand-new series has no PC folder yet;
   * the settle age (planner SETTLE_MS) holds its first files. Evaluated per
   * file at apply time (executor _guard), so a download that starts mid-apply
   * still holds its folder. Rows come from downloader.js getRunning.
   */
  function _busyCheck(prep) {
    const urlByPath = new Map();
    for (const s of (prep.pc && prep.pc.series) || []) {
      const u = s.folderPath && s.url ? normalizeSeriesUrl(s.url) : "";
      if (u) urlByPath.set(_pathKey(s.folderPath), u);
    }
    return (folderPath) => {
      if (!folderPath) return false;
      const key = _pathKey(folderPath);
      const url = urlByPath.get(key) || null;
      for (const d of _safe(() => deps.getRunningDownloads(), []) || []) {
        if (!d) continue;
        if (d.seriesDir && _pathKey(d.seriesDir) === key) return true;
        if (url && d.url && normalizeSeriesUrl(d.url) === url) return true;
      }
      return false;
    };
  }

  async function _verifyFolders(rt, prep, transport, signal, runId, mode, folders) {
    const want = folders ? new Set(folders) : null;
    const pcByKey = new Map(prep.pc.series.map((s) => [planner.pcSeriesKey(s.folder), s]));
    const list = [];
    for (const sp of prep.plan.series) {
      if (!sp.shardId || !sp.deviceFolder || (want && !want.has(sp.deviceFolder))) continue;
      if (sp.state === "gone" || sp.state === "held") continue;
      const s = pcByKey.get(sp.seriesKey);
      list.push({
        folder: sp.deviceFolder,
        shardId: sp.shardId,
        binding: sp.binding,
        pcFiles: s ? s.files.filter((f) => f.sha256 && profiles.isMirrored(f.name, prep.target)) : [],
        bytes: planner.verifyBytesOf(prep.device, [sp.deviceFolder]),
      });
    }
    const m = transport.kind === "folder" || (prep.probe.caps && prep.probe.caps.sha256sum) ? "device-hash" : mode;
    if (!VERIFY_MODES.includes(m)) return { status: "failed", reason: "needs-mode" };
    const ex = _executor(rt, prep, transport, signal, runId);
    const v = await ex.verify({ folders: list, mode: m, runId: String(runId) });
    // For _replanAfterVerify: the listings this verify merged against, and
    // the folders it tried (prepare.js verifyAttempted keys).
    v.listings = ex.verifyListings;
    v.attempted = list.map((x) => verifyKeyOf(x));
    return v;
  }

  /**
   * The plan after an explicit Verify (the Verify job, a plan's
   * verifyFirst): the first prepare's facts with the verified folders'
   * fresh listings, topped up by refreshPlan in the same job (plan R11),
   * instead of a second full prepare. A refresh that finds the cache wrong
   * falls back to that full prepare. `verifyMode` is the job's one-shot
   * mode. Returns a prep to adopt, or a refusal.
   */
  async function _replanAfterVerify(rt, prep, v, { transport, signal, runId, cfg, verifyMode }) {
    const folders = new Map(prep.device.folders);
    for (const [name, files] of v.listings || []) folders.set(name, { ok: true, files });
    const after = { ...prep, device: { ...prep.device, folders }, verifyAttempted: new Set([...(prep.verifyAttempted || []), ...(v.attempted || [])]) };
    const opts = _planOpts(cfg, { verifyMode });
    const r = await refreshPlan({ ..._refreshDeps(rt, cfg, transport, signal, runId), prep: _withSel(rt, after), reloadView: true, opts });
    if (r.ok) {
      r.prep.usedOpts = { ...(prep.usedOpts || {}), verifyMode: opts.verifyMode };
      return r.prep;
    }
    if (r.escalate) return _prepare(rt, cfg, transport, signal, runId, { verifyMode });
    return _stripFacts(rt, r, opts);
  }

  /** refreshPlan's inputs every caller shares. */
  function _refreshDeps(rt, cfg, transport, signal, runId) {
    return { transport, store: rt.store, hashPool, target: cfg, settings, signal, onProgress: _progressOf(runId), runId, now, connectWaitMs: T.connectWaitMs, executorDeps: T.executorDeps };
  }

  // A run's result as jobs and lastRun carry it. `skipped` lists files and
  // deletes the executor passed over with their reason (downloading,
  // settling, changed-since-review, listing-failed, …) and `skippedSeries`
  // whole series (invalid-folder-*, folder-reappeared): without them a run
  // that copied less than the review promised looked like a plain success.
  function _runSummary(r) {
    const count = (arr, k) => (arr || []).reduce((m, x) => ((m[x[k]] = (m[x[k]] || 0) + 1), m), {});
    const skipped = [
      ...(r.files || []).filter((f) => f.outcome === "skipped").map((f) => ({ kind: f.kind || "write", folder: f.folder, name: f.name, reason: f.reason || null })),
      ...(r.deletes || []).filter((d) => d.outcome === "skipped").map((d) => ({ kind: "delete", folder: d.folder, name: d.name, reason: d.reason || null })),
    ];
    return {
      skipped: skipped.slice(0, 50),
      skippedReasons: count(skipped, "reason"),
      skippedSeries: (r.skippedSeries || []).slice(0, 50),
      reason: r.reason || null,
      message: r.message || null,
      filesDone: r.filesDone,
      filesTotal: r.filesTotal,
      bytesDone: r.bytesDone,
      bytesTotal: r.bytesTotal,
      charged: r.charged,
      files: count(r.files, "outcome"),
      failed: (r.files || []).filter((f) => f.outcome === "failed").slice(0, 20).map((f) => ({ folder: f.folder, name: f.name, reason: f.reason })),
      deletes: count(r.deletes, "outcome"),
      renames: count(r.renames, "outcome"),
      evictions: r.evictions && r.evictions.length ? r.evictions : undefined,
      prune: r.prune || undefined,
      folders: r.folders || undefined,
      warnings: (r.warnings || []).slice(0, 50),
    };
  }

  /** Queue a fresh plan of this target behind the current job (after an edit or a run). */
  function _replanSoon(targetId) {
    const rt = rts.get(targetId);
    if (rt) rt.autoHold = false;
    if (rt && !rt.stale) {
      rt.stale = true;
      _planChanged(targetId);
    }
    const t = _target(targetId);
    if (!t || !_isConnected(t)) return;
    jobs.enqueueAuto({ targetId, serial: t.serial || null });
    setImmediate(_pump);
  }

  // ---------------------------------------------------------- cheap re-plans
  //
  // Plan sync-temp/plans/device-sync-cheap-replans.md; the service header's
  // "DECISIONS WITHOUT FULL RE-PLANS" has the rules. prepare.js "cached
  // facts" owns derivePlan / refreshPlan; this section decides WHEN.

  /** `view` with the in-memory selection: buildPlan reads its rebinds. */
  function _selView(rt, view) {
    return { ...view, selection: { ops: rt.selOps || {}, rebinds: rt.selRebinds || {} } };
  }

  function _withSel(rt, prep) {
    return { ...prep, view: _selView(rt, prep.view) };
  }

  /**
   * Write the queued Links (rt.links, bound:false) into the record. Single
   * flight per target: a job's start awaits a bind _deriveNow began, and a
   * Link queued while one runs is taken by its next pass. Callers make sure
   * no job of the target is past its start (the job start itself, or
   * _deriveNow outside a job). A target never applied to gets its
   * identity-only header first; a selection change deferred for want of one
   * (selDeferred) is written under it, so the adopt keeps it.
   */
  function _bindLinks(rt) {
    if (rt.bindP) return rt.bindP;
    if (!rt.links.some((l) => !l.bound)) return Promise.resolve(false);
    rt.bindP = (async () => {
      let bound = false;
      for (;;) {
        const todo = rt.links.filter((l) => !l.bound);
        if (!todo.length) return bound;
        const t = _target(rt.id);
        const prep = rt.prep;
        if (!t || !prep) {
          rt.links = [];
          return bound;
        }
        // Against the record as it is: a click validated against the plan
        // must not give a folder or a series a second shard.
        const l0 = await rt.store.load();
        const hadEpoch = rt.store.epoch;
        const ex = new Executor({ transport: { kind: t.kind }, store: rt.store, target: prep.target, settings, probe: prep.probe, view: prep.view, now });
        await ex.ensureHeader();
        if (!hadEpoch && rt.store.epoch) {
          rt.selEpoch = rt.store.epoch;
          if (rt.selOps || rt.selDeferred) await rt.store.writeSelection({ ops: rt.selOps || {}, rebinds: rt.selRebinds || {} });
          rt.selDeferred = false;
        }
        const ci = !l0.header || l0.header.caseInsensitive !== false;
        const fold = (n) => (ci ? String(n).toLowerCase() : String(n));
        for (const l of todo) {
          // The shards this Link replaces (H.link: the orphaned folder's,
          // the gone series') are no clash; any other is a bind that landed
          // since the click.
          const mine = (sh) => sh.shardId === l.rebindShardId || sh.shardId === l.dropShardId;
          const clash = l0.shards.find((sh) => !mine(sh) && (fold(sh.name) === fold(l.deviceFolder) || (l.binding && l.binding.identityKey && sh.identityKey === l.binding.identityKey)));
          if (clash) {
            _log("link dropped: the record already binds", clash.shardId);
            rt.links = rt.links.filter((x) => x !== l);
            continue;
          }
          // Delete before the bind: a crash between the two leaves the
          // series unlinked (the suggestion shows again), never holding two
          // shards on one target.
          if (l.dropShardId) await rt.store.deleteShard(l.dropShardId);
          // The kept shard is rewritten in place (store.bindFolder), so the
          // folder's name is never claimed by two shards.
          l.shardId = await rt.store.bindFolder({ name: l.deviceFolder, binding: l.binding, shardId: l.rebindShardId || newShardId() });
          l.bound = true;
          rt.recordChanged = true;
          bound = true;
        }
      }
    })().finally(() => {
      rt.bindP = null;
    });
    return rt.bindP;
  }

  /**
   * A review decision changed this target's derivation inputs (config, a
   * Link, a rebind, a planning setting): re-derive its plan. Serialized per
   * target; resolves when this derive is done (a refresh it asks for runs
   * later, as a job). Never throws.
   */
  function _derive(targetId) {
    const rt = rts.get(targetId);
    if (!rt) {
      if (_target(targetId)) _replanSoon(targetId);
      return Promise.resolve();
    }
    rt.deriving += 1;
    _planChanged(targetId);
    const run = rt.deriveChain
      .then(() => _deriveNow(rt))
      .catch((e) => _log("derive", e && e.stack))
      .finally(() => {
        rt.deriving -= 1;
        _planChanged(targetId);
      });
    rt.deriveChain = run;
    return run;
  }

  async function _deriveNow(rt) {
    const id = rt.id;
    const live = () => started && rts.get(id) === rt && !!_target(id);
    // A running job of the target holds the record and owns the adopt: the
    // derive runs when it ends (_afterJob).
    const deferred = () => {
      if (!_jobOn(id)) return false;
      rt.rederive = true;
      return true;
    };
    if (!live() || deferred()) return;
    if (rt.links.some((l) => !l.bound)) {
      try {
        await _bindLinks(rt);
      } catch (e) {
        // A record write that failed: those Links are dropped, not retried
        // forever; the row shows its suggestion again.
        _log("link bind", e && e.message);
        rt.links = rt.links.filter((l) => l.bound);
      }
      if (!live() || deferred()) return;
    }
    if (!rt.prep) return _replanSoon(id);
    // R8: a full plan is owed (library change, settle, an apply); it reads
    // the new config itself, and a derived adopt must not look current.
    // _replanSoon queues it if nothing has (a cancelled refresh, a drop on
    // disconnect), so the decision is never left waiting on a Re-check.
    if (rt.stale) return _replanSoon(id);
    const t = _target(id);
    const cfg = _cfg(t);
    let source = rt.prep;
    if (rt.refusal) {
      // Only a needs-mode refusal has facts to derive from; any other
      // refusal is about the device or the record, which a full plan re-reads.
      if (rt.refusal.code !== "needs-mode" || !rt.needsModeFacts) return _replanSoon(id);
      source = rt.needsModeFacts;
    } else if (!rt.summary) return _replanSoon(id);
    let view = source.view;
    let freshView = false;
    if (rt.recordChanged) {
      // A bind the cached view doesn't show: read the record here (disk
      // only; no job of the target runs, `deferred` re-checks after the
      // await). The Link then shows as needs-verify at once, and a device
      // that can't hash refuses needs-mode before any job (plan §1).
      view = await loadView(rt.store, source, cfg);
      if (!live() || deferred()) return;
      freshView = true;
    }
    const opts = _planOpts(cfg, {});
    const d = derivePlan({ prep: source, view: _selView(rt, view), target: cfg, settings, verifyMode: opts.verifyMode, now });
    if (d.ok === false) {
      if (d.code !== "needs-mode") return _replanSoon(id);
      _refused(rt, _stripFacts(rt, d, opts));
      return;
    }
    d.prep.usedOpts = { ...(source.usedOpts || {}), verifyMode: opts.verifyMode };
    if (d.pending) {
      // A stat or a hash is missing: the plan isn't shown until it lands. A
      // target that is away is owed a full plan; its reconnect plans anyway.
      if (_isConnected(t)) return _requestRefresh(id);
      rt.stale = true;
      _planChanged(id);
      return;
    }
    // Only verifies missing: an honest interim plan (needs-verify rows, no
    // pre-selected deletes, planner.js header) while the refresh runs.
    _adoptPlan(rt, d.prep, { freshView });
    if (d.obligations.verify.length && _isConnected(t)) _requestRefresh(id);
  }

  /** Queue a refresh of this target behind the current job. */
  function _requestRefresh(targetId) {
    const t = _target(targetId);
    if (!t) return;
    if (_jobOn(targetId)) {
      _rt(targetId).rederive = true;
    } else if (_isConnected(t)) {
      jobs.enqueueAuto({ targetId, serial: t.serial || null, kind: "refresh" });
      setImmediate(_pump);
    }
    _planChanged(targetId);
  }

  /**
   * The refresh job: bind (the job start), then only the facts the
   * derivation is missing (prepare.js refreshPlan), then adopt. A refresh
   * never queues another refresh: what it couldn't settle becomes a full
   * plan (escalate) or a warning. With no facts to start from (or a full
   * plan owed) it prepares in full instead.
   */
  function _startRefresh(targetId) {
    const t = _target(targetId);
    return _runJob({ phase: "refresh", targetId, serial: t.serial, auto: true }, async (runId, signal, transport, cfg) => {
      const rt = _rt(targetId);
      const nm = rt.refusal && rt.refusal.code === "needs-mode" ? rt.needsModeFacts : null;
      const source = nm || (rt.summary ? rt.prep : null);
      if (!source || rt.stale) {
        const prep = await _prepare(rt, cfg, transport, signal, runId, {});
        if (!prep.ok) return _refused(rt, prep);
        return { status: "completed", summary: { plan: _adopt(rt, prep) } };
      }
      const opts = _planOpts(cfg, {});
      const r = await refreshPlan({ ..._refreshDeps(rt, cfg, transport, signal, runId), prep: _withSel(rt, source), reloadView: rt.recordChanged, opts });
      if (r.ok === false && r.escalate) {
        // The cache is what's wrong (a folder the cached root listing left
        // out): plan in full, next in the lane.
        _replanSoon(targetId);
        return { status: "completed", summary: { escalated: r.escalate, folder: r.folder || null } };
      }
      if (r.ok === false) return _refused(rt, _stripFacts(rt, r, opts));
      r.prep.usedOpts = { ...(source.usedOpts || {}), verifyMode: opts.verifyMode };
      return { status: "completed", summary: { plan: _adoptPlan(rt, r.prep, { freshView: !!r.viewReloaded }), warnings: (r.warnings || []).slice(0, 50) } };
    });
  }

  // ---------------------------------------------------------- library status

  /** `walk`: a library walk already made (_afterLibraryChange shares its own). */
  async function _computeLibraryStatus(walk) {
    const pc = walk || (await walkLibrary(deps.getLibraryRoot()));
    const bySeries = {};
    if (!pc.ok) return { asOf: now(), bySeries, error: pc.error };
    for (const s of pc.series) for (const f of s.files) f.sha256 = hashPool ? hashPool.lookup(f.path, f.size, f.mtimeNs) : null;
    for (const t of _targets()) {
      const cfg = _cfg(t);
      const rt = _rt(t.id);
      const l = await rt.store.load();
      const view = loadRecordView({ header: l.header, shards: l.shards, selection: l.selection, target: cfg, measured: {} });
      const m = planner.libraryStatus({ pc, view, target: cfg, lastPlan: rt.plan });
      for (const [key, v] of m) (bySeries[key] = bySeries[key] || []).push({ targetId: t.id, ...v });
    }
    return { asOf: now(), bySeries };
  }

  /**
   * A library change was announced (notifyLibraryChanged: a download, a
   * scan, a delete, a merge, a metadata edit). Walks the library once and
   * re-plans in full only the targets whose plan was built from a different
   * library (pc-inventory.js pcSignature against prep.pc): a scan that only
   * read the library, such as the Sync tab's cover scan on open, changes
   * nothing and must not re-plan. A plan with no library snapshot to compare
   * (a refusal other than needs-mode, a walk that failed) re-plans as before.
   * The walk also feeds the library status.
   */
  async function _afterLibraryChange(reasons) {
    const walk = await walkLibrary(deps.getLibraryRoot());
    if (!started) return;
    const sig = pcSignature(walk);
    const replanned = [];
    for (const t of _targets()) {
      const rt = rts.get(t.id);
      if (!_isConnected(t) || !rt || !rt.plan) continue;
      const facts = rt.refusal ? (rt.refusal.code === "needs-mode" ? rt.needsModeFacts : null) : rt.prep;
      if (sig != null && facts && pcSignature(facts.pc) === sig) continue;
      _replanSoon(t.id);
      replanned.push(t.id);
    }
    slog.event("library-change", { reasons, replanned });
    _refreshLibraryStatus(walk);
    _schedulePrewarm(0);
  }

  function _refreshLibraryStatus(walk) {
    if (!started) return;
    _computeLibraryStatus(walk).then(
      (s) => {
        // A walk that outlived a stop describes a library sync no longer watches.
        if (!started) return;
        libStatus = s;
        emit("library-status", { status: s });
      },
      (e) => _log("library status", e && e.message),
    );
  }

  // ---------------------------------------------------------- handlers

  const H = {};

  // Settings as the renderer sees them: chapterPatterns holds RegExps.
  function _publicSettings() {
    const { chapterPatterns, ...rest } = settings;
    return rest;
  }

  H["get-state"] = async () => {
    if (!enabled) {
      return { ok: true, enabled: false, settings: _publicSettings(), specs: SYNC_SETTING_SPECS, config: null, adb: null, devices: [], folderPresence: {}, job: jobs.snapshot(), plans: {}, prompt: null, prompts: [], pending: [], find: null, prewarm: null };
    }
    if (!(await _ensureStarted())) return refuse("disabled", "sync could not start");
    const plans = {};
    for (const t of _targets()) plans[t.id] = _planView(t.id);
    const shown = prompts.shown();
    const pending = prompts.pending();
    return {
      ok: true,
      enabled: true,
      settings: _publicSettings(),
      specs: SYNC_SETTING_SPECS,
      config: _publicConfig(),
      adb: _adbView(),
      ..._devicePayload(),
      job: jobs.snapshot(),
      plans,
      prompt: shown[0] || null,
      prompts: shown,
      pending,
      find: findSources.status(),
      prewarm,
      // Settings → Sync → Advanced "Open log folder" (openFolder IPC). Only
      // while on: the folder isn't created while sync is off.
      logDir: path.join(root, "logs"),
    };
  };

  H["label-preview"] = async (p) => {
    const patterns = p.patterns != null ? p.patterns : settings.syncChapterPatterns;
    return { ok: true, ...labelPreview({ name: p.name, folderName: p.folderName, patterns }) };
  };

  // runId (optional): the job the caller showed. A Cancel clicked as that job
  // ended must not abort the next queued job, maybe another target's.
  H.cancel = async (p) => {
    if (!current) return { ok: true, wasRunning: false };
    if (p && p.runId != null && p.runId !== current.runId) return { ok: true, wasRunning: false };
    current.ac.abort();
    return { ok: true, wasRunning: true };
  };

  H["find-sources:cancel"] = async () => {
    if (!findSources) return { ok: true, wasRunning: false };
    return { ok: true, ...findSources.cancel() };
  };

  // --- config ops

  const OPS = {};

  function _cleanTarget(src, base) {
    const out = { ...(base || {}) };
    for (const k of TARGET_FIELDS) if (k in src) out[k] = src[k] == null ? null : src[k];
    if (out.root && out.kind === "adb") out.root = String(out.root).replace(/\/+$/, "") || "/";
    return out;
  }

  function _validate(t) {
    return profiles.validateTarget(t, { libraryRoot: _safe(() => deps.getLibraryRoot(), null), userDataDir: root, platform: T.platform });
  }

  function _jobOn(targetId) {
    return !!current && current.targetId === targetId;
  }

  function _listEdit(p, field, fn) {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const list = Array.isArray(t[field]) ? t[field].slice() : [];
    const r = fn(list);
    if (r && r.ok === false) return r;
    return { patch: { [field]: r || list } };
  }

  async function _identityClear(targetId, cfg) {
    const rt = _rt(targetId);
    const l = await rt.store.load();
    const keep = l.header && !profiles.recordIdentityChanged(l.header, cfg) ? l.header : null;
    const id = profiles.recordIdentity(cfg);
    if (cfg.kind === "folder") {
      // The header pins the volume measured now (prepare.js compares it).
      // Bounded: this runs inside _configTxn, and realpath/stat on a dead
      // network path can hang for long; every other config edit (and
      // apply's _clearEvictionMarks) would wait behind it. 3 s like
      // FolderTransport.checkPresence; on a timeout the old volume stands
      // and prepare.js measures the real one on the next plan.
      const tp = new FolderTransport({ root: cfg.root });
      let probeTimer = null;
      const pr = await Promise.race([
        tp.probe().catch(() => null),
        new Promise((r) => {
          probeTimer = setTimeout(() => r(null), T.identityProbeMs != null ? T.identityProbeMs : 3000);
        }),
      ]);
      clearTimeout(probeTimer);
      id.volumeId = pr && pr.volumeId != null ? String(pr.volumeId) : keep ? keep.volumeId || null : null;
    }
    await rt.store.clear({
      version: 1,
      ...id,
      canonicalRoot: null,
      caseInsensitive: keep ? !!keep.caseInsensitive : true,
      caseMeasured: keep ? !!keep.caseMeasured : false,
      caps: {},
    });
    rt.prep = rt.plan = rt.summary = rt.refusal = rt.gen = null;
    rt.selOps = null;
    rt.selRebinds = {};
    // Links and cached facts belong to the old identity's record.
    rt.links = [];
    rt.recordChanged = false;
    rt.rederive = false;
    rt.needsModeFacts = null;
    // A new identity owes nothing to the old one's failed plan or settle
    // countdown; config-op's _replanSoon then plans it if it is reachable.
    rt.autoHold = false;
    rt.lastPlan = null;
    if (rt.settleTimer) clearTimeout(rt.settleTimer);
    rt.settleTimer = null;
    if (findSources) await findSources.forget(targetId);
    _planChanged(targetId);
  }

  OPS["add-target"] = async (p) => {
    if (!_isObj(p.target)) return refuse("invalid", "target required");
    // A new target starts with the global "Removals for new targets"
    // (design D5); changing that setting later never moves this target.
    const t = _cleanTarget(p.target, {
      id: `t${crypto.randomBytes(5).toString("hex")}`,
      deletePolicy: settings.syncDeletePolicy || "guarded",
      excludes: [],
      aliases: [],
      ignoredSuggestions: [],
      acknowledged: [],
    });
    const errors = _validate(t);
    if (errors.length) return refuse("invalid", errors[0].message, { errors });
    return { next: { ...config, targets: [..._targets(), t] }, targetId: t.id };
  };

  OPS["update-target"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    if (!_isObj(p.patch)) return refuse("invalid", "patch required");
    const next = _cleanTarget(p.patch, t);
    // A reader switch carries the custom-only fields (profiles.js
    // CUSTOM_FIELDS) the patch leaves out: into custom, seeded from what the
    // old preset resolved, so the switch itself changes no file; out of
    // custom, cleared, since a preset stores none (validateTarget refuses).
    if ("profile" in p.patch && next.profile !== _cfg(t).profile) {
      const prev = _cfg(t);
      for (const f of profiles.CUSTOM_FIELDS) {
        if (f in p.patch) continue;
        next[f] = next.profile === "custom" ? (Array.isArray(prev[f]) ? prev[f].slice() : prev[f]) : null;
      }
    }
    const errors = _validate(next);
    if (errors.length) return refuse("invalid", errors[0].message, { errors });
    const identity = profiles.recordIdentityChanged(_cfg(t), _cfg(next));
    if (identity && _jobOn(t.id)) return refuse("busy", "that target's job is running");
    // An identity edit clears the record and must plan in full (review R10:
    // with no re-plan a target whose last plan failed stayed autoHold
    // forever); any other field only changes the derivation (_derive).
    if (identity) return { next: { ...config, targets: _targets().map((x) => (x.id === t.id ? next : x)) }, after: () => _identityClear(t.id, _cfg(next)), targetId: t.id, replan: "full" };
    // A Removals change: stored choices that merely repeat the old policy's
    // default follow the new one (planner.js pruneDefaultChoices), persisted
    // before the derive reads them.
    const policyChanged = "deletePolicy" in p.patch && _cfg(next).deletePolicy !== _cfg(t).deletePolicy;
    return { next: { ...config, targets: _targets().map((x) => (x.id === t.id ? next : x)) }, after: policyChanged ? () => _prunePolicyChoices(t.id) : null, targetId: t.id, replan: "derive" };
  };

  OPS["remove-target"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    if (_jobOn(t.id)) return refuse("busy", "that target's job is running");
    return {
      next: { ...config, targets: _targets().filter((x) => x.id !== t.id) },
      after: async () => {
        jobs.dropTarget(t.id);
        _batchDrop(t.id);
        rts.delete(t.id);
        _planChanged(t.id); // → {removed:true}
        if (findSources) await findSources.forget(t.id);
        const dir = path.join(root, "targets", t.id);
        await fs.promises.rm(dir, { recursive: true, force: true }).catch((e) => _log("remove-target rm", e && e.message));
      },
    };
  };

  OPS["forget-record"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    if (_jobOn(t.id)) return refuse("busy", "that target's job is running");
    return { next: null, after: () => _identityClear(t.id, _cfg(t)), targetId: t.id, replan: "full" };
  };

  OPS["set-adb-path"] = async (p) => {
    const v = p.path == null || p.path === "" ? null : String(p.path);
    if (v != null && (!path.isAbsolute(v) || /[\0\r\n]/.test(v))) return refuse("invalid", "the adb path must be absolute");
    adbInfo = { ...adbInfo, candidates: null };
    return { next: { ...config, adbPath: v } };
  };

  // Deep equality for stored list entries (strings or flat objects).
  const _sameEntry = (a, b) => {
    if (!_isObj(a) || !_isObj(b)) return a === b;
    const ka = Object.keys(a).filter((k) => a[k] != null);
    const kb = Object.keys(b).filter((k) => b[k] != null);
    return ka.length === kb.length && ka.every((k) => a[k] === b[k]);
  };
  const _seriesRef = (e) => _isObj(e) && (typeof e.identityKey === "string" || typeof e.url === "string" || typeof e.pcFolder === "string");
  const _pick = (e, keys) => Object.fromEntries(keys.filter((k) => typeof e[k] === "string" && e[k]).map((k) => [k, e[k]]));

  OPS["add-alias"] = async (p) =>
    _listEdit(p, "aliases", (list) => {
      const a = p.alias;
      if (!_seriesRef(a) || typeof a.deviceFolder !== "string") return refuse("invalid", "an alias needs a series and a device folder");
      const why = segmentProblem(a.deviceFolder);
      if (why) return refuse("invalid", `invalid device folder name (${why})`);
      list.push(_pick(a, ["identityKey", "url", "pcFolder", "deviceFolder"]));
      return list;
    });
  // Removals name the ENTRY, not its index (review R12): ops are queued and
  // sent with the newest config version, so an index taken from a render
  // before the previous removal landed pointed at the wrong entry. A missing
  // entry is a no-op, so a double click removes one.
  OPS["remove-alias"] = async (p) =>
    _listEdit(p, "aliases", (list) => {
      if (!_isObj(p.entry)) return refuse("invalid", "entry required");
      const i = list.findIndex((x) => _sameEntry(x, p.entry));
      if (i >= 0) list.splice(i, 1);
      return list;
    });
  OPS["add-exclude"] = async (p) =>
    _listEdit(p, "excludes", (list) => {
      if (!_seriesRef(p.entry)) return refuse("invalid", "an exclude needs a series");
      const e = _pick(p.entry, ["identityKey", "url", "pcFolder"]);
      if (p.entry.removeFiles === true) e.removeFiles = true;
      list.push(e);
      return list;
    });
  // An exclude can change under the chip that names it (_clearEvictionMarks
  // strips removeFiles after an apply), so an exact match falls back to the
  // same series: the click meant that series, whatever its flags are now.
  OPS["remove-exclude"] = async (p) =>
    _listEdit(p, "excludes", (list) => {
      if (!_isObj(p.entry)) return refuse("invalid", "entry required");
      let i = list.findIndex((x) => _sameEntry(x, p.entry));
      if (i < 0 && _seriesRef(p.entry)) i = list.findIndex((x) => _sameSeries(x, _pick(p.entry, ["identityKey", "url", "pcFolder"])));
      if (i >= 0) list.splice(i, 1);
      return list;
    });

  /**
   * One add or remove on a list field (R12): formats, sidecars and
   * cleanupNames go through update-target's validation and custom seeding;
   * `only` through set-selection-mode's cleaning. The base is the stored
   * list (or, for a custom field, what the profile resolves), never a copy
   * the renderer rendered, so two quick chip clicks both land.
   */
  OPS["edit-list"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const field = p.field;
    if (!["formats", "sidecars", "cleanupNames", "only"].includes(field)) return refuse("invalid", "that field isn't a list");
    if (("add" in p) === ("remove" in p)) return refuse("invalid", "one of add or remove");
    const base = field === "only" ? t.only || [] : _cfg(t)[field] || [];
    const has = (v) => base.some((x) => _sameEntry(x, v));
    let next = base.slice();
    if ("add" in p && !has(p.add)) next.push(p.add);
    if ("remove" in p) next = next.filter((x) => !_sameEntry(x, p.remove));
    if (field === "only") return OPS["set-selection-mode"]({ targetId: t.id, mode: _cfg(t).selection, only: next });
    return OPS["update-target"]({ targetId: t.id, patch: { [field]: next } });
  };
  /** The series a plan names as a stored list entry (excludes, only). */
  function _planSeriesRef(targetId, seriesKey) {
    const rt = rts.get(targetId);
    const sp = rt && rt.plan ? rt.plan.bySeriesKey.get(String(seriesKey)) : null;
    if (!sp) return null;
    return _pick({ identityKey: sp.identityKey, url: sp.urlKey, pcFolder: sp.folder }, ["identityKey", "url", "pcFolder"]);
  }

  /**
   * True when a stored list entry refers to `ref`'s series by any one key:
   * looser than naming.findSeriesEntry's precedence on purpose, so removing
   * or including a series clears every entry that could still match it.
   */
  function _sameSeries(entry, ref) {
    if (!entry) return false;
    if (ref.identityKey && entry.identityKey === ref.identityKey) return true;
    if (ref.url && entry.url && normalizeSeriesUrl(entry.url) === normalizeSeriesUrl(ref.url)) return true;
    return !!ref.pcFolder && entry.pcFolder === ref.pcFolder;
  }

  // "Remove from <target>" (approved design D7): stop syncing the series
  // there AND remove its files, through Review. One saved exclude entry
  // carries both (removeFiles; planner.js 8b turns it into delete ops), so an
  // automatic re-plan can't drop the pending removal. In `only` mode the
  // series also leaves the only-list, or the exclude would mean nothing.
  OPS["remove-series"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const ref = _planSeriesRef(t.id, p.seriesKey);
    if (!ref) return refuse("invalid", "that series isn't in this target's plan");
    const excludes = (t.excludes || []).filter((e) => !_sameSeries(e, ref));
    // removeFiles:false is the detail sheet's Exclude (U2-8): stop syncing,
    // leave the folder on the target (it becomes a preserved device-only row).
    excludes.push(p.removeFiles === false ? { ...ref } : { ...ref, removeFiles: true });
    const only = (t.only || []).filter((e) => !_sameSeries(e, ref));
    return { patch: { excludes, only } };
  };
  // The ledger's Include (design D8) and the undo of remove-series: every
  // exclude of the series goes (its removeFiles mark with it), and in `only`
  // mode it joins the only-list.
  OPS["include-series"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const ref = _planSeriesRef(t.id, p.seriesKey);
    if (!ref) return refuse("invalid", "that series isn't in this target's plan");
    const excludes = (t.excludes || []).filter((e) => !_sameSeries(e, ref));
    const only = (t.only || []).slice();
    if (_cfg(t).selection === "only" && !only.some((e) => _sameSeries(e, ref))) only.push(ref);
    return { patch: { excludes, only } };
  };

  OPS["set-selection-mode"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    if (!profiles.SELECTION_MODES.includes(p.mode)) return refuse("invalid", "unknown series selection mode");
    const only = Array.isArray(p.only) ? p.only.filter(_seriesRef).map((e) => _pick(e, ["identityKey", "url", "pcFolder"])) : t.only || [];
    return { patch: { selection: p.mode, only } };
  };
  const _idList = (field, key, add) => async (p) =>
    _listEdit(p, field, (list) => {
      const id = p[key];
      if (typeof id !== "string" || !id) return refuse("invalid", `${key} required`);
      const has = list.includes(id);
      if (add && !has) list.push(id);
      if (!add && has) list.splice(list.indexOf(id), 1);
      return list;
    });
  OPS["ack-anomaly"] = _idList("acknowledged", "anomalyId", true);
  OPS["unack-anomaly"] = _idList("acknowledged", "anomalyId", false);
  OPS["ignore-suggestion"] = _idList("ignoredSuggestions", "suggestionId", true);
  OPS["unignore-suggestion"] = _idList("ignoredSuggestions", "suggestionId", false);

  H["config-op"] = (p) => _configTxn(() => _configOp(p));
  async function _configOp(p) {
    const fn = OPS[p.op];
    if (!fn) return refuse("invalid", `unknown config op ${p.op}`);
    if (p.expectVersion != null && p.expectVersion !== config.version) {
      return refuse("version-conflict", "the sync settings changed meanwhile", { config: _publicConfig() });
    }
    let r = await fn(p);
    if (r && r.ok === false) return r;
    if (r.patch) {
      const t = _target(p.targetId);
      const next = { ...t, ...r.patch };
      r = { next: { ...config, targets: _targets().map((x) => (x.id === t.id ? next : x)) }, targetId: t.id, replan: "derive" };
    }
    if (r.next) await _saveConfig(r.next);
    if (r.after) await r.after();
    if (r.next) emit("config", { config: _publicConfig() });
    // `replan`: "full" (the cache is invalid: identity, forget-record) or
    // "derive" (a review decision; plan device-sync-cheap-replans.md).
    // Not awaited: the reply is the config, and a derive is never refused.
    if (r.targetId && r.replan === "derive") _derive(r.targetId);
    else if (r.targetId && r.replan) _replanSoon(r.targetId);
    return { ok: true, config: _publicConfig(), ...(r.targetId && p.op === "add-target" ? { targetId: r.targetId } : {}) };
  }

  // --- adb

  H["locate-adb"] = async (p) => {
    await _adbBinary({ rescan: !!p.rescan });
    let serverVersion = null;
    if (adbInfo.binary) {
      const s = await _ensureServer();
      serverVersion = s.ok ? s.version : null;
    }
    return { ok: true, candidates: adbInfo.candidates || [], resolved: adbInfo.binary, serverVersion, error: adbInfo.error };
  };

  /** R1: keep the tracker up for DISCOVER_MS with no adb target (see _syncWatchers). */
  function _discover() {
    discoverUntil = now() + (T.discoverMs != null ? T.discoverMs : DISCOVER_MS);
    clearTimeout(discoverTimer);
    discoverTimer = setTimeout(() => {
      discoverTimer = null;
      _syncWatchers();
    }, discoverUntil - now() + 50);
    if (discoverTimer.unref) discoverTimer.unref();
    _syncWatchers();
  }

  H["list-devices"] = async () => {
    const s = await _ensureServer();
    if (!s.ok) return refuse("disconnected", `no adb server: ${adbInfo.error}`);
    _discover();
    const rows = await client.devices();
    // U3-7: "I've allowed it" / "Re-check" on the connect card. A full list
    // is as authoritative as a poll, so it feeds the same diff (a tablet
    // just authorized connects now, not on the tracker's next frame). The
    // tracker is always up here (_discover), so the list never goes stale.
    _onDevices(rows, { source: "manual" });
    return { ok: true, devices: rows.map((d) => ({ serial: d.serial, state: d.state, stateKind: d.stateKind, model: d.model, product: d.product, transportId: d.transportId, targetable: d.targetable })) };
  };

  function _validRemote(p) {
    return typeof p === "string" && p.startsWith("/") && !/[\0\r\n]/.test(p) && !p.split("/").includes("..") && Buffer.byteLength(p) <= 1018;
  }

  H["browse-remote"] = async (p) => {
    if (typeof p.serial !== "string" || !p.serial) return refuse("invalid", "serial required");
    const want = p.path == null ? "/sdcard" : String(p.path);
    if (!_validRemote(want)) return refuse("invalid", "an absolute device path without '..'");
    const s = await _ensureServer();
    if (!s.ok) return refuse("disconnected", `no adb server: ${adbInfo.error}`);
    const dev = client.device(p.serial);
    const session = dev.sync();
    try {
      let canonical = want.replace(/\/+$/, "") || "/";
      try {
        const rp = await dev.shell(`realpath ${shq(canonical)}`);
        const out = rp.stdout.toString("utf8").replace(/\r?\n$/, "");
        if (rp.exitCode === 0 && _validRemote(out)) canonical = out;
      } catch (e) {
        if (e && (e.kind === "device-lost" || e.kind === "offline" || e.kind === "unauthorized")) return refuse("disconnected", e.message);
      }
      const l = await session.list(canonical);
      const entries = (l.entries || l.dirs || [])
        .filter((e) => e && e.name && e.name !== "." && e.name !== ".." && (e.type === "dir" || e.isDir))
        .map((e) => ({ name: e.name, isDir: true }))
        .sort((a, b) => a.name.localeCompare(b.name));
      // /sdcard is a symlink: STA2 the presets under its real path.
      let sdcard = "/sdcard";
      try {
        const rs = await dev.shell("realpath /sdcard");
        const out = rs.stdout.toString("utf8").replace(/\r?\n$/, "");
        if (rs.exitCode === 0 && _validRemote(out)) sdcard = out;
      } catch (_) {
        // keep /sdcard
      }
      const presets = [];
      for (const pr of BROWSE_PRESETS) {
        const real = pr.path === "/sdcard" ? sdcard : pr.path.startsWith("/sdcard/") ? sdcard + pr.path.slice(7) : pr.path;
        const st = await session.stat(real).catch(() => ({ ok: false }));
        presets.push({ ...pr, exists: !!st.ok && st.type === "dir" });
      }
      const parent = canonical === "/" ? null : canonical.slice(0, canonical.lastIndexOf("/")) || "/";
      return { ok: true, path: canonical, parent, entries, presets };
    } catch (e) {
      if (e && ["device-lost", "offline", "unauthorized", "connecting", "server-unavailable"].includes(e.kind)) return refuse("disconnected", e.message);
      throw e;
    } finally {
      await session.close().catch(() => {});
    }
  };

  // --- jobs

  function _jobPre(p) {
    const t = _target(p.targetId);
    if (!t) return { refusal: refuse("invalid", "unknown target") };
    if (current) return { refusal: refuse("busy", "another sync job is running", { job: jobs.snapshot().job }) };
    if (!_isConnected(t)) return { refusal: refuse("disconnected", t.kind === "adb" ? "the device isn't connected" : "the folder isn't available") };
    return { t };
  }

  async function _libraryPresent() {
    try {
      return (await fs.promises.stat(deps.getLibraryRoot())).isDirectory();
    } catch (_) {
      return false;
    }
  }

  H.plan = async (p) => {
    if (p.ifNeeded === true) return _planIfNeeded(p);
    const pre = _jobPre(p);
    if (pre.refusal) return pre.refusal;
    if (!(await _libraryPresent())) return refuse("library-missing", "the library folder can't be read");
    const o = _isObj(p.overrides) ? p.overrides : {};
    // Removals are a target field now (design D5): an override would be a
    // second, invisible policy. Refused loudly so a stale caller can't think
    // it re-planned add-only.
    if ("deletePolicy" in o || "clearOverrides" in o) return refuse("invalid", "removals are a target setting: update-target {deletePolicy}");
    const overrides = {
      hashMode: ["cached", "rehash"].includes(o.hashMode) ? o.hashMode : undefined,
      verifyFirst: o.verifyFirst === true,
      verifyMode: ["read-back", "adopt-size"].includes(o.verifyMode) ? o.verifyMode : undefined,
    };
    const rt = _rt(pre.t.id);
    const stored = _cfg(pre.t).verifyMode;
    if (rt.prep && rt.prep.probe && pre.t.kind === "adb" && !(rt.prep.probe.caps || {}).sha256sum && overrides.verifyFirst && !overrides.verifyMode && !stored) {
      return _needsMode(rt.prep);
    }
    return _startPlan(pre.t.id, { overrides });
  };

  /**
   * `plan {ifNeeded:true}` (UI-pass U2-1): the Sync tab asks on every open
   * and target switch, and the SERVICE decides, so a renderer that unmounts
   * on each tab switch can't loop or miss a stale plan. Plans only when the
   * target is connected and has no current summary (none, or stale), holds
   * no refusal, isn't queued or running, and its last plan didn't fail
   * (autoHold). Queued as an automatic plan (jobs.enqueueAuto dedups),
   * never refused `busy`. {ok, planned:boolean, why} says what it did.
   */
  async function _planIfNeeded(p) {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const rt = rts.get(t.id);
    const why = !_isConnected(t)
      ? "disconnected"
      : current && current.targetId === t.id
        ? "running"
        : jobs.snapshot().queue.some((q) => q.targetId === t.id)
          ? "queued"
          : rt && rt.refusal
            ? "refused"
            : rt && rt.summary && !rt.stale
              ? "fresh"
              : rt && rt.autoHold
                ? "held"
                : null;
    if (why) return { ok: true, planned: false, why };
    if (!(await _libraryPresent())) return refuse("library-missing", "the library folder can't be read");
    jobs.enqueueAuto({ targetId: t.id, serial: t.serial || null });
    setImmediate(_pump);
    return { ok: true, planned: true, why: null };
  }

  H["series-detail"] = async (p) => {
    const rt = rts.get(p.targetId);
    if (!rt || !rt.plan) return refuse("invalid", "no plan for that target");
    return planner.seriesDetail(rt.plan, String(p.seriesKey), _selection(rt));
  };

  // The Start review (contract.js "review"): planner.planReview plus what
  // only the service knows. `gen` is what apply must send back; `busy` says
  // a writing job holds the target (the review is read-only meanwhile).
  H.review = async (p) => {
    if (!_target(p.targetId)) return refuse("invalid", "unknown target");
    const rt = rts.get(p.targetId);
    if (!rt || !rt.plan || !rt.summary) return refuse("invalid", "plan this target first");
    const sel = _selection(rt);
    return {
      ...planner.planReview(rt.plan, sel),
      gen: rt.gen,
      // The selection this review describes (U2-11): apply {selSeq} must
      // name it, so a toggle that lands after the review was read can't
      // ride on its Start (review R6).
      selSeq: rt.selSeq,
      stale: rt.stale || _updating(rt),
      capacity: _capacity(rt, sel),
      policy: _policyOf(rt),
      warnings: rt.summary.warnings,
      // A refresh is about to replace this plan: Start is gated, not refused (R18).
      busy: !!current && current.targetId === p.targetId && (QUIT_ASK_PHASES.has(current.phase) || current.phase === "refresh"),
    };
  };

  H["set-selection"] = async (p) => {
    const rt = rts.get(p.targetId);
    // !summary: the target is refused (_refused keeps plan/gen but nulls the
    // summary); a toggle must not rebuild a summary beside the refusal.
    if (!rt || !rt.plan || !rt.summary) return refuse("invalid", "no plan for that target");
    // An apply/prune/rename on this target has already taken its op list:
    // a toggle now would show as applied and do nothing (UI-pass decision 9).
    // A plan job may run: its adopt keeps the selection (same epoch).
    if (current && current.targetId === p.targetId && QUIT_ASK_PHASES.has(current.phase)) {
      return refuse("busy", "that target's sync is running", { job: jobs.snapshot().job });
    }
    // U2-3: a toggle made on an older plan would land on the new plan's op
    // of the same id. Optional so in-process callers needn't track gens.
    if (p.gen != null && p.gen !== rt.gen) return _genMismatch(rt, p.gen);
    let changedSeries = [];
    if (_isObj(p.rebind)) {
      if (current) return refuse("busy", "another sync job is running");
      const r = planner.applyRebind(rt.plan, rt.selRebinds, p.rebind);
      if (!r.ok) return r;
      rt.selRebinds = r.rebinds;
      await _persistSelection(rt, p.targetId);
      // The folder name is part of every op id: a derived plan, a new gen.
      // A name never probed tops up as a stat-only refresh.
      await _derive(p.targetId);
    } else {
      const r = planner.changeSelection(rt.plan, rt.selOps, p);
      if (!r.ok) return r;
      rt.selOps = r.ops;
      await _persistSelection(rt, p.targetId).catch((e) => _log("selection write", e && e.message));
      rt.summary = _summarize(rt);
      changedSeries = r.changedSeries;
    }
    if (Number.isInteger(p.seq)) rt.selSeq = p.seq;
    _planChanged(p.targetId);
    const ev = planner.evaluateSelection(rt.plan, _selection(rt));
    return {
      ok: true,
      gen: rt.gen,
      seq: p.seq == null ? null : p.seq,
      totals: ev.totals,
      losses: ev.losses,
      lossIfSelected: ev.lossIfSelected,
      massDelete: ev.massDelete,
      massRepush: ev.massRepush,
      changedSeries,
      stale: rt.stale || _updating(rt),
    };
  };

  H.verify = async (p) => {
    const pre = _jobPre(p);
    if (pre.refusal) return pre.refusal;
    if (p.mode != null && !VERIFY_MODES.includes(p.mode)) return refuse("invalid", "unknown verify mode");
    const rt = _rt(pre.t.id);
    const caps = rt.prep && rt.prep.probe && rt.prep.probe.caps;
    // No mode named: the target's stored needs-mode answer.
    const mode = p.mode != null ? p.mode : _cfg(pre.t).verifyMode || undefined;
    if (pre.t.kind === "adb" && caps && !caps.sha256sum && (!mode || mode === "device-hash")) {
      return _needsMode(rt.prep);
    }
    const folders = Array.isArray(p.folders) ? p.folders.map(String) : null;
    // Re-check's "Re-hash" + "Read back" together (design D4, U2-9): the
    // PC-side choice rides this one verify job's prepares, then is gone.
    const hashMode = ["cached", "rehash"].includes(p.hashMode) ? p.hashMode : undefined;
    const vopts = { verifyMode: mode === "device-hash" ? undefined : mode, hashMode };
    return _runJob({ phase: "verify", targetId: pre.t.id, serial: pre.t.serial }, async (runId, signal, transport, cfg) => {
      let prep = await _prepare(rt, cfg, transport, signal, runId, vopts);
      if (!prep.ok) return _refused(rt, prep);
      const v = await _verifyFolders(rt, prep, transport, signal, runId, mode, folders);
      if (v.status === "failed" && v.reason === "needs-mode") return _refused(rt, _needsMode(prep));
      if (v.status !== "completed") return { status: v.status, summary: _runSummary(v) };
      prep = await _replanAfterVerify(rt, prep, v, { transport, signal, runId, cfg, verifyMode: vopts.verifyMode });
      if (!prep.ok) return _refused(rt, prep);
      return { status: "completed", summary: { ..._runSummary(v), plan: _adopt(rt, prep) } };
    });
  };

  // Mass acknowledgments over IPC are the COUNT the review showed (U2-2):
  // reconcileForApply blocks unless it equals the fresh count, so a
  // selection change that grows the set after the tick can't ride on it.
  // A bare `true` is not accepted from the renderer.
  function _ackArgs(p) {
    const n = (v) => (Number.isInteger(v) && v >= 0 ? v : false);
    return { ackLosses: Array.isArray(p.ackLosses) ? p.ackLosses.map(String) : [], ackMassDelete: n(p.ackMassDelete), ackMassRepush: n(p.ackMassRepush) };
  }

  H.apply = async (p) => {
    const pre = _jobPre(p);
    if (pre.refusal) return pre.refusal;
    const rt = rts.get(pre.t.id);
    if (!rt || !rt.plan) return refuse("invalid", "plan this target first");
    if (!Number.isInteger(p.gen)) return refuse("invalid", "gen required: the plan generation the review showed");
    if (!(await _libraryPresent())) return refuse("library-missing", "the library folder can't be read");
    // After the await: a rebind may have swapped the plan meanwhile.
    const changed = _genMismatch(rt, p.gen);
    if (changed) return changed;
    // A Link or a decision not in this plan yet: the review is about to change.
    if (_updating(rt)) return refuse("blocked", "the plan is being updated", { blocking: [{ kind: "plan-changed", gen: rt.gen }] });
    // Optional so in-process callers needn't track it; the renderer always sends it.
    if (p.selSeq !== undefined && p.selSeq !== rt.selSeq) {
      return refuse("blocked", "the selection changed since the review", { blocking: [{ kind: "plan-changed", gen: rt.gen }] });
    }
    const acks = _ackArgs(p);
    // Against the reviewed plan first: what the user can fix without a re-plan.
    const pre2 = planner.reconcileForApply({ reviewed: rt.plan, fresh: rt.plan, storedOps: rt.selOps, ...acks });
    if (!pre2.ok) return refuse("blocked", "the selection needs acknowledgments first", { blocking: pre2.blocking, dropped: pre2.dropped });
    return _startApply(pre.t.id, acks, rt.plan);
  };

  /** The `plan-changed` refusal when `gen` isn't the target's current plan, else null. */
  function _genMismatch(rt, gen) {
    if (rt.plan && rt.summary && rt.gen === gen) return null;
    return refuse("blocked", "the plan changed since you reviewed it", { blocking: [{ kind: "plan-changed", gen: rt.summary ? rt.gen : null, reviewedGen: gen }] });
  }

  /**
   * `reviewed` is the plan whose gen the caller just matched, captured
   * synchronously before the job starts: apply runs only its selected ops
   * that the fresh re-plan still holds unchanged (reconcileForApply).
   */
  function _startApply(targetId, acks, reviewed) {
    const t = _target(targetId);
    return _runJob({ phase: "apply", targetId, serial: t.serial }, async (runId, signal, transport, cfg) => {
      const rt = _rt(targetId);
      const fresh = await _prepare(rt, cfg, transport, signal, runId, {});
      if (!fresh.ok) return _refused(rt, fresh);
      const rc = planner.reconcileForApply({ reviewed, fresh: fresh.plan, storedOps: rt.selOps, ...acks });
      if (!rc.ok) {
        _adopt(rt, fresh);
        return { status: "failed", summary: { refusal: refuse("blocked", "the device or library changed since the review", { blocking: rc.blocking, dropped: rc.dropped }) } };
      }
      const r = await _executor(rt, fresh, transport, signal, runId).apply({ plan: fresh.plan, ops: rc.ops, device: fresh.device, pc: fresh.pc, ackLosses: acks.ackLosses, runId: String(runId) });
      await _clearEvictionMarks(targetId, fresh.plan, r.evictions).catch((e) => _log("eviction mark", e && e.message));
      _replanSoon(targetId);
      if (r.ok === false) return { status: "failed", summary: { refusal: r, dropped: rc.dropped } };
      return { status: r.status, summary: { ..._runSummary(r), dropped: rc.dropped } };
    });
  }

  /**
   * A removal whose folder is gone has done its job: its exclude entry stays
   * (the series stays off the target) but loses removeFiles, so a folder of
   * that name appearing later isn't offered for deletion on the old click.
   */
  function _clearEvictionMarks(targetId, plan, evictions) {
    return _configTxn(() => _clearEvictionMarksNow(targetId, plan, evictions));
  }
  async function _clearEvictionMarksNow(targetId, plan, evictions) {
    const done = (evictions || []).filter((x) => x.removed);
    const t = _target(targetId);
    if (!done.length || !t) return;
    const refs = done.map((x) => plan.bySeriesKey.get(x.seriesKey)).filter(Boolean).map((sp) => ({ identityKey: sp.identityKey, url: sp.urlKey, pcFolder: sp.folder }));
    let changed = false;
    const excludes = (t.excludes || []).map((e) => {
      if (!e.removeFiles || !refs.some((r) => _sameSeries(e, r))) return e;
      changed = true;
      const { removeFiles: _drop, ...rest } = e;
      return rest;
    });
    if (!changed) return;
    await _saveConfig({ ...config, targets: _targets().map((x) => (x.id === t.id ? { ...t, excludes } : x)) });
    emit("config", { config: _publicConfig() });
  }

  H.prune = async (p) => {
    const pre = _jobPre(p);
    if (pre.refusal) return pre.refusal;
    // Always an explicit list (UI-pass decision 3): "no list = every orphan"
    // turned one click on an empty selection into removing them all.
    if (!Array.isArray(p.folders) || !p.folders.length) return refuse("invalid", "name the device folders to remove");
    const want = new Set(p.folders.map(String));
    return _runJob({ phase: "prune", targetId: pre.t.id, serial: pre.t.serial }, async (runId, signal, transport, cfg) => {
      const rt = _rt(pre.t.id);
      const prep = await _prepare(rt, cfg, transport, signal, runId, {});
      if (!prep.ok) return _refused(rt, prep);
      const folders = prep.plan.deviceOnly.filter((d) => d.state === "orphaned" && d.shardId && want.has(d.name)).map((d) => ({ name: d.name, shardId: d.shardId }));
      // A named folder that isn't orphaned on the fresh listing is reported,
      // never silently dropped.
      const taken = new Set(folders.map((f) => f.name));
      const notOrphaned = [...want].filter((n) => !taken.has(n));
      const r = await _executor(rt, prep, transport, signal, runId).prune({ folders, runId: String(runId) });
      _replanSoon(pre.t.id);
      return { status: r.status, summary: { ..._runSummary(r), notOrphaned } };
    });
  };

  H["rename-device-folder"] = async (p) => {
    const pre = _jobPre(p);
    if (pre.refusal) return pre.refusal;
    if (typeof p.to !== "string" || segmentProblem(p.to)) return refuse("invalid", "invalid device folder name");
    if (typeof p.seriesKey !== "string") return refuse("invalid", "seriesKey required");
    return _runJob({ phase: "rename", targetId: pre.t.id, serial: pre.t.serial }, async (runId, signal, transport, cfg) => {
      const rt = _rt(pre.t.id);
      const prep = await _prepare(rt, cfg, transport, signal, runId, {});
      if (!prep.ok) return _refused(rt, prep);
      const sp = prep.plan.bySeriesKey.get(p.seriesKey);
      if (!sp || !sp.shardId || sp.state === "gone" || sp.state === "held") throw new RunStop("failed", "not-a-bound-folder");
      if (sp.deviceFolder === p.to) return { status: "completed", summary: { reason: "same-name" } };
      const r = await _executor(rt, prep, transport, signal, runId).renameFolder({ shardId: sp.shardId, to: p.to, runId: String(runId) });
      _replanSoon(pre.t.id);
      return { status: r.status, summary: _runSummary(r) };
    });
  };

  // A queued bind (service header, LINK): validated now against the plan
  // AND the Links it doesn't show yet, so two clicks during a job can't give
  // a folder or a series two shards (R2). Bound by _bindLinks, planned by
  // the refresh _derive asks for.
  H.link = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    // An apply/prune/rename holds its op list (the selection-lock rule).
    if (current && current.targetId === t.id && QUIT_ASK_PHASES.has(current.phase)) return refuse("busy", "that target's sync is running", { job: jobs.snapshot().job });
    const rt = rts.get(t.id);
    if (!rt || !rt.plan) return refuse("invalid", "plan this target first");
    const ids = Array.isArray(p.suggestionIds) ? p.suggestionIds.map(String) : [];
    if (!ids.length) return refuse("invalid", "suggestionIds required");
    const picks = [];
    const folders = new Set(rt.links.map((l) => l.deviceFolder));
    const series = new Set(rt.links.map((l) => l.seriesKey));
    for (const id of ids) {
      const g = rt.plan.suggestions.find((x) => x.id === id);
      if (!g) return refuse("invalid", `unknown suggestion ${id}`);
      const sp = rt.plan.bySeriesKey.get(g.seriesKey);
      if (!sp || (sp.state !== "new" && sp.state !== "gone")) return refuse("invalid", `${g.seriesKey} is already bound`);
      if (folders.has(g.deviceFolder) || series.has(g.seriesKey)) return refuse("invalid", "one device folder and one series per link");
      folders.add(g.deviceFolder);
      series.add(g.seriesKey);
      picks.push({ g, sp });
    }
    for (const { g, sp } of picks) {
      // A folder the record already names (orphaned: its series left the
      // library) keeps its shard, rebound to this series; a gone series'
      // own shard is then dropped, so one folder has one shard and one
      // series has one shard. Otherwise a gone series' shard is rebound, or
      // a new one made. (planner.js deviceOnly state "orphaned"; grep
      // dropShardId)
      const du = rt.plan.deviceOnly.find((d) => d.name === g.deviceFolder);
      const orphan = du && du.state === "orphaned" && du.shardId ? du.shardId : null;
      const gone = sp.state === "gone" ? sp.shardId : null;
      rt.links.push({
        suggestionId: g.id,
        seriesKey: g.seriesKey,
        deviceFolder: g.deviceFolder,
        binding: sp.binding,
        rebindShardId: orphan || gone,
        dropShardId: orphan && gone && gone !== orphan ? gone : null,
        bound: false,
      });
    }
    // Binds now unless a job of the target runs; never touches the device.
    await _derive(t.id);
    return { ok: true, queued: true, linked: picks.map(({ g }) => ({ suggestionId: g.id, seriesKey: g.seriesKey, deviceFolder: g.deviceFolder })) };
  };

  // Review opens the Sync tab on the prompt's targets (renderer side);
  // Not now only closes the prompt. Neither starts a job (design D2). A
  // connection still planning can be answered from its Preparing card
  // (U3-3): its card then never shows.
  H["prompt-response"] = async (p) => {
    if (!["review", "not-now"].includes(p.action)) return refuse("invalid", "unknown action");
    const id = String(p.promptId);
    const pr = prompts.respond(id);
    if (!pr) return refuse("invalid", "that prompt is no longer open");
    emit("prompt", { action: "answered", prompt: pr, response: p.action, prompts: prompts.shown(), pending: prompts.pending() });
    return { ok: true };
  };

  H["library-status"] = async () => {
    if (!libStatus) libStatus = await _computeLibraryStatus();
    return { ok: true, ...libStatus };
  };

  H["export-report"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const rt = rts.get(t.id);
    if (!rt || !rt.plan) return refuse("invalid", "plan this target first");
    const sel = _selection(rt);
    const stamp = new Date(now()).toISOString().slice(0, 10);
    const r = await deps.showSaveDialog({
      title: "Export sync report",
      defaultPath: `sync-report-${(t.name || t.id).replace(/[^A-Za-z0-9 _-]/g, "_")}-${stamp}.json`,
      filters: [{ name: "JSON", extensions: ["json"] }],
    });
    if (!r || r.canceled || !r.filePath) return { ok: false, code: "invalid", message: "cancelled", cancelled: true };
    const report = {
      generatedAt: new Date(now()).toISOString(),
      target: { ...t },
      summary: planner.planSummary(rt.plan, sel),
      series: rt.plan.series.map((sp) => planner.seriesDetail(rt.plan, sp.seriesKey, sel)),
      deviceOnly: rt.plan.deviceOnly.map((d) => ({ ...d, rows: rt.plan.deviceOnlyRows.get(d.key) || [] })),
      anomalies: rt.plan.anomalies,
      suggestions: rt.plan.suggestions,
      warnings: (rt.prep && rt.prep.warnings) || [],
    };
    await fs.promises.writeFile(r.filePath, JSON.stringify(report, null, 2));
    return { ok: true, path: r.filePath };
  };

  H["device-cover"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const folder = String(p.folder || "");
    if (segmentProblem(folder)) return refuse("invalid", "invalid folder name");
    const rt = rts.get(t.id);
    const known = rt && rt.plan && (rt.plan.deviceOnly.some((d) => d.name === folder) || rt.plan.series.some((sp) => sp.deviceFolder === folder));
    if (!known) return refuse("invalid", "unknown device folder");
    const file = path.join(rt.store.coversDir, `${crypto.createHash("sha1").update(folder).digest("hex")}.jpg`);
    try {
      if ((await fs.promises.stat(file)).size > 0) return { ok: true, path: file };
    } catch (_) {
      // not cached yet
    }
    if (!_isConnected(t)) return refuse("disconnected", "the device isn't connected");
    const tp = _transportFor(_cfg(t));
    try {
      await tp.probe();
      const buf = await tp.readSmall(`${folder}/cover.jpg`, COVER_MAX_BYTES);
      if (!buf || !buf.length) return { ok: true, path: null };
      await fs.promises.mkdir(path.dirname(file), { recursive: true });
      const tmp = `${file}.${process.pid}.tmp`;
      await fs.promises.writeFile(tmp, buf);
      await fs.promises.rename(tmp, file);
      return { ok: true, path: file };
    } finally {
      await tp.close().catch(() => {});
      tp.destroy();
    }
  };

  H["find-sources:start"] = async (p) => {
    const t = _target(p.targetId);
    if (!t) return refuse("invalid", "unknown target");
    const rt = rts.get(t.id);
    if (!rt || !rt.plan) return refuse("invalid", "plan this target first");
    // Rows follow ALL device-only folders; `folders` only narrows the run.
    const folders = rt.plan.deviceOnly.map((d) => d.name);
    const only = Array.isArray(p.folders) ? p.folders.map(String) : undefined;
    const r = await findSources.start({ targetId: t.id, folders, only });
    if (!r.ok) return refuse(r.code === "busy" ? "busy" : "invalid", r.message);
    return { ok: true, started: r.started };
  };

  H["find-sources:get"] = async (p) => {
    if (!_target(p.targetId)) return refuse("invalid", "unknown target");
    // With a plan, rows are reconciled with its device-only folders first, so
    // the sheet can edit a folder's row before its first search (U4).
    const rt = rts.get(p.targetId);
    return findSources.get(p.targetId, rt && rt.plan ? rt.plan.deviceOnly.map((d) => d.name) : undefined);
  };

  H["find-sources:update-row"] = async (p) => {
    if (!_target(p.targetId)) return refuse("invalid", "unknown target");
    const r = await findSources.updateRow({ targetId: p.targetId, folder: String(p.folder || ""), patch: p.patch });
    if (!r.ok) return refuse(r.code === "busy" ? "busy" : "invalid", r.message);
    return r;
  };

  // ---------------------------------------------------------- dispatch

  async function dispatch(name, payload) {
    const ch = CHANNELS[name];
    try {
      if (payload != null && !_isObj(payload)) return refuse("invalid", "the request must be an object");
      const p = payload || {};
      if (shut) return refuse("disabled", "the app is quitting");
      if (!enabled && !CHANNELS_WHILE_DISABLED.has(ch)) return refuse("disabled", "device sync is off");
      if (enabled && name !== "label-preview" && !(await _ensureStarted())) {
        if (!CHANNELS_WHILE_DISABLED.has(ch)) return refuse("disabled", "device sync could not start");
      }
      const h = H[name];
      if (!h) return refuse("invalid", `no handler for ${name}`);
      return await h(p);
    } catch (e) {
      _log(`handler ${name}`, e && e.stack);
      return refuse("invalid", `internal error: ${(e && e.message) || e}`, { internal: true });
    }
  }

  for (const name of CHANNEL_NAMES) {
    deps.ipcMain.handle(CHANNELS[name], (_event, payload) => dispatch(name, payload));
  }

  deps.onFocus(() => {
    try {
      if (started && prompts) prompts.focus();
    } catch (e) {
      _log("focus", e && e.message);
    }
  });

  if (enabled) _ensureStarted();

  // ---------------------------------------------------------- facade

  const facade = {
    /** save-settings hook: start or stop with syncEnabled; the job is cancelled first. */
    applySettings(merged) {
      try {
        rawSettings = merged || {};
        const next = resolveSyncSettings(rawSettings);
        const was = enabled;
        const prev = settings;
        settings = next;
        enabled = next.syncEnabled;
        // The renderer learns `enabled` from here, not from re-reading
        // get-state after its save (UI-pass push model).
        emit("settings", { enabled, settings: _publicSettings() });
        if (was && enabled && started) {
          const same = (k) => JSON.stringify(prev[k]) === JSON.stringify(next[k]);
          if (!PLANNING_SETTINGS.every(same)) {
            // Hash mode changes what the cached hashes mean: plan in full.
            // The others only change the derivation (R14).
            const full = !same("syncHashMode");
            for (const t of _targets()) if (rts.get(t.id) && rts.get(t.id).plan) (full ? _replanSoon(t.id) : _derive(t.id));
          }
        }
        if (was && !enabled) {
          stopP = _stop().catch((e) => _log("stop", e && e.stack)).finally(() => {
            stopP = null;
          });
          return stopP;
        }
        if (!was && enabled) {
          return _ensureStarted().then(() => {
            if (started) _schedulePrewarm(0);
          });
        }
        if (started && hashPool) hashPool.setConcurrency(_workers());
      } catch (e) {
        _log("applySettings", e && e.stack);
      }
      return Promise.resolve();
    },

    /** A download finished, a scan / merge / delete / metadata edit changed the library. */
    notifyLibraryChanged(reason) {
      try {
        if (!started) return;
        libStatus = null;
        clearTimeout(replanTimer);
        libReasons.add(String(reason || "unknown"));
        replanTimer = setTimeout(() => {
          replanTimer = null;
          const reasons = [...libReasons];
          libReasons.clear();
          _afterLibraryChange(reasons).catch((e) => _log("library change", e && e.stack));
        }, T.replanDebounceMs != null ? T.replanDebounceMs : REPLAN_DEBOUNCE_MS);
        if (replanTimer.unref) replanTimer.unref();
      } catch (e) {
        _log("notifyLibraryChanged", reason, e && e.message);
      }
    },

    /** True while an apply, prune or rename runs (the close listener asks). */
    needsQuitAsk() {
      return _safe(() => jobs.needsQuitAsk(), false);
    },

    /** confirm-quit's payload.sync, or null. */
    quitInfo() {
      return _safe(() => {
        const j = jobs.snapshot().job;
        if (!j || j.state !== "running") return null;
        const t = _target(j.targetId);
        // done/total count files for an apply, folders for a prune or rename
        // (executor.js prune/renameFolder progress).
        return { target: t ? t.name || t.id : j.targetId, phase: j.phase, unit: j.phase === "apply" ? "files" : "folders", done: j.progress.filesDone, total: j.progress.filesTotal };
      }, null);
    },

    isJobRunning() {
      return !!current;
    },

    /** reinstall-python: stop the search tree before python-env is deleted. */
    cancelFindSources() {
      try {
        return findSources ? findSources.cancelAndWait().then((r) => r, () => ({ wasRunning: false })) : Promise.resolve({ wasRunning: false });
      } catch (_) {
        return Promise.resolve({ wasRunning: false });
      }
    },

    /** Quit: cancel and wait (≤ timeoutMs), flush. Never rejects. */
    shutdown({ timeoutMs = SHUTDOWN_MS } = {}) {
      try {
        if (shut) return Promise.resolve();
        const p = _within(_stop({ timeoutMs, reason: "shutdown" }), timeoutMs + 500).catch((e) => _log("shutdown", e && e.stack));
        return p.then(() => {
          shut = true;
        });
      } catch (e) {
        _log("shutdown", e && e.stack);
        return Promise.resolve();
      }
    },

    /**
     * app.on("quit") / app.exit(): synchronous and idempotent. Aborts the
     * job, kills the search's tree synchronously (proc-kill.js
     * killTreeNow; ≤2 s on win32), abandons queued record
     * writes (each file on disk is whole: atomic renames) and flushes the PC
     * hash cache synchronously.
     */
    shutdownNow() {
      if (shut && !started) return;
      shut = true;
      // Reached with the service still up only when shutdown() didn't run
      // first: app.exit() (reinstall-python) or a quit that skipped
      // window-all-closed. hadJob: the job this kill aborts. hadFind: a
      // find-sources run is still registered (reinstall-python's awaited
      // cancel can leave it registered while its last save settles);
      // searchProc: its search process is still attached, i.e. this kill
      // is the one that has to end the tree.
      if (started) {
        const s = findSources && findSources.searcher;
        slog.event("quit-now", { hadJob: current ? current.phase : null, hadFind: !!(findSources && _safe(() => findSources.isRunning(), false)), searchProc: !!(s && _safe(() => s.isRunning(), false)) });
      }
      try {
        if (current) current.ac.abort();
        if (current && current.transport) current.transport.destroy();
      } catch (_) {}
      try {
        if (findSources) findSources.cancelNow();
      } catch (_) {}
      try {
        if (tracker) tracker.stop();
        if (watcher) watcher.stop();
      } catch (_) {}
      try {
        clearInterval(busyTimer);
        clearTimeout(prewarmTimer);
        clearTimeout(replanTimer);
        for (const rt of rts.values()) clearTimeout(rt.settleTimer);
        if (prewarmAc) prewarmAc.abort();
      } catch (_) {}
      try {
        if (queue) queue.abandon();
      } catch (_) {}
      try {
        if (hashPool) {
          hashPool.flushSync();
          hashPool.close();
        }
      } catch (e) {
        _log("shutdownNow flush", e && e.message);
      }
      started = false;
      slogOpen = false;
    },

    // For tools/ tests: the state get-state reports, without IPC.
    _debug() {
      return { started, enabled, current: current ? { runId: current.runId, phase: current.phase, targetId: current.targetId, signal: current.ac.signal } : null, rts, prompts, tracker, findSources, hashPool, autoBatches };
    },
  };
  return facade;
}

module.exports = { initDeviceSync, validateDeps, DEPS, PREWARM_DELAY_MS, REPLAN_DEBOUNCE_MS, SHUTDOWN_MS, CONFIG_FILE };
