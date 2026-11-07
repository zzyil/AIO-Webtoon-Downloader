// ============================================================
// DEVICE SYNC — PLAN PREPARATION (main process)
//
// Owns everything between "plan this target" and a plan the user can
// review: the device I/O and record repairs the pure planner can't do
// itself (P3 decision: its own module; service.js only calls it). One call,
// in the plan's fixed order ("Provenance": rename intents, then gone
// detection, then pending resolution, coverage Verify and the zero-files
// sanity check; each later step skips gone folders):
//    1. probe the target (case, caps, canonical root, volume id)
//    2. load the record; walk the library; refuse on input refusals
//       (library-missing, record-mismatch) BEFORE any device or record write
//    3. record upkeep: the measured case flag into the header; GC of stale
//       shards, only against a valid header (a missing or corrupt header
//       makes every shard read as stale, and GC would wipe the record)
//    4. list the root; refuse when it is empty while the record manages
//       folders (a locked or half-mounted storage lists as empty)
//    5. folder rename intents, from the root listing's exact names
//    6. gone detection: planner.goneCandidates → STA2 of every name (all
//       ENOENT) → on adb, the rc-checked `find -type d` must list the same
//       directories as LIS2; anything else refuses device-listing-suspect
//       (every such refusal names its `cause`: root-unlistable, empty-root,
//       missed-folder, stat-failed, listings-differ; the UI words each,
//       copy.js listingSuspectCopy — `detail` stays the raw error)
//       → every device refusal is decided: the `device-ready` progress line
//         (also sent by a refusal at steps 2-6; grep deviceReady)
//       → free space, read once for the capacity gauge (grep _readFreeSpace)
//    7. list every root folder (FolderTransport sweeps its dot-temps first);
//       read details.json / .aio_series.json for adoption
//    8. file rename intents, then `pending` entries (listing, then one RECV)
//    9. hash the PC files the target mirrors (hash-pool.js cache), last, so
//       a refused plan never pays for it
//   10. buildPlan; STA2 the names it asks about (needsProbe); again
//   11. coverage Verify of plan.needsVerify (executor.verify), reload, plan
//   12. persist the final plan's entryFixes / shardNameFixes
//
// Also owns re-planning from a prepare's result after a review decision:
// derivePlan (no I/O) and refreshPlan (only the device facts the decision
// newly needs); see the "cached facts" section below.
//
// ERRORS: a refusal comes back as {ok:false, code} (contract.js). A device
// fault, a record-write failure or a cancel throws (AdbError, StoreError or
// executor RunStop); service.js maps it with executor.policyFor. Device
// calls wait out `connecting` like the executor (withConnectWait).
//
// Read by: service.js (P4). Depends on: transports.js (through the
// interface), store.js, hash-pool.js, pc-inventory.js, planner.js,
// provenance.js, executor.js (Verify, the connect wait), profiles.js,
// naming.js, contract.js.
// ============================================================

const wire = require("./adb/wire");
const { refuse } = require("./contract");
const { slotKey } = require("./naming");
const { isMirrored } = require("./profiles");
const { loadRecordView, resolvePending, resolveRenameIntent } = require("./provenance");
const planner = require("./planner");
const { walkLibrary } = require("./pc-inventory");
const { Executor, RunStop, withConnectWait, CONNECT_WAIT_MS } = require("./executor");
const { segmentProblem } = require("./transports");

const ENOENT = wire.ERRNO.ENOENT;
// Device-side series metadata read for adoption (parent rule 2: ≤256 KB).
const META_MAX_BYTES = 256 * 1024;
const DEVICE_META = Object.freeze({ details: "details.json", aio: ".aio_series.json" });
// buildPlan asks for name probes; a gone series' verbatim fallback is only
// asked for once its earlier candidates were probed, so a few passes.
const PROBE_PASSES = 4;

/**
 * Prepare one target's plan.
 * @param {object} c
 * @param {object} c.transport        AdbTransport | FolderTransport
 * @param {import('./store').RecordStore} c.store
 * @param {import('./hash-pool').HashPool} c.hashPool
 * @param {object} c.target           resolveTargetConfig() result
 * @param {object} c.settings         resolveSyncSettings() result
 * @param {string} c.libraryRoot      getConfiguredOutputRoot()
 * @param {object} [c.opts]
 * @param {'read-back'|'adopt-size'} [c.opts.verifyMode]  for a device without sha256sum
 * @param {'cached'|'rehash'} [c.opts.hashMode]           default settings.syncHashMode
 * @param {AbortSignal} [c.signal]
 * @param {(ev:object)=>void} [c.onProgress]  {phase:'probe'|'hashing'|'listing'|'verify'|'planning', ...}
 * @param {number|string} [c.runId]   the job's id, stamped on the coverage Verify's run log
 *                                    (targets/<id>/logs) so it matches service.log's job lines
 * @param {() => number} [c.now]
 * @param {Function} [c.walk]         walkLibrary, injectable
 * @param {object} [c.executorDeps]   extra Executor deps for the coverage Verify (tests: hooks)
 * @param {number} [c.connectWaitMs]
 * @returns {Promise<object>} {ok:true, plan, device, pc, view, probe, target,
 *   verify, warnings, planOpts, freeSpace:{bytes|null, asOf}} or a refusal
 */
