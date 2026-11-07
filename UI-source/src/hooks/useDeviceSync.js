// ============================================================
// useDeviceSync — renderer mirror of Device Sync (main: electron/sync/service.js)
//
// Mounted ONCE, by App.jsx, for the app's lifetime (like useUpdateCheck);
// every Sync surface reads it from App's props.
//
// DEV SEAM: useDeviceSync({source}) reads `source` instead of
// window.electronAPI — an object with the same sync* wrappers and
// onSyncEvent. Only the DEV gallery passes one (src/dev/SyncGallery.jsx,
// fixtures from tools/_gen_sync_fixtures.js); it never touches
// window.electronAPI, whose presence turns on every other hook's IPC.
// `source` is read at mount; a later change is ignored.
//
// STATE OWNERSHIP: main owns everything. This hook adopts the sync:get-state
// snapshot, then applies "sync-event" pushes by kind. Same startup ordering
// as useUpdateCheck: events that arrive before the snapshot resolves are
// BUFFERED and drained after it, so adoption is a plain overwrite.
//
// JOB EVENTS (sync/job-record.js): begin and finish carry the full `job`
// view; progress carries only `progress`. A begin/finish with a runId older
// than the mirrored job, or a progress for any other runId, is dropped (a
// stale run's late event after the snapshot).
//
// PLANS ARE PUSHED (UI pass U0): a `plans` event carries one target's whole
// view {summary, refusal, stale, updating, links, lastRun, gen, selSeq,
// lastPlan} whenever any of it changes (field meanings: sync/contract.js
// EVENT_KINDS; the reducer copies fields BY NAME, so a new field needs a line
// in the "plans" case),
// and `removed:true` once the target is gone. The hook only overwrites; it
// never re-reads get-state to catch up (that re-read raced main's identity
// clear and lost job results). `adb` and `settings` events likewise. The one
// re-read left: a `settings` event that flips `enabled` adopts a fresh
// snapshot, since turning sync on or off replaces every field at once.
//
// LIBRARY STATUS (UI-pass U4-2, the Library cover chip and DetailView line):
// asked once when sync is on, then replaced by `library-status` events;
// dropped when sync turns off. Shape {asOf, bySeries: {folderPath:
// [{targetId, state, pending}]}} (service.js _computeLibraryStatus).
//
// SELECTION CLIENT (UI-pass U2-10): actions.select(targetId, req) queues an
// edit through selectionQueue.js (pure; batching, FIFO, one request in
// flight, pending until a `plans` push's selSeq reaches it). It lives here,
// not in the Sync tab, because the tab unmounts on every tab switch and its
// pending marks must outlive that. state.selection[targetId] is that queue;
// selectionQueue.isSettled() says when Review's Start may run.
//
// CONFIG-OP CLIENT (review R12 + the plausible "one client per surface"
// finding): actions.configOp is THE app's one config-op client. Ops are
// serialized (one in flight, the rest queued in call order) across every
// surface, and each is sent with the newest config version known here:
// assigned (not max'd) from every `config` event, snapshot and reply, so a
// service whose config went backwards (file deleted, sync toggled) doesn't
// lock edits out. Sending the newest version is safe because every op is an
// intent (an entry-named removal, an edit-list add/remove, a scalar patch),
// not a list rebuilt from a render. useConfigOps.js only counts pending ops
// for its surface's buttons.
//
// Read by: App.jsx (grep useDeviceSync). Depends on: preload.js sync* wrappers and
// onSyncEvent. The event kinds below duplicate sync/contract.js EVENT_KINDS;
// tools/_test_device_sync_contract.js keeps them equal, and
// tools/_test_device_sync_hook.js runs this file. grep: deviceSync
// ============================================================

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import * as SQ from "./selectionQueue";

const EVENT_KINDS = ["device", "job", "config", "prompt", "find", "prewarm", "library-status", "plans", "adb", "settings"];

const api = () => (typeof window !== "undefined" && window.electronAPI && typeof window.electronAPI.syncGetState === "function" ? window.electronAPI : null);

const INITIAL = Object.freeze({
  loaded: false,
  enabled: false,
  settings: null,
  specs: null,
  config: null,
  adb: null,
  devices: [],
  folderPresence: {},
  job: null,
  queue: [],
  plans: {},
  prompt: null,
  prompts: [],
  // UI-pass U3-3: connections still planning (the Preparing card), from
  // every `prompt` event and get-state.
  pending: [],
  find: null,
  findRows: {},
  prewarm: null,
  libraryStatus: null,
  logDir: null,
  selection: {}, // targetId → selectionQueue state (renderer-only)
});

