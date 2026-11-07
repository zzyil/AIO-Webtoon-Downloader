// ============================================================
// DEV ONLY — a fake Device Sync backend for the ?gallery=sync page
//
// makeSyncSource(caseSpec) builds the object useDeviceSync({source}) reads
// instead of window.electronAPI (hooks/useDeviceSync.js, DEV SEAM): the
// sync* wrappers plus onSyncEvent. Every answer comes from the fixtures that
// tools/_gen_sync_fixtures.js recorded off the REAL service, so shapes can't
// drift from main; regenerate them after a backend change.
//
// The one thing here that is NOT recorded is config-op: it applies a naive
// copy of the edit (patch merge, list push/splice, version bump) and pushes a
// `config` event, so editors in the gallery respond to clicks. It does no
// validation, no identity clearing and no re-plan. Never treat its behaviour
// as the service's.
//
// TAB MODE (spec.tab = a snapshot name from tab.json, phase U2): the plan-
// backed channels answer from the same recording. seriesDetail and review are
// the recorded replies; set-selection looks the request up among the recorded
// selection steps (seq and gen ignored) and pushes that step's recorded
// `plans` view, or re-pushes the current view unchanged when it wasn't
// recorded — so a tick on an unrecorded op shows its pending state and then
// snaps back. That snap-back is the fake's limit, not the service's.
// apply replays the recorded job events on their own clock, then pushes the
// recorded after-apply view. Nothing here re-plans. U4 adds library-status
// (recorded) and the find-sources channels (makeFindChannels below).
//
// Loaded only through src/dev/SyncGallery.jsx, which main.jsx imports
// dynamically under import.meta.env.DEV, so none of this reaches dist
// (tools/_check_sync_dist.js greps dist for "__aioSyncFixture" after a build).
// ============================================================

import stateOn from "./sync-fixtures/state-on.json";
import stateOff from "./sync-fixtures/state-off.json";
import devices from "./sync-fixtures/devices.json";
import locateAdb from "./sync-fixtures/locate-adb.json";
import browseRemote from "./sync-fixtures/browse-remote.json";
import labelPreview from "./sync-fixtures/label-preview.json";
import adbStates from "./sync-fixtures/adb-states.json";
import refusals from "./sync-fixtures/refusals.json";
import tab from "./sync-fixtures/tab.json";
import prompts from "./sync-fixtures/prompts.json";

const clone = (x) => (x == null ? x : JSON.parse(JSON.stringify(x)));
export const FIXTURES = {
  stateOn: stateOn.data,
  stateOff: stateOff.data,
  devices: devices.data,
  locateAdb: locateAdb.data,
  browseRemote: browseRemote.data,
  labelPreview: labelPreview.data,
  adbStates: adbStates.data,
  refusals: refusals.data,
  tab: tab.data,
  prompts: prompts.data,
};

/**
 * @param {{base?:"on"|"off", tab?:string, prompt?:string, adb?:string, mutate?:(snap)=>void, hold?:boolean}} spec
 *   base picks the recorded snapshot (tab, when set, wins: a tab.json
 *   snapshot name; prompt, a prompts.json recording's get-state at its card,
 *   wins over both); adb swaps in one recorded adb state; mutate edits the
 *   snapshot copy before the hook sees it; hold leaves every set-selection
 *   unanswered (the pending state); findRows(rows) edits a copy of the
 *   recorded find-sources rows (tab mode; the coverage cases).
 */