async function preparePlan(c) {
  const transport = c.transport;
  const store = c.store;
  const signal = c.signal || null;
  const now = c.now || Date.now;
  const opts = c.opts || {};
  const settings = c.settings || {};
  const progress = c.onProgress || (() => {});
  const warnings = [];
  const waitOpts = { timeoutMs: c.connectWaitMs != null ? c.connectWaitMs : CONNECT_WAIT_MS, signal };
  const dev = (fn) => withConnectWait(transport, fn, waitOpts);
  const checkCancel = () => {
    if (signal && signal.aborted) throw new RunStop("cancelled", "cancelled");
  };

  // 1. probe
  progress({ phase: "probe" });
  const probe = await dev(() => transport.probe());
  checkCancel();
  // A folder target's identity carries the volume measured now, so a
  // different disk at the same drive letter is a record-mismatch.
  const target = c.target.kind === "folder" && probe.volumeId != null ? { ...c.target, volumeId: String(probe.volumeId) } : c.target;

  // 2. record, library, input refusals
  const loaded = await store.load();
  for (const x of loaded.corrupt) warnings.push({ kind: "shard-corrupt", shardId: x.shardId, error: x.error });
  if (loaded.headerState === "corrupt") warnings.push({ kind: "header-corrupt" });
  const pc = await (c.walk || walkLibrary)(c.libraryRoot);
  checkCancel();
  const measured = { canonicalRoot: probe.canonicalRoot || null };
  // Unmeasured case (nothing to flip): a stored flag wins; with no record
  // the probe's assumed case-insensitive stands.
  if (probe.caseMeasured || !loaded.header) measured.caseInsensitive = !!probe.caseInsensitive;
  const viewOf = (l) => loadRecordView({ header: l.header, shards: l.shards, selection: l.selection, target, measured });
  let view = viewOf(loaded);

  // DEVICE-READY (deviation 14, user decision 2026-10-10): one progress line
  // as soon as every device-side refusal is decided (end of step 6), so the
  // UI can act before the listings and the PC hashing finish. A refusal at
  // steps 2-6 sends it too, carrying that refusal; the job still ends
  // `failed` with summary.refusal (service.js _refused). `mayNeedMode` is a
  // conservative hint, not the planner's needsVerify: the device can't hash
  // and has folders a coverage Verify might need, so the UI can ask
  // read-back vs adopt-size up front. service.js forwards only the first one
  // per job (grep preflightSent).
  let readySent = false;
  const deviceReady = (refusal, dirCount) => {
    if (readySent) return;
    readySent = true;
    const canHash = transport.kind === "folder" || !!(probe.caps && probe.caps.sha256sum);
    progress({
      phase: "device-ready",
      preflight: {
        kind: transport.kind,
        caps: { ...(probe.caps || {}) },
        canonicalRoot: probe.canonicalRoot || null,
        caseInsensitive: !!view.caseInsensitive,
        caseMeasured: !!probe.caseMeasured,
        canHash,
        mayNeedMode: !canHash && !opts.verifyMode && dirCount > 0,
        refusal: refusal ? { ...refusal } : null,
      },
    });
  };
  const refused = (r) => {
    deviceReady(r, 0);
    return { ...r, warnings };
  };

  const early = planner.inputRefusal(pc, view);
  if (early) return refused(early);
  if (pc.warnings && pc.warnings.length) warnings.push(...pc.warnings);

  // 3. record upkeep (only against a valid header)
  if (view.epoch) {
    const h = loaded.header;
    if (probe.caseMeasured && (h.caseInsensitive !== !!probe.caseInsensitive || !h.caseMeasured)) {
      await store.updateHeader({ caseInsensitive: !!probe.caseInsensitive, caseMeasured: true });
    }
    // A header from forget-record or rule 12's clear (service.js) pins the
    // identity only: the first probe after it fills the measured fields.
    if (!h.canonicalRoot && probe.canonicalRoot) {
      await store.updateHeader({ canonicalRoot: probe.canonicalRoot, caps: probe.caps || {} });
    }
    if (view.staleShardIds.length) await store.gc(view.staleShardIds);
  }

  // 4. root listing
  progress({ phase: "listing", done: 0, total: null });
  let root = await dev(() => transport.listRoot());
  if (!root.ok) return refused(refuse("device-listing-suspect", "the target folder can't be listed", { detail: root.error, cause: "root-unlistable" }));
  if (root.undecodable.length) warnings.push({ kind: "undecodable-names", folder: null, names: root.undecodable.map((u) => u.display) });
  const visibleDirs = (l) => l.dirs.filter((d) => !d.name.startsWith("."));
  if (view.shards.length && !visibleDirs(root).length) {
    return refused(refuse("device-listing-suspect", "the device lists no folders while this target has synced series (locked or not mounted?)", { detail: "empty-root", cause: "empty-root" }));
  }

  // 5. folder rename intents
  if (await _folderIntents(view, root, { dev, transport, store, warnings })) {
    root = await dev(() => transport.listRoot());
    if (!root.ok) return refused(refuse("device-listing-suspect", "the target folder can't be listed", { detail: root.error, cause: "root-unlistable" }));
  }
  checkCancel();

  // 6. gone detection. Every candidate's answer is kept (goneAnswers) so a
  // derived plan (derivePlan) needs a device stat only for a new candidate.
  const gone = new Set();
  const goneAnswers = new Map();
  const cands = planner.goneCandidates({ view, pc, target, device: { dirs: root.dirs } });
  for (const cand of cands) {
    let absent = true;
    for (const name of cand.names) {
      if (segmentProblem(name)) {
        absent = false;
        break;
      }
      const st = await dev(() => transport.stat(name));
      if (st.exists && st.type === "dir") {
        return refused(refuse("device-listing-suspect", "a folder the device listing left out exists", { detail: "listing-missed-folder", cause: "missed-folder", folder: name }));
      }
      if (st.exists) {
        absent = false; // something else holds the name: not gone, held
        break;
      }
      if (st.error !== ENOENT) {
        return refused(refuse("device-listing-suspect", "a folder's state can't be read", { detail: `stat-errno-${st.error}`, cause: "stat-failed", folder: name }));
      }
    }
    goneAnswers.set(_candKey(cand), absent);
    if (absent) gone.add(cand.shardId);
  }
  let rootCrossChecked = false;
  if (gone.size && transport.kind === "adb") {
    // A readdir error ends LIS2 early without an error (quiet failures): a
    // second, rc-checked listing must agree before anything reads as gone.
    let alt;
    try {
      alt = await dev(() => transport.listRootDirsAlt());
    } catch (e) {
      if (!(e && e.kind === "remote-fail")) throw e;
      alt = { ok: false, error: "find-failed" };
    }
    if (!alt.ok || !_sameKeys(alt.keys, root.dirKeys)) {
      return refused(refuse("device-listing-suspect", "two listings of the device disagree", { detail: alt.ok ? "listings-differ" : alt.error, cause: "listings-differ" }));
    }
    rootCrossChecked = true;
  }

  deviceReady(null, visibleDirs(root).length);

  // Free space, read once per prepare for the Sync tab's capacity gauge
  // (UI pass, user decision 2). service.js recomputes `needed` on every
  // selection change against this one reading; the executor still measures
  // again at apply time and is the one that refuses. Unknown is null, never 0.
  const freeSpace = await _readFreeSpace(transport, { dev, signal, now, warnings });

  // 7. folder listings and device metadata
  const device = { trusted: true, dirs: root.dirs, rootFiles: root.files, folders: new Map(), meta: new Map() };
  const ci = view.caseInsensitive;
  const boundSlots = new Set(view.shards.map((sh) => slotKey(sh.name, ci)));
  const dirs = visibleDirs(root);
  for (let i = 0; i < dirs.length; i += 1) {
    checkCancel();
    const name = dirs[i].name;
    progress({ phase: "listing", done: i, total: dirs.length, folder: name });
    if (typeof transport.sweepLeftovers === "function") await transport.sweepLeftovers(name);
    const l = await dev(() => transport.listFolder(name));
    device.folders.set(name, l.ok ? { ok: true, files: l.files } : { ok: false, files: [], error: l.error });
    if (!l.ok) continue;
    if (l.undecodable.length) warnings.push({ kind: "undecodable-names", folder: name, names: l.undecodable.map((u) => u.display) });
    const meta = await _readMeta(name, l, { dev, transport, bound: boundSlots.has(slotKey(name, ci)) });
    if (meta) device.meta.set(name, meta);
  }
  progress({ phase: "listing", done: dirs.length, total: dirs.length });

  // 8. file rename intents, then pending entries (gone folders aren't listed)
  for (const sh of view.shards) {
    checkCancel();
    const listed = device.folders.get(sh.name);
    if (!listed || !listed.ok) continue;
    let changed = false;
    if (sh.renameIntent && sh.renameIntent.scope === "file") {
      const r = await _fileIntent(sh, listed, { dev, transport, warnings, ci });
      changed = true;
      if (r.moved) {
        const l = await dev(() => transport.listFolder(sh.name));
        device.folders.set(sh.name, l.ok ? { ok: true, files: l.files } : { ok: false, files: [], error: l.error });
        if (!l.ok) {
          await store.writeShard(sh);
          continue;
        }
      }
    }
    if (await _resolvePendingIn(sh, device.folders.get(sh.name), { dev, transport, warnings, ci, signal })) changed = true;
    if (changed) await store.writeShard(sh);
  }

  // 9. PC hashes: last, so a device that refuses never costs a first-time
  // hash of the whole library
  const hashAttempted = await _hashPc(c.hashPool, pc, target, {
    mode: opts.hashMode || settings.syncHashMode || "cached",
    signal,
    progress,
    warnings,
  });
  checkCancel();

  // 10-11. plan, name probes, coverage Verify, plan again
  const nameProbe = new Map();
  const planOpts = { now: now(), gone, nameProbe };
  progress({ phase: "planning" });
  let plan = await _planWithProbes({ target, settings, pc, device, view, opts: planOpts }, { dev, transport });
  if (plan.ok === false) return { ...plan, warnings };

  // The facts this prepare measured, kept on its result so derivePlan and
  // refreshPlan can re-plan without repeating them (see "CACHED FACTS" below).
  const facts = () => ({
    ok: true,
    plan,
    device,
    pc,
    view,
    probe,
    target,
    verify,
    warnings,
    planOpts,
    freeSpace,
    measured,
    goneAnswers,
    rootDirKeys: root.dirKeys,
    rootCrossChecked,
    hashAttempted,
    verifyAttempted,
  });
  const verifyAttempted = new Set();
  let verify = null;
  if (plan.needsVerify.length) {
    const mode = verifyModeFor(target.kind, probe, opts.verifyMode);
    if (!mode) {
      // The facts ride along (`prep`, never sent to the renderer: service.js
      // strips it, grep stripFacts) so choosing a mode needs a verify-only
      // refresh, not a second full prepare.
      return { ..._needsModeRefusal(plan.needsVerify.map((v) => v.folder), plan, device), warnings, prep: facts() };
    }
    const pcByKey = new Map(pc.series.map((s) => [planner.pcSeriesKey(s.folder), s]));
    const ex = new Executor({
      transport,
      store,
      hashPool: c.hashPool,
      target,
      settings,
      probe,
      view,
      signal,
      now,
      connectWaitMs: waitOpts.timeoutMs,
      onProgress: (ev) => progress({ ...ev, phase: "verify" }),
      ...(c.executorDeps || {}),
    });
    verify = await ex.verify({
      runId: c.runId != null ? String(c.runId) : undefined,
      mode,
      folders: plan.needsVerify.map((v) => {
        const s = pcByKey.get(v.seriesKey);
        return {
          folder: v.folder,
          shardId: v.shardId,
          binding: v.binding,
          pcFiles: s ? s.files.filter((f) => f.sha256 && isMirrored(f.name, target)) : [],
          bytes: planner.verifyBytesOf(device, [v.folder]),
        };
      }),
    });
    if (verify.status !== "completed") throw verify.cause || new RunStop(verify.status, verify.reason);
    for (const v of plan.needsVerify) verifyAttempted.add(_verifyKey(v));
    for (const row of verify.folders) {
      if (row.outcome !== "verified") warnings.push({ kind: "verify-failed", folder: row.folder, reason: row.reason });
    }
    // The verify merged its entries against a fresh listing: plan against it.
    for (const [name, files] of ex.verifyListings) device.folders.set(name, { ok: true, files });
    view = viewOf(await store.load());
    plan = await _planWithProbes({ target, settings, pc, device, view, opts: planOpts }, { dev, transport });
    if (plan.ok === false) return { ...plan, warnings, verify };
  }

  // 12. what the plan read off the listing becomes the record's
  await _persistFixes(plan, view, store);
  if (c.hashPool) await c.hashPool.flush().catch(() => {});
  // planOpts and the other facts: service.js re-plans from this cached input
  // after a decision (derivePlan / refreshPlan), without repeating the I/O.
  return facts();
}