// Action name → preload wrapper. get-state is refresh(); events come from
// onSyncEvent. Every wrapper resolves {ok:true,…} or {ok:false, code, message}.
const ACTIONS = {
  configOp: "syncConfigOp",
  locateAdb: "syncLocateAdb",
  listDevices: "syncListDevices",
  browseRemote: "syncBrowseRemote",
  plan: "syncPlan",
  seriesDetail: "syncSeriesDetail",
  setSelection: "syncSetSelection",
  verify: "syncVerify",
  apply: "syncApply",
  prune: "syncPrune",
  renameDeviceFolder: "syncRenameDeviceFolder",
  cancel: "syncCancel",
  promptResponse: "syncPromptResponse",
  libraryStatus: "syncLibraryStatus",
  labelPreview: "syncLabelPreview",
  exportReport: "syncExportReport",
  deviceCover: "syncDeviceCover",
  findSourcesStart: "syncFindSourcesStart",
  findSourcesCancel: "syncFindSourcesCancel",
  findSourcesGet: "syncFindSourcesGet",
  findSourcesUpdateRow: "syncFindSourcesUpdateRow",
  link: "syncLink",
  review: "syncReview",
};

function rowsByFolder(rows) {
  const out = {};
  for (const r of rows || []) if (r && typeof r.folder === "string") out[r.folder] = r;
  return out;
}

/** Pure reducer: one pushed event onto the mirrored state. Unknown kinds are ignored. */
function applySyncEvent(s, ev) {
  if (!ev || typeof ev !== "object" || !EVENT_KINDS.includes(ev.kind)) return s;
  switch (ev.kind) {
    case "device":
      return { ...s, devices: Array.isArray(ev.devices) ? ev.devices : s.devices, folderPresence: ev.folderPresence || s.folderPresence };
    case "job": {
      const cur = s.job;
      if (ev.job) {
        if (cur && typeof ev.runId === "number" && ev.runId < cur.runId) return s;
        return { ...s, job: ev.job };
      }
      if (!cur || ev.runId !== cur.runId || cur.state !== "running") return s;
      return { ...s, job: { ...cur, progress: { ...cur.progress, ...(ev.progress || {}) } } };
    }
    case "config":
      return { ...s, config: ev.config || null };
    case "prompt": {
      const prompts = Array.isArray(ev.prompts) ? ev.prompts : s.prompts;
      const pending = Array.isArray(ev.pending) ? ev.pending : s.pending;
      return { ...s, prompts, prompt: prompts[0] || null, pending };
    }
    case "find": {
      // Batch events carry done/total; a row edit (find-sources updateRow)
      // carries only the row and leaves the batch status alone.
      let find = s.find;
      if (typeof ev.done === "number") {
        const live = ev.state === "running" || ev.state === "waiting";
        find = live ? { targetId: ev.targetId, waiting: ev.state === "waiting" ? ev.waiting || null : null, done: ev.done, total: ev.total } : null;
      }
      if (!ev.row || typeof ev.row.folder !== "string" || !ev.targetId) return { ...s, find };
      const rows = { ...(s.findRows[ev.targetId] || {}), [ev.row.folder]: ev.row };
      return { ...s, find, findRows: { ...s.findRows, [ev.targetId]: rows } };
    }
    case "prewarm":
      return { ...s, prewarm: ev.prewarm || null };
    case "library-status":
      // A late event from a walk that began before sync went off is stale.
      if (!s.enabled) return s;
      return { ...s, libraryStatus: ev.status || null };
    case "plans": {
      if (typeof ev.targetId !== "string") return s;
      const plans = { ...s.plans };
      if (ev.removed) delete plans[ev.targetId];
      else {
        plans[ev.targetId] = {
          summary: ev.summary || null,
          refusal: ev.refusal || null,
          stale: !!ev.stale,
          updating: !!ev.updating,
          links: Array.isArray(ev.links) ? ev.links : [],
          lastRun: ev.lastRun || null,
          gen: ev.gen == null ? null : ev.gen,
          selSeq: Number.isInteger(ev.selSeq) ? ev.selSeq : null,
          lastPlan: ev.lastPlan || null,
        };
      }
      return { ...s, plans };
    }
    case "adb":
      return { ...s, adb: ev.adb || null };
    case "settings":
      return { ...s, enabled: !!ev.enabled, settings: ev.settings || s.settings };
    default:
      return s;
  }
}