export function makeSyncSource(spec = {}) {
  const snap = clone(spec.prompt ? FIXTURES.prompts[spec.prompt].state : spec.tab ? FIXTURES.tab.snapshots[spec.tab] : spec.base === "off" ? FIXTURES.stateOff : FIXTURES.stateOn);
  if (spec.adb && FIXTURES.adbStates[spec.adb]) snap.adb = clone(FIXTURES.adbStates[spec.adb]);
  if (spec.mutate) spec.mutate(snap);
  const listeners = new Set();
  const emit = (ev) => setTimeout(() => listeners.forEach((fn) => fn(ev)), 0);
  const ok = (x) => Promise.resolve({ ok: true, ...x });

  const target = (id) => (snap.config.targets || []).find((t) => t.id === id);
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const listOp = (p, field, edit) => {
    const t = target(p.targetId);
    if (t) t[field] = edit((t[field] || []).slice());
  };
  function applyOp(p) {
    const cfg = snap.config;
    switch (p.op) {
      case "add-target": {
        const id = `tdev${Math.random().toString(16).slice(2, 9)}`;
        cfg.targets.push({ id, excludes: [], aliases: [], only: [], ignoredSuggestions: [], acknowledged: [], ...p.target });
        return id;
      }
      case "update-target":
        Object.assign(target(p.targetId) || {}, p.patch || {});
        return null;
      case "remove-target":
        cfg.targets = cfg.targets.filter((t) => t.id !== p.targetId);
        return null;
      case "set-adb-path":
        cfg.adbPath = p.path || null;
        return null;
      case "add-alias":
        listOp(p, "aliases", (l) => [...l, p.alias]);
        return null;
      case "add-exclude":
        listOp(p, "excludes", (l) => [...l, p.entry]);
        return null;
      case "remove-alias":
        listOp(p, "aliases", (l) => l.filter((x) => !same(x, p.entry)));
        return null;
      case "remove-exclude":
        listOp(p, "excludes", (l) => l.filter((x) => !same(x, p.entry)));
        return null;
      case "edit-list":
        listOp(p, p.field, (l) => ("remove" in p ? l.filter((x) => !same(x, p.remove)) : l.some((x) => same(x, p.add)) ? l : [...l, p.add]));
        return null;
      case "set-selection-mode":
        Object.assign(target(p.targetId) || {}, { selection: p.mode, ...(Array.isArray(p.only) ? { only: p.only } : {}) });
        return null;
      default:
        return null; // forget-record and the plan-backed ops: version bump only
    }
  }

  const tabApi = spec.tab ? makeTabChannels(snap, emit, !!spec.hold, spec.findRows) : {};
  return {
    syncGetState: () => Promise.resolve(clone(snap)),
    onSyncEvent: (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    syncConfigOp: (p) => {
      if (snap.config && p.expectVersion !== snap.config.version) {
        return Promise.resolve({ ok: false, code: "version-conflict", message: "the sync settings changed", config: clone(snap.config) });
      }
      const targetId = applyOp(p);
      snap.config.version += 1;
      emit({ kind: "config", config: clone(snap.config) });
      return ok({ config: clone(snap.config), ...(targetId ? { targetId } : {}) });
    },
    syncLocateAdb: () => Promise.resolve(clone(FIXTURES.locateAdb)),
    syncListDevices: () => Promise.resolve(clone(FIXTURES.devices)),
    // The connect card's answers (U3): the prompt closes the way main's
    // `answered` event closes it. Nothing else happens (no re-plan).
    syncPromptResponse: ({ promptId, action }) => {
      const p = (snap.prompts || []).find((x) => x.promptId === promptId) || (snap.pending || []).find((x) => x.promptId === promptId);
      if (!p) return Promise.resolve({ ok: false, code: "invalid", message: "that prompt is no longer open" });
      snap.prompts = (snap.prompts || []).filter((x) => x.promptId !== promptId);
      snap.pending = (snap.pending || []).filter((x) => x.promptId !== promptId);
      snap.prompt = snap.prompts[0] || null;
      emit({ kind: "prompt", action: "answered", prompt: clone(p), response: action, prompts: clone(snap.prompts), pending: clone(snap.pending) });
      return ok({});
    },
    syncBrowseRemote: ({ path }) => {
      const r = FIXTURES.browseRemote[path] || Object.values(FIXTURES.browseRemote).find((v) => v.path === path);
      return Promise.resolve(r ? clone(r) : { ok: false, code: "invalid", message: `no fixture for ${path}` });
    },
    syncLabelPreview: ({ name, patterns }) => {
      // Any line that won't compile answers the recorded refusal shape (its
      // text is the recorded one, not this pattern's).
      const bad = (patterns || "").split("\n").some((l) => {
        try {
          new RegExp(l.trim());
          return false;
        } catch {
          return true;
        }
      });
      if (bad) return Promise.resolve(clone(FIXTURES.labelPreview.badPattern));
      const e = FIXTURES.labelPreview.byName[name];
      return Promise.resolve(e ? clone(patterns ? e.custom : e.builtin) : { ok: true, unit: "unknown", label: null });
    },
    // Not used by Settings → Sync; answered so a stray call can't throw.
    ...Object.fromEntries(
      ["syncPlan", "syncSeriesDetail", "syncSetSelection", "syncVerify", "syncApply", "syncPrune", "syncRenameDeviceFolder", "syncCancel",
        "syncLibraryStatus", "syncExportReport", "syncDeviceCover", "syncFindSourcesStart", "syncFindSourcesCancel",
        "syncFindSourcesGet", "syncFindSourcesUpdateRow", "syncLink", "syncReview"].map((k) => [k, () => Promise.resolve({ ok: false, code: "disabled", message: "not in the gallery" })]),
    ),
    ...tabApi,
  };
}

const later = (ms, v) => new Promise((r) => setTimeout(() => r(v), ms));
// A selection request's identity: what it asks for, not when.
const selKey = ({ gen, seq, targetId, ...rest }) => JSON.stringify(rest);

/** The plan-backed channels for TAB MODE (header). Mutates `snap` as the job runs. */
function makeTabChannels(snap, emit, hold, findRows) {
  const T = FIXTURES.tab;
  const tid = () => Object.keys(snap.plans || {})[0] || T.targetId;
  const view = () => snap.plans[tid()];
  const push = (v) => {
    snap.plans[tid()] = v;
    emit({ kind: "plans", targetId: tid(), ...clone(v) });
  };
  const byKey = new Map(T.selection.filter((x) => x.view).map((x) => [selKey(x.request), x]));
  return {
    syncPlan: (p) => {
      if (p && p.ifNeeded) return Promise.resolve({ ok: true, planned: false, why: "fresh" });
      push({ ...view(), stale: true });
      return later(700).then(() => {
        push({ ...view(), stale: false });
        return { ok: true, planned: true };
      });
    },
    syncSeriesDetail: ({ seriesKey }) => later(120, T.details[seriesKey] ? clone(T.details[seriesKey]) : { ok: false, code: "invalid", message: `no fixture for ${seriesKey}` }),
    syncReview: () => {
      const v = view();
      const r = clone((v.selSeq || 0) >= 5 ? T.reviewWithLoss : T.review);
      return later(150, { ...r, gen: v.gen });
    },
    syncSetSelection: (p) => {
      if (hold) return new Promise(() => {});
      const v = view();
      if (p.gen !== v.gen) return later(150, clone(T.refusals.planChanged));
      const hit = byKey.get(selKey(p));
      return later(250).then(() => {
        const { kind, targetId, ...next } = hit ? clone(hit.view) : clone(v);
        push({ ...next, gen: v.gen, selSeq: p.seq });
        return hit ? { ...clone(hit.reply), seq: p.seq, gen: v.gen } : { ok: true, gen: v.gen, seq: p.seq, totals: v.summary && v.summary.totals };
      });
    },
    syncApply: (p) => {
      const v = view();
      if (p.gen !== v.gen) return Promise.resolve(clone(T.refusals.planChanged));
      for (const { t, ev } of T.applyEvents) setTimeout(() => emit(clone(ev)), t);
      const end = T.applyEvents.length ? T.applyEvents[T.applyEvents.length - 1].t : 0;
      setTimeout(() => push(clone(T.snapshots.afterApply.plans[T.targetId])), end + 60);
      return Promise.resolve({ ok: true, runId: 7 });
    },
    syncCancel: () => Promise.resolve({ ok: true }),
    syncVerify: () => Promise.resolve({ ok: true, runId: 8 }),
    syncPrune: () => later(300, { ok: true, runId: 9 }),
    syncRenameDeviceFolder: () => later(300, { ok: true, runId: 10 }),
    syncLink: () => later(200, { ok: true }),
    syncExportReport: () => later(200, { ok: true, path: "D:/Sync-fixture/report.txt" }),
    syncDeviceCover: () => Promise.resolve({ ok: false, code: "invalid", message: "no covers in the gallery" }),
    syncLibraryStatus: () => Promise.resolve(clone(T.libraryStatus)),
    ...makeFindChannels(snap, emit, findRows),
  };
}

/**
 * find-sources (U4) over the recorded rows (tab.json `find`, whose search
 * results are synthetic: see the generator). get answers the recording;
 * update-row patches a copy the naive way (a new query re-queues the row);
 * start replays the recorded run: each searched row turns `searching`, then
 * its recorded result. Nothing here searches. `edit` (spec.findRows) rewrites
 * a copy of the recorded rows first; a replayed search still lands the
 * recorded result.
 */
function makeFindChannels(snap, emit, edit) {
  const T = FIXTURES.tab;
  const start = clone(T.find.rows);
  const rows = new Map((edit ? edit(start) || start : start).map((r) => [r.folder, r]));
  const tid = () => Object.keys(snap.plans || {})[0] || T.targetId;
  const rowEv = (row, extra) => emit({ kind: "find", targetId: tid(), state: "idle", row: clone(row), ...(extra || {}) });
  return {
    syncFindSourcesGet: () => later(80, { ok: true, running: false, targetId: tid(), waiting: null, done: 0, total: 0, rows: [...rows.values()].map(clone) }),
    syncFindSourcesUpdateRow: ({ folder, patch }) => {
      const row = rows.get(folder);
      if (!row) return Promise.resolve({ ok: false, code: "invalid", message: "unknown row" });
      if (patch.query != null && patch.query !== row.query) Object.assign(row, { query: patch.query, state: "queued", candidates: [], pickUrl: null, multiSource: null, error: null });
      // The runner's ROW_PATCH_KEYS (electron/sync/find-sources.js).
      for (const k of ["pickUrl", "pinnedUrl", "skip", "include"]) if (k in patch) row[k] = patch[k];
      rowEv(row);
      return later(60, { ok: true, row: clone(row) });
    },
    syncFindSourcesStart: ({ folders }) => {
      const want = Array.isArray(folders) ? new Set(folders) : null;
      const todo = [...rows.values()].filter((r) => (!want || want.has(r.folder)) && !r.skip && r.state !== "done");
      const recorded = new Map(T.find.rows.map((r) => [r.folder, r]));
      todo.forEach((r, i) => {
        setTimeout(() => {
          r.state = "searching";
          emit({ kind: "find", targetId: tid(), state: "running", done: i, total: todo.length, row: clone(r) });
        }, 400 * i + 50);
        setTimeout(() => {
          const rec = recorded.get(r.folder) || {};
          Object.assign(r, { state: rec.state === "error" ? "error" : "done", candidates: clone(rec.candidates || []), pickUrl: rec.pickUrl || null, multiSource: typeof rec.multiSource === "boolean" ? rec.multiSource : null, error: rec.error || null });
          emit({ kind: "find", targetId: tid(), state: "running", done: i + 1, total: todo.length, row: clone(r) });
        }, 400 * i + 400);
      });
      setTimeout(() => emit({ kind: "find", targetId: tid(), state: "done", done: todo.length, total: todo.length }), 400 * todo.length + 450);
      return later(30, { ok: true, started: todo.length > 0 });
    },
    syncFindSourcesCancel: () => Promise.resolve({ ok: true, wasRunning: false }),
  };
}