// ------------------------------------------------------------ cached facts
//
// CACHED FACTS (plan sync-temp/plans/device-sync-cheap-replans.md). A
// prepare's result doubles as a cache of what it measured: the probe, the
// library walk with its hashes, the root and folder listings with their
// metadata, gone answers (goneAnswers, keyed by _candKey), name probes
// (planOpts.nameProbe), and which PC files and device folders it already
// tried to hash or verify (hashAttempted, verifyAttempted: a failure is not
// retried until the next full prepare, so a decision never loops on it).
//
// A review decision (a config op, a Link) changes only the target config or
// the record, i.e. buildPlan's inputs, so the plan is re-derived from the
// cache (derivePlan, no I/O). A fact the decision newly needs (a gone
// candidate or a name never stat'd, a file newly mirrored, a folder newly
// bound) is an OBLIGATION; refreshPlan fulfils exactly those on the device
// and the hash pool, inside a lane job. Anything that contradicts the cache
// (a folder the cached root listing left out) escalates to a full prepare:
// {ok:false, escalate:<cause>}, never a refusal, because the cache is what
// is wrong, not the device.
//
// Safety does not rest on the cache: apply always runs a fresh prepare and
// planner.reconcileForApply (service.js _startApply).

/** The resolved target with the probed volume id (prepare step 1's rule). */
function _withVolume(target, probe) {
  return target.kind === "folder" && probe && probe.volumeId != null ? { ...target, volumeId: String(probe.volumeId) } : target;
}