function fromSnapshot(snap) {
  const j = snap.job || {};
  return {
    ...INITIAL,
    loaded: true,
    enabled: !!snap.enabled,
    settings: snap.settings || null,
    specs: snap.specs || null,
    config: snap.config || null,
    adb: snap.adb || null,
    devices: Array.isArray(snap.devices) ? snap.devices : [],
    folderPresence: snap.folderPresence || {},
    job: j.job || null,
    queue: Array.isArray(j.queue) ? j.queue : [],
    plans: snap.plans || {},
    prompt: snap.prompt || null,
    prompts: Array.isArray(snap.prompts) ? snap.prompts : [],
    pending: Array.isArray(snap.pending) ? snap.pending : [],
    find: snap.find || null,
    prewarm: snap.prewarm || null,
    logDir: typeof snap.logDir === "string" ? snap.logDir : null,
  };
}

export function useDeviceSync(opts) {
  const sourceRef = useRef(opts && opts.source ? opts.source : null);
  const getApi = useCallback(() => sourceRef.current || api(), []);
  const [state, setState] = useState(INITIAL);
  const hydratedRef = useRef(false);
  const bufferRef = useRef([]);
  // The last `enabled` the hook adopted, read by onEvent without a render.
  const enabledRef = useRef(false);
  const refreshSeqRef = useRef(0);
  // Selection queues: the ref is the source of truth (timers and replies
  // read it synchronously); state.selection mirrors it for rendering.
  const selRef = useRef({});
  const selTimers = useRef({});
  const stateRef = useRef(state);
  stateRef.current = state;
  // CONFIG-OP CLIENT (header): newest known config version + the op chain.
  const cfgVersionRef = useRef(0);
  const cfgChainRef = useRef(Promise.resolve());
  const _seeVersion = (cfg) => {
    if (cfg && typeof cfg.version === "number") cfgVersionRef.current = cfg.version;
  };

  function _selPut(targetId, next) {
    if (selRef.current[targetId] === next) return;
    selRef.current = { ...selRef.current, [targetId]: next };
    setState((s) => ({ ...s, selection: { ...s.selection, [targetId]: next } }));
  }

  /** Send the target's next queued edit if none is in flight. */
  const pumpSel = useCallback((targetId) => {
    const cur = selRef.current[targetId] || SQ.EMPTY;
    const view = stateRef.current.plans[targetId];
    const { st, request } = SQ.takeNext(cur, view ? view.selSeq : null);
    if (!request) return;
    _selPut(targetId, st);
    const a = getApi();
    const call = a ? Promise.resolve(a.syncSetSelection({ targetId, ...request })) : Promise.resolve({ ok: false, code: "disabled", message: "electronAPI unavailable" });
    call
      .catch((e) => ({ ok: false, code: "invalid", message: (e && e.message) || "the request failed" }))
      .then((reply) => {
        _selPut(targetId, SQ.onReply(selRef.current[targetId] || SQ.EMPTY, request.seq, reply));
        pumpSel(targetId);
      });
  }, [getApi]);

  /** Re-read the whole snapshot (latest request wins). */
  const refresh = useCallback(() => {
    const a = getApi();
    if (!a) return Promise.resolve(null);
    const seq = ++refreshSeqRef.current;
    return Promise.resolve(a.syncGetState())
      .then((snap) => {
        if (seq === refreshSeqRef.current && snap && snap.ok !== false) {
          enabledRef.current = !!snap.enabled;
          _seeVersion(snap.config);
          // Library status survives a re-read while sync stays on (only its
          // own events and the load below replace it); off drops it.
          // An enabled FLIP (s.enabled differs) drops it too: a status from
          // before sync was off describes a library that may have changed.
          setState((s) => ({ ...fromSnapshot(snap), findRows: s.findRows, libraryStatus: snap.enabled && s.enabled ? s.libraryStatus : null }));
        }
        return snap;
      })
      .catch(() => null);
  }, [getApi]);

  const onEvent = useCallback(
    (ev) => {
      if (!hydratedRef.current) {
        bufferRef.current.push(ev);
        return;
      }
      setState((s) => applySyncEvent(s, ev));
      if (ev && ev.kind === "config") _seeVersion(ev.config);
      if (ev && ev.kind === "plans" && selRef.current[ev.targetId]) _selPut(ev.targetId, SQ.onView(selRef.current[ev.targetId], ev.selSeq));
      if (ev && ev.kind === "settings" && !!ev.enabled !== enabledRef.current) {
        enabledRef.current = !!ev.enabled;
        refresh();
      }
    },
    [refresh],
  );

  useEffect(() => {
    const a = getApi();
    if (!a) return undefined;
    let alive = true;
    hydratedRef.current = false;
    bufferRef.current = [];
    const unsubscribe = a.onSyncEvent(onEvent);
    // Adopt the snapshot (or the defaults when get-state refused or failed),
    // then drain what arrived meanwhile.
    const hydrate = (snap) => {
      if (!alive) return;
      const buffered = bufferRef.current;
      bufferRef.current = [];
      hydratedRef.current = true;
      let s = snap && snap.ok !== false ? fromSnapshot(snap) : { ...INITIAL, loaded: true };
      for (const ev of buffered) s = applySyncEvent(s, ev);
      enabledRef.current = s.enabled;
      _seeVersion(s.config);
      setState(() => s);
    };
    Promise.resolve()
      .then(() => a.syncGetState())
      .then(hydrate, () => hydrate(null));
    return () => {
      alive = false;
      if (typeof unsubscribe === "function") unsubscribe();
    };
  }, [onEvent, getApi]);

  // Library status (UI-pass U4-2): main computes it lazily and pushes it
  // only after a plan job or a library change, so a session that turns sync
  // on asks once. An event that lands before the answer is newer and wins.
  useEffect(() => {
    if (!state.enabled) return undefined;
    const a = getApi();
    if (!a || typeof a.syncLibraryStatus !== "function") return undefined;
    let alive = true;
    Promise.resolve(a.syncLibraryStatus({}))
      .then((r) => {
        if (!alive || !r || r.ok === false) return;
        const status = { asOf: r.asOf || null, bySeries: r.bySeries || {}, ...(r.error ? { error: r.error } : {}) };
        setState((s) => (s.libraryStatus ? s : { ...s, libraryStatus: status }));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [state.enabled, getApi]);

  const actions = useMemo(() => {
    const out = { refresh };
    for (const [name, fn] of Object.entries(ACTIONS)) {
      out[name] = (payload) => {
        const a = getApi();
        if (!a) return Promise.resolve({ ok: false, code: "disabled", message: "electronAPI unavailable" });
        return Promise.resolve(a[fn](payload));
      };
    }
    // find-sources:get answers the target's rows; keep them so find events
    // (which carry one row each) patch a complete table.
    /**
     * Queue a selection edit: {changes:[{opId,selected}], seriesKey?} or a
     * bulk request ({bulk, …} / {rebind}) with an optional seriesKey for the
     * row's pending mark. False when refused locally: no plan, or an
     * apply/prune/rename holds the target (the service would answer busy).
     */
    out.select = (targetId, req) => {
      const s = stateRef.current;
      const view = s.plans[targetId];
      if (!req || !view || view.gen == null || SQ.selectionLocked(s.job, targetId)) return false;
      const cur = selRef.current[targetId] || SQ.EMPTY;
      // seriesKey marks the row pending for both shapes; a bulk request also
      // SENDS it (bulk "series" needs it, bulk "group" is scoped by it).
      const { seriesKey, changes, ...rest } = req;
      _selPut(targetId, Array.isArray(changes) ? SQ.enqueueChanges(cur, changes, view.gen, seriesKey) : SQ.enqueueBulk(cur, seriesKey ? { ...rest, seriesKey } : rest, view.gen, seriesKey));
      clearTimeout(selTimers.current[targetId]);
      selTimers.current[targetId] = setTimeout(() => pumpSel(targetId), SQ.BATCH_MS);
      return true;
    };
    // CONFIG-OP CLIENT (header): serialized, newest version.
    const sendOp = out.configOp;
    out.configOp = (payload) => {
      const next = cfgChainRef.current.then(async () => {
        let r;
        try {
          r = await sendOp({ ...(payload || {}), expectVersion: cfgVersionRef.current });
        } catch (e) {
          r = { ok: false, code: "invalid", message: (e && e.message) || "the request failed" };
        }
        if (r && r.config) _seeVersion(r.config);
        return r || { ok: false, code: "invalid", message: "no answer" };
      });
      cfgChainRef.current = next.catch(() => {});
      return next;
    };
    out.clearSelectionError = (targetId) => {
      const cur = selRef.current[targetId];
      if (cur && cur.error) _selPut(targetId, { ...cur, error: null });
    };
    const getRows = out.findSourcesGet;
    out.findSourcesGet = (payload) =>
      getRows(payload).then((r) => {
        if (r && r.ok && payload && payload.targetId) {
          setState((s) => ({ ...s, findRows: { ...s.findRows, [payload.targetId]: rowsByFolder(r.rows) } }));
        }
        return r;
      });
    return out;
  }, [refresh, getApi, pumpSel]);

  useEffect(() => () => {
    for (const t of Object.values(selTimers.current)) clearTimeout(t);
  }, []);

  return { ...state, actions };
}

export default useDeviceSync;