/** A gone candidate's cache key: the shard plus every name it was stat'd under. */
function _candKey(cand) {
  return `${cand.shardId}\u0000${cand.names.join("\u0000")}`;
}

/** A coverage Verify's cache key: the folder AND the shard (a Link binds a new shard to a folder). */
function _verifyKey(v) {
  return `${v.folder}\u0000${v.shardId || ""}`;
}

/**
 * The coverage Verify mode a prepare or refresh uses: device-hash when the
 * device can hash (a folder target, adb with sha256sum), else the target's
 * needs-mode answer; null when there is none (a needs-mode refusal).
 */
function verifyModeFor(kind, probe, verifyMode) {
  if (kind === "folder" || (probe && probe.caps && probe.caps.sha256sum)) return "device-hash";
  return verifyMode === "read-back" || verifyMode === "adopt-size" || verifyMode === "device-hash" ? verifyMode : null;
}

function _needsModeRefusal(folders, plan, device) {
  return refuse("needs-mode", "this device can't hash files: choose how to verify", {
    folders,
    modes: ["read-back", "adopt-size"],
    // UI-pass U3-4: the connect card's "Set up (reads ~N GB)" and the
    // Sync tab's chooser read these off the refusal.
    bytes: planner.verifyBytesOf(device, folders),
    firstSync: !!plan.firstSync,
  });
}

/**
 * Re-plan from a prepare's cached facts. No device, disk or hash I/O.
 *
 * @param {object} c
 * @param {object} c.prep        a prepare (or an earlier derive/refresh) result
 * @param {object} c.target      the CURRENT resolved target config
 * @param {object} c.settings    the current resolveSyncSettings() result
 * @param {object} [c.view]      a freshly loaded record view; default the cached one
 *                               (a config-only decision leaves the record alone)
 * @param {string} [c.verifyMode] the target's needs-mode answer (or a one-shot)
 * @param {() => number} [c.now]
 * @returns {object} {ok:true, prep, obligations:{gone, names, hashes, verify}, pending}
 *   where `prep` is a new prep object sharing the cached facts, and
 *   `pending` is true when a stat or hash obligation makes the plan
 *   unshowable (a gone series would read as folder-unreadable; unhashed files
 *   plan as unknown). Verify obligations alone leave an honest interim plan
 *   (planner state needs-verify, no pre-selected deletes). A refusal is
 *   returned as is; a needs-mode refusal carries `prep` like prepare's.
 */
function derivePlan(c) {
  const prep = c.prep;
  const now = c.now || Date.now;
  const target = _withVolume(c.target, prep.probe);
  const view = c.view || prep.view;
  const ci = !!view.caseInsensitive;
  const obligations = { gone: [], names: [], hashes: [], verify: [] };

  const gone = new Set();
  for (const cand of planner.goneCandidates({ view, pc: prep.pc, target, device: { dirs: prep.device.dirs } })) {
    // prepare step 6: a name that can't be a path segment is never gone
    if (cand.names.some((n) => segmentProblem(n))) continue;
    const absent = prep.goneAnswers.get(_candKey(cand));
    if (absent === undefined) obligations.gone.push(cand);
    else if (absent) gone.add(cand.shardId);
  }

  for (const s of prep.pc.series) {
    if (s.imageOnly || !planner.isIncluded(s, target)) continue;
    for (const f of s.files) if (!f.sha256 && isMirrored(f.name, target) && !prep.hashAttempted.has(f.path)) obligations.hashes.push(f);
  }

  // prepare reads .aio_series.json only for folders unbound at the time
  // (_readMeta); a folder bound since must plan as a fresh prepare sees it.
  const bound = new Set(view.shards.map((sh) => slotKey(sh.name, ci)));
  let meta = prep.device.meta;
  if (meta instanceof Map && [...meta].some(([name, m]) => m && m.aio && bound.has(slotKey(name, ci)))) {
    meta = new Map([...meta].map(([name, m]) => [name, m && m.aio && bound.has(slotKey(name, ci)) ? _withoutAio(m) : m]).filter(([, m]) => m));
  }

  const planOpts = { now: now(), gone, nameProbe: new Map(prep.planOpts.nameProbe) };
  const plan = planner.buildPlan({ target, settings: c.settings, pc: prep.pc, device: { ...prep.device, meta }, view, opts: planOpts });
  if (plan.ok === false) return plan;
  obligations.names = plan.needsProbe.filter((n) => !planOpts.nameProbe.has(n));
  obligations.verify = plan.needsVerify.filter((v) => !prep.verifyAttempted.has(_verifyKey(v)));

  const next = { ...prep, plan, view, target, planOpts };
  const pending = !!(obligations.gone.length || obligations.names.length || obligations.hashes.length);
  // A needs-mode answer is decidable here (the probe's caps are cached), so
  // a Link on a device that can't hash refuses at once, before any interim plan.
  if (!pending && obligations.verify.length && !verifyModeFor(target.kind, prep.probe, c.verifyMode)) {
    return { ..._needsModeRefusal(obligations.verify.map((v) => v.folder), plan, prep.device), prep: next };
  }
  return { ok: true, prep: next, obligations, pending };
}

function _withoutAio(m) {
  const { aio: _omit, ...rest } = m;
  return Object.keys(rest).length ? rest : null;
}

/**
 * Fulfil a derived plan's obligations, then derive again, until none is left
 * (bounded: PROBE_PASSES + 1). Runs inside a lane job; touches only what the
 * obligations name. Never re-lists the root or an unaffected folder.
 *
 * @param {object} c
 * @param {object} c.prep         the cached prepare result (not mutated: its
 *                                containers are copied; a PC file's sha256 is
 *                                set in place, which is true whatever follows)
 * @param {object} c.transport    an unprobed job transport: seeded from the
 *                                cached probe (adoptProbe), since probe()
 *                                lists the root
 * @param {import('./store').RecordStore} c.store
 * @param {import('./hash-pool').HashPool} [c.hashPool]
 * @param {object} c.target       the CURRENT resolved target config
 * @param {object} c.settings
 * @param {boolean} [c.reloadView] the record changed since the cached view (a Link, a Verify)
 * @param {object} [c.opts]       {verifyMode, hashMode}
 * @param {AbortSignal} [c.signal]
 * @param {(ev:object)=>void} [c.onProgress]
 * @param {number|string} [c.runId]
 * @param {() => number} [c.now]
 * @param {object} [c.executorDeps]
 * @param {number} [c.connectWaitMs]
 * @returns {Promise<object>} {ok:true, prep, verify, warnings, viewReloaded}
 *   (viewReloaded: prep.view was read from the store in this call) | a refusal
 *   (needs-mode carries `prep`) | {ok:false, escalate, folder?}: plan afresh
 */
async function refreshPlan(c) {
  const transport = c.transport;
  const signal = c.signal || null;
  const now = c.now || Date.now;
  const opts = c.opts || {};
  const settings = c.settings || {};
  const progress = c.onProgress || (() => {});
  const warnings = [];
  const waitOpts = { timeoutMs: c.connectWaitMs != null ? c.connectWaitMs : CONNECT_WAIT_MS, signal };
  const dev = (fn) => withConnectWait(transport, fn, waitOpts);
  const checkCancel = () => {
    if (signal && signal.aborted) throw new RunStop("cancelled", "cancelled");
  };
  const escalate = (cause, folder) => ({ ok: false, escalate: cause, folder: folder || null, warnings });

  if (typeof transport.adoptProbe === "function") transport.adoptProbe(c.prep.probe);
  let prep = {
    ...c.prep,
    device: { ...c.prep.device, folders: new Map(c.prep.device.folders) },
    planOpts: { ...c.prep.planOpts, nameProbe: new Map(c.prep.planOpts.nameProbe) },
    goneAnswers: new Map(c.prep.goneAnswers),
    hashAttempted: new Set(c.prep.hashAttempted),
    verifyAttempted: new Set(c.prep.verifyAttempted),
    warnings: c.prep.warnings || [],
  };
  const viewOf = (l) => loadRecordView({ header: l.header, shards: l.shards, selection: l.selection, target: _withVolume(c.target, prep.probe), measured: prep.measured });
  let view = c.reloadView ? viewOf(await c.store.load()) : prep.view;
  let viewReloaded = !!c.reloadView;
  let verify = null;

  progress({ phase: "planning" });
  for (let pass = 0; ; pass += 1) {
    checkCancel();
    const d = derivePlan({ prep, view, target: c.target, settings, verifyMode: opts.verifyMode, now });
    if (d.ok === false) return { ...d, warnings };
    prep = d.prep;
    const ob = d.obligations;
    const left = ob.gone.length + ob.names.length + ob.hashes.length + ob.verify.length;
    if (!left) break;
    // Each pass answers everything asked; only a gone series' later name
    // candidates (buildPlan asks them once the earlier ones are answered)
    // need another. More passes than prepare's own means the cache is off.
    if (pass > PROBE_PASSES) return escalate("refresh-unsettled");

    let newlyGone = false;
    for (const cand of ob.gone) {
      let absent = true;
      for (const name of cand.names) {
        const st = await dev(() => transport.stat(name));
        // The cached root listing doesn't have it: the cache is stale.
        if (st.exists && st.type === "dir") return escalate("missed-folder", name);
        if (st.exists) {
          absent = false;
          break;
        }
        if (st.error !== ENOENT) return escalate("stat-failed", name);
      }
      prep.goneAnswers.set(_candKey(cand), absent);
      if (absent) newlyGone = true;
    }
    if (newlyGone && transport.kind === "adb" && !prep.rootCrossChecked) {
      // prepare step 6's rc-checked second listing, against the cached root:
      // a quiet LIS2 failure must not make a folder read as gone.
      let alt;
      try {
        alt = await dev(() => transport.listRootDirsAlt());
      } catch (e) {
        if (!(e && e.kind === "remote-fail")) throw e;
        alt = { ok: false };
      }
      if (!alt.ok || !_sameKeys(alt.keys, prep.rootDirKeys)) return escalate("listings-differ");
      prep.rootCrossChecked = true;
    }

    for (const n of ob.names) {
      if (segmentProblem(n)) {
        prep.planOpts.nameProbe.set(n, false);
        continue;
      }
      const st = await dev(() => transport.stat(n));
      if (st.exists && st.type === "dir") return escalate("missed-folder", n);
      prep.planOpts.nameProbe.set(n, !st.exists && st.error === ENOENT);
    }

    if (ob.hashes.length) {
      // prune:false: a subset must not prune the cache's other entries.
      await _hashFiles(c.hashPool, prep.pc, ob.hashes, { mode: opts.hashMode || settings.syncHashMode || "cached", signal, progress, warnings, prune: false });
      for (const f of ob.hashes) prep.hashAttempted.add(f.path);
    }
    checkCancel();

    if (ob.verify.length) {
      // derivePlan refused needs-mode already when there is no mode.
      const mode = verifyModeFor(prep.target.kind, prep.probe, opts.verifyMode);
      // The executor writes shards under store.epoch and mints a new header
      // when it reads none: a store this job hasn't loaded yet would start a
      // new record epoch and orphan every shard. Load it first.
      if (!c.store.epoch) {
        view = viewOf(await c.store.load());
        viewReloaded = true;
      }
      const pcByKey = new Map(prep.pc.series.map((s) => [planner.pcSeriesKey(s.folder), s]));
      const ex = new Executor({
        transport,
        store: c.store,
        hashPool: c.hashPool,
        target: prep.target,
        settings,
        probe: prep.probe,
        view,
        signal,
        now,
        connectWaitMs: waitOpts.timeoutMs,
        onProgress: (ev) => progress({ ...ev, phase: "verify" }),
        ...(c.executorDeps || {}),
      });
      verify = await ex.verify({
        runId: c.runId != null ? String(c.runId) : undefined,
        mode,
        folders: ob.verify.map((v) => {
          const s = pcByKey.get(v.seriesKey);
          return {
            folder: v.folder,
            shardId: v.shardId,
            binding: v.binding,
            pcFiles: s ? s.files.filter((f) => f.sha256 && isMirrored(f.name, prep.target)) : [],
            bytes: planner.verifyBytesOf(prep.device, [v.folder]),
          };
        }),
      });
      if (verify.status !== "completed") throw verify.cause || new RunStop(verify.status, verify.reason);
      for (const v of ob.verify) prep.verifyAttempted.add(_verifyKey(v));
      for (const row of verify.folders) {
        if (row.outcome !== "verified") warnings.push({ kind: "verify-failed", folder: row.folder, reason: row.reason });
      }
      // The verify merged its entries against a fresh listing; the plan
      // must read the same one, not the cached (older) listing.
      for (const [name, files] of ex.verifyListings) prep.device.folders.set(name, { ok: true, files });
      view = viewOf(await c.store.load());
      viewReloaded = true;
    }
  }

  // prepare step 12, for the fixes this plan read off the listing.
  await _persistFixes(prep.plan, view, c.store);
  if (c.hashPool) await c.hashPool.flush().catch(() => {});
  // A re-verified folder's earlier verify-failed gives way to this outcome.
  // (Re-hashed files carry no earlier warning: hashAttempted keeps a file
  // that failed from ever being asked again.)
  const reverified = new Set(verify ? verify.folders.map((r) => r.folder) : []);
  const kept = (prep.warnings || []).filter((w) => !(w.kind === "verify-failed" && reverified.has(w.folder)));
  return { ok: true, prep: { ...prep, view, verify: verify || prep.verify, warnings: [...kept, ...warnings] }, verify, warnings, viewReloaded };
}

/**
 * The record view a prepare would build from the store as it is now, for
 * a prep's cached facts (its probe's volume and measured case). service.js
 * re-derives from it after a Link while the device is away (no job can run).
 */
async function loadView(store, prep, target) {
  const l = await store.load();
  return loadRecordView({ header: l.header, shards: l.shards, selection: l.selection, target: _withVolume(target, prep.probe), measured: prep.measured });
}

// ------------------------------------------------------------------ steps

/**
 * {bytes|null, total|null, asOf}: unknown is null, never 0 (a 0 would read
 * as "full"). `total` (the gauge's "used" part) comes only from storage();
 * a transport with just freeSpace() (test stubs) reports no total.
 */
async function _readFreeSpace(transport, { dev, signal, now, warnings }) {
  const fin = (n) => (Number.isFinite(n) ? n : null);
  if (typeof transport.storage !== "function" && typeof transport.freeSpace !== "function") return { bytes: null, total: null, asOf: now() };
  try {
    if (typeof transport.storage === "function") {
      const st = (await dev(() => transport.storage())) || {};
      return { bytes: fin(st.free), total: fin(st.total), asOf: now() };
    }
    const b = await dev(() => transport.freeSpace());
    return { bytes: fin(b), total: null, asOf: now() };
  } catch (e) {
    if (e instanceof RunStop || (signal && signal.aborted)) throw e;
    // A device that drops here fails the next listing anyway; anything
    // else only costs the gauge.
    if (e && ["device-lost", "offline", "unauthorized"].includes(e.kind)) throw e;
    warnings.push({ kind: "free-space-unknown", message: e && e.message });
    return { bytes: null, total: null, asOf: now() };
  }
}

/** @returns {Promise<Set<string>>} the paths it tried (hashed or not): derivePlan's hashAttempted */
async function _hashPc(hashPool, pc, target, { mode, signal, progress, warnings }) {
  const files = [];
  for (const s of pc.series) {
    if (s.imageOnly || !planner.isIncluded(s, target)) continue;
    for (const f of s.files) if (isMirrored(f.name, target)) files.push(f);
  }
  return _hashFiles(hashPool, pc, files, { mode, signal, progress, warnings });
}

async function _hashFiles(hashPool, pc, files, { mode, signal, progress, warnings, prune = true }) {
  const attempted = new Set();
  if (!hashPool) return attempted;
  progress({ phase: "hashing", done: 0, total: files.length, bytesDone: 0, bytesTotal: null });
  const res = await hashPool.hashFiles(files, {
    mode,
    signal,
    onProgress: (done, total, bytesDone, bytesTotal) => progress({ phase: "hashing", done, total, bytesDone, bytesTotal }),
  });
  for (const f of files) {
    const r = res.get(f.path);
    // The sha must belong to the stat the plan uses: a file that changed
    // since the walk stays unhashed (no op) until the next plan.
    attempted.add(f.path);
    if (r && r.sha256 && r.size === f.size && String(r.mtimeNs) === String(f.mtimeNs)) f.sha256 = r.sha256;
    else warnings.push({ kind: "pc-unhashed", path: f.path, error: (r && r.error) || "changed-since-walk" });
  }
  // A walk that skipped anything doesn't prune: the cache would lose
  // entries of files that still exist.
  const complete = !pc.series.some((s) => s.readError) && !(pc.warnings || []).length;
  if (prune && complete) hashPool.prune(pc.series.flatMap((s) => s.files.map((f) => f.path)));
  return attempted;
}

function _sameKeys(a, b) {
  if (!(a instanceof Set) || !(b instanceof Set) || a.size !== b.size) return false;
  for (const k of a) if (!b.has(k)) return false;
  return true;
}

/**
 * Finish or revert leftover folder renames (executor.renameFolder) from the
 * root listing's exact names. A gone intent stays: gone detection STA2s
 * every name it holds.
 * @returns {Promise<boolean>} whether a folder was moved (re-list the root)
 */
async function _folderIntents(view, root, { dev, transport, store, warnings }) {
  const present = new Set(root.dirs.map((d) => d.name));
  let moved = false;
  for (const sh of view.shards) {
    const ri = sh.renameIntent;
    if (!ri || ri.scope === "file") continue;
    const r = resolveRenameIntent(ri, present);
    if (r.state === "gone") continue;
    if (r.state === "done") sh.name = ri.to;
    else if (r.state === "not-started") sh.name = ri.from;
    else if (r.state === "conflict") {
      sh.name = ri.from;
      warnings.push({ kind: "rename-conflict", scope: "folder", from: ri.from, to: ri.to });
    } else if (r.state === "mid") {
      const fin = await dev(() => transport.move(ri.via, ri.to));
      moved = true;
      if (fin.ok) sh.name = ri.to;
      else {
        const back = await dev(() => transport.move(ri.via, ri.from));
        if (back.ok) sh.name = ri.from;
        else {
          warnings.push({ kind: "rename-unresolved", scope: "folder", via: ri.via, from: ri.from, to: ri.to, reason: fin.reason });
          continue;
        }
      }
    }
    sh.renameIntent = null;
    await store.writeShard(sh);
  }
  return moved;
}

/**
 * Finish or revert a leftover rename-to-match inside one folder, from the
 * folder listing's exact names; the entry follows the file. Mutates sh.
 * @returns {Promise<{moved:boolean}>}
 */
async function _fileIntent(sh, listed, { dev, transport, warnings, ci }) {
  const ri = sh.renameIntent;
  const present = new Set(listed.files.map((f) => f.name));
  const r = resolveRenameIntent(ri, present);
  const follow = () => {
    const from = slotKey(ri.from, ci);
    const e = sh.files.get(from);
    sh.files.delete(from);
    if (e) sh.files.set(slotKey(ri.to, ci), { ...e, name: ri.to });
  };
  let moved = false;
  if (r.state === "done") follow();
  else if (r.state === "conflict") warnings.push({ kind: "rename-conflict", scope: "file", folder: sh.name, from: ri.from, to: ri.to });
  else if (r.state === "mid") {
    moved = true;
    const fin = await dev(() => transport.move(`${sh.name}/${ri.via}`, `${sh.name}/${ri.to}`));
    if (fin.ok) follow();
    else {
      const back = await dev(() => transport.move(`${sh.name}/${ri.via}`, `${sh.name}/${ri.from}`));
      if (!back.ok) {
        warnings.push({ kind: "rename-unresolved", scope: "file", folder: sh.name, via: ri.via, from: ri.from, to: ri.to, reason: fin.reason });
        return { moved };
      }
    }
  }
  // not-started and gone: the entry stays where it was; the listing decides.
  sh.renameIntent = null;
  return { moved };
}

/**
 * Pending resolution for one listed folder (the plan's table): the listing
 * first, then one RECV hashed on the PC when the size fits. A RECV that
 * fails leaves the entry pending (it plans as a pre-selected update).
 * @returns {Promise<boolean>} whether an entry changed
 */
async function _resolvePendingIn(sh, listed, { dev, transport, warnings, ci, signal }) {
  if (!listed || !listed.ok) return false;
  const bySlot = new Map(listed.files.map((f) => [slotKey(f.name, ci), f]));
  let changed = false;
  for (const [slot, e] of [...sh.files]) {
    if (!e || e.origin !== "pending") continue;
    const f = bySlot.get(slot) || null;
    let r = resolvePending(e, f, undefined);
    if (r.outcome === "needs-recv") {
      let recv;
      try {
        const rb = await dev(() => transport.readBack(`${sh.name}/${f.name}`, { signal }));
        recv = rb.ok && rb.size === f.size ? { sha: rb.sha256 } : { error: rb.ok ? "size-changed" : rb.error || "recv-failed" };
      } catch (err) {
        if (!(err && err.kind === "remote-fail")) throw err;
        recv = { error: err.message };
      }
      r = resolvePending(e, f, recv);
    }
    if (r.outcome === "unresolved") {
      warnings.push({ kind: "pending-unresolved", folder: sh.name, name: e.name });
      continue;
    }
    if (r.entry) sh.files.set(slot, r.entry);
    else sh.files.delete(slot);
    changed = true;
  }
  return changed;
}

/** details.json for every folder; .aio_series.json (a leftover) for unbound ones. */
async function _readMeta(name, listing, { dev, transport, bound }) {
  const meta = {};
  let any = false;
  for (const [key, file] of Object.entries(DEVICE_META)) {
    if (key === "aio" && bound) continue;
    const f = listing.files.find((x) => x.name === file);
    if (!f || f.size > META_MAX_BYTES) continue;
    const buf = await dev(() => transport.readSmall(`${name}/${file}`, META_MAX_BYTES));
    if (!buf) continue;
    any = true;
    try {
      const v = JSON.parse(buf.toString("utf8"));
      if (v && typeof v === "object" && !Array.isArray(v)) meta[key] = v;
      else if (key === "details") meta.malformed = true;
    } catch (_) {
      if (key === "details") meta.malformed = true;
    }
  }
  return any ? meta : null;
}

/** buildPlan until every name it asks about has a STA2 answer. */
async function _planWithProbes(input, { dev, transport }) {
  let plan = null;
  for (let pass = 0; pass < PROBE_PASSES; pass += 1) {
    plan = planner.buildPlan(input);
    if (plan.ok === false) return plan;
    const ask = plan.needsProbe.filter((n) => !input.opts.nameProbe.has(n));
    if (!ask.length) return plan;
    for (const n of ask) {
      if (segmentProblem(n)) {
        input.opts.nameProbe.set(n, false);
        continue;
      }
      const st = await dev(() => transport.stat(n));
      input.opts.nameProbe.set(n, !st.exists && st.error === ENOENT);
    }
  }
  return planner.buildPlan(input);
}

/** Persist the final plan's entryFixes and shardNameFixes (and mirror them in the view). */
async function _persistFixes(plan, view, store) {
  const byId = new Map(view.shards.map((sh) => [sh.shardId, sh]));
  const touched = new Set();
  for (const fx of plan.entryFixes || []) {
    const sh = byId.get(fx.shardId);
    if (!sh) continue;
    sh.files.set(fx.slot, fx.entry);
    touched.add(sh);
  }
  for (const fx of plan.shardNameFixes || []) {
    const sh = byId.get(fx.shardId);
    if (!sh || sh.name === fx.name) continue;
    sh.name = fx.name;
    touched.add(sh);
  }
  for (const sh of touched) await store.writeShard(sh);
}

module.exports = { preparePlan, derivePlan, refreshPlan, loadView, verifyModeFor, verifyKeyOf: _verifyKey, META_MAX_BYTES, PROBE_PASSES };
