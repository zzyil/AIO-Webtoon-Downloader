// ============================================================
// DEVICE SYNC — EXECUTOR (main process)
//
// Owns MAKING a reviewed plan happen on one target, and the record writes
// that keep every step recoverable:
//   apply()        parent rules 6-7: per series, in this order:
//                    series in space.js's order (the free-space model)
//                    [new or gone folder] its first chapter fits → STA2
//                    the chosen name (must be ENOENT) → write the shard → mkdir
//                    cleanup names → renames → gate-free deletes →
//                    pushes/updates/replaces (each admitted by the space
//                    ledger, _roomFor) → the post-series listing (devMtime,
//                    quick verify) → gated deletes (delete gate, slot
//                    guard) → a confirming listing
//                    [series being removed from the target, planner 8b]
//                    rmdir if empty → drop its shard (result.evictions)
//   verify()       rule 8 / "Verify modes": device-hash, read-back, adopt-size
//   prune()        orphan removal: delete hash-proven files of ours, then
//                  rmdir only if empty; report leftovers
//   renameFolder() rule 1's "Rename on device", through a rename intent
//
// INTENT (deviation 1). Before a push the slot's entry becomes `pending`
// (carrying `prev`, the entry it replaces, and `prevObserved`, the slot as
// the plan saw it); after OKAY it becomes `pushed`. Each is an awaited
// atomic shard write: a failed `pending` write stops that file's SEND,
// because a push without recorded intent would break the guarantee. Batched
// intent (intentBatch > 1) writes N pending entries at once and marks them
// pushed at the batch end; it is built and tested, and stays off (1) until
// P5 measures the NTFS fsync cost. Every rename writes {scope, from, via?,
// to} into the shard first and clears it after; prepare.js finishes or
// reverts a leftover one from the parent listing's exact names.
//
// devMtime COMES ONLY FROM LISTINGS (review #4): the `pushed` write carries
// size and sha; the post-series listing fills devMtime. A series cut short
// before that listing leaves `pushed` entries without devMtime, which the
// next listing showing the exact size adopts (planner entryFixes).
//
// ERROR POLICY (the plan's error table), by AdbError kind:
//   remote-fail, timeout, session-closed  → charged (err.charges) against the
//                                           consecutive-failure budget; a
//                                           success resets it
//   no-space on a write, .nomedia or a new series' mkdir → uncharged; the
//                                           space ledger decides (_onNoSpace:
//                                           go on, or stop writing and finish
//                                           "out-of-room"); elsewhere → abort
//   read-only, record-write-failed, server-unavailable → abort
//   device-lost, offline                  → abort once: "disconnected"
//   unauthorized                          → abort (USB-debugging guidance)
//   connecting                            → wait up to CONNECT_WAIT_MS for the
//                                           device (P3 decision), uncharged;
//                                           then "disconnected"
//   cancelled                             → stop
// A PC read error (tagged `source`) skips that file, uncharged.
//
// KILL POINTS. hooks(point, ctx) is awaited at each crash window the tests
// exercise; a hook that never resolves is a process kill at that point
// (nothing after it runs). Production passes none.
//
// Read by: prepare.js (coverage Verify), service.js (P4). Depends on:
// transports.js (through the interface), store.js, hash-pool.js,
// provenance.js, planner.js (series keys), chapter-labels.js, naming.js,
// profiles.js.
// ============================================================

const crypto = require("crypto");
const { slotKey } = require("./naming");
const { labelFile } = require("./chapter-labels");
const { recordIdentity } = require("./profiles");
const { classifyDeviceFile, resolvePending, mergeVerify, verifyFailed, isOurs } = require("./provenance");
const { pcSeriesKey, EVICT_REASON, isSettling } = require("./planner");
const { newEpoch, newShardId } = require("./store");
const { segmentProblem } = require("./transports");
const { opCost, simulateRun, gateLabels, vouchedLabel } = require("./space");
const { refuse } = require("./contract");

// Pushes must fit in free space minus this (parent rule 6). It also absorbs
// a file unlinked while Komikku holds it open, which frees nothing yet
// (review #16).
const RESERVE_BYTES = 1024 * 1024 * 1024;
// The running ledger re-measures free space after this much written or
// this long (_roomFor): its estimate can't see other apps' writes.
const REFRESH_BYTES = 1024 * 1024 * 1024;
const REFRESH_MS = 30 * 1000;
// Apply's re-stat refuses a settling file (planner isSettling, one rule for
// plan and apply); the planner offers it as `settling` (deviation 9).
const { SETTLE_MS } = require("./planner");
const CONNECT_WAIT_MS = 60 * 1000;
const DEFAULT_BUDGET = 3;

/** Ends the run with a status; never escapes the executor. */
class RunStop extends Error {
  constructor(status, reason, cause) {
    super(reason);
    this.status = status;
    this.reason = reason;
    if (cause) this.cause = cause;
  }
}

/**
 * The error table as a function.
 * @returns {{action:'stop', status:string, reason:string} | {action:'charge', charges:number} | {action:'skip', reason:string}}
 */
function policyFor(e) {
  if (e instanceof RunStop) return { action: "stop", status: e.status, reason: e.reason };
  if (e && e.source) return { action: "skip", reason: "pc-file-unreadable" };
  switch (e && e.kind) {
    case "cancelled":
      return { action: "stop", status: "cancelled", reason: "cancelled" };
    case "device-lost":
    case "offline":
      return { action: "stop", status: "disconnected", reason: e.kind };
    case "connecting":
      return { action: "stop", status: "disconnected", reason: "still-connecting" };
    case "unauthorized":
    case "server-unavailable":
    case "no-space":
    case "read-only":
    case "record-write-failed":
      return { action: "stop", status: "failed", reason: e.kind };
    case "remote-fail":
    case "timeout":
    case "session-closed":
      return { action: "charge", charges: Number.isFinite(e.charges) ? e.charges : 1 };
    default:
      return { action: "stop", status: "failed", reason: "internal" };
  }
}

/**
 * A device call under the `connecting` policy (P3 decision): on
 * kind 'connecting', wait up to timeoutMs for the device, then retry,
 * uncharged; a device that doesn't come back ends the run "disconnected".
 * Shared with prepare.js.
 */
async function withConnectWait(transport, fn, { timeoutMs = CONNECT_WAIT_MS, signal } = {}) {
  for (;;) {
    try {
      return await fn();
    } catch (e) {
      if (!(e && e.kind === "connecting")) throw e;
      const w = await transport.waitReady({ timeoutMs, signal });
      if (w.state === "device") continue;
      if (w.state === "cancelled") throw new RunStop("cancelled", "cancelled");
      if (w.state === "unauthorized") throw new RunStop("failed", "unauthorized", e);
      throw new RunStop("disconnected", w.state === "timeout" ? "still-connecting" : w.state === "offline" ? "offline" : "device-lost", e);
    }
  }
}

function _copyShard(sh) {
  return { ...sh, renameIntent: sh.renameIntent || null, files: new Map(sh.files instanceof Map ? sh.files : Object.entries(sh.files || {})) };
}

function _tempName(prefix) {
  return `${prefix}${crypto.randomBytes(6).toString("hex")}`;
}

class Executor {
  /**
   * @param {object} d
   * @param {object} d.transport     AdbTransport | FolderTransport
   * @param {import('./store').RecordStore} d.store
   * @param {import('./hash-pool').HashPool} [d.hashPool]
   * @param {object} d.target        resolveTargetConfig() result
   * @param {object} [d.settings]    resolveSyncSettings() result
   * @param {object} d.probe         transport.probe() result
   * @param {object} d.view          loadRecordView() result (shards are copied)
   * @param {(p:string)=>Promise<{size:number, mtimeMs:number, mtimeNs:string}|null>} [d.statPc]
   * @param {(folderPath:string)=>boolean} [d.isPathBusy]  a running download writes there
   * @param {(ev:object)=>void} [d.onProgress]
   * @param {(point:string, ctx:object)=>Promise<void>|void} [d.hooks]
   * @param {AbortSignal} [d.signal]
   * @param {() => number} [d.now]
   * @param {number} [d.intentBatch]   1 = per-file intent (deviation 1)
   * @param {number} [d.connectWaitMs]
   * @param {number} [d.reserveBytes]
   */
  constructor(d) {
    this.transport = d.transport;
    this.store = d.store;
    this.hashPool = d.hashPool || null;
    this.target = d.target;
    this.settings = d.settings || {};
    this.probe = d.probe;
    this.ci = !!(d.view && d.view.caseInsensitive);
    this.shards = new Map(((d.view && d.view.shards) || []).map((sh) => [sh.shardId, _copyShard(sh)]));
    this.statPc = d.statPc || _statPc;
    this.isPathBusy = d.isPathBusy || (() => false);
    this.onProgress = d.onProgress || (() => {});
    this.hooks = d.hooks || null;
    this.signal = d.signal || null;
    this.now = d.now || Date.now;
    this.intentBatch = Math.max(1, d.intentBatch | 0 || 1);
    this.lane = null; // {seriesKey, landed, total} of the series being written (_writes)
    this.connectWaitMs = d.connectWaitMs != null ? d.connectWaitMs : CONNECT_WAIT_MS;
    this.reserveBytes = d.reserveBytes != null ? d.reserveBytes : RESERVE_BYTES;
    this.budgetLimit = Math.max(1, (this.settings.syncErrorBudget | 0) || DEFAULT_BUDGET);
    this.failStreak = 0;
    this.verifyListings = new Map(); // folder → listing.files of each folder verify() listed
    // space.js's cost model differs by kind (adb overwrites in place, a
    // folder writes a temp beside the old copy). Test stubs may omit it.
    this.kind = this.transport.kind || (this.target && this.target.kind) || "folder";
    this.room = { free: null, at: 0, written: 0 }; // the running ledger (_roomFor)
    this.outOfRoom = false;
    this.noRoom = { files: 0, bytes: 0, emptied: [] };
  }

  // ---------------------------------------------------------------- plumbing

  async _hook(point, ctx) {
    if (this.hooks) await this.hooks(point, ctx || {});
  }

  _checkCancel() {
    if (this.signal && this.signal.aborted) throw new RunStop("cancelled", "cancelled");
  }

  /** A device call under the `connecting` policy (withConnectWait). */
  _dev(fn) {
    return withConnectWait(this.transport, fn, { timeoutMs: this.connectWaitMs, signal: this.signal });
  }

  /** Charge a failure; throws RunStop when the budget is spent. */
  _charge(n, result, e) {
    this.failStreak += n;
    if (this.failStreak >= this.budgetLimit) {
      throw new RunStop("failed", "error-budget", e);
    }
    if (result) result.charged += n;
  }

  _ok() {
    this.failStreak = 0;
  }

  /** Apply the error table to a fault inside one file's work. */
  _onFault(e, result, rec) {
    const pol = policyFor(e);
    if (pol.action === "stop") throw e instanceof RunStop ? e : new RunStop(pol.status, pol.reason, e);
    if (pol.action === "skip") {
      rec.outcome = "skipped";
      rec.reason = pol.reason;
      return;
    }
    rec.outcome = "failed";
    rec.reason = (e && e.kind) || "failed";
    rec.message = e && e.message;
    this._charge(pol.charges, result, e);
  }

  async _ensureHeader() {
    if (this.store.epoch) return;
    const p = this.probe || {};
    const id = recordIdentity(this.target);
    // A folder target's volume is the one measured now (fs.stat's dev), so a
    // different disk at the same drive letter reads as record-mismatch.
    if (id.kind === "folder" && p.volumeId != null) id.volumeId = String(p.volumeId);
    // store.ensureHeader is single-flight: one epoch however many first
    // writers race (service.js _persistSelection, sync:link, apply).
    await this.store.ensureHeader(() => ({
      recordEpoch: newEpoch(),
      version: 1,
      ...id,
      canonicalRoot: p.canonicalRoot || null,
      caseInsensitive: !!p.caseInsensitive,
      caseMeasured: !!p.caseMeasured,
      caps: p.caps || {},
      createdAt: this.now(),
    }));
  }

  /**
   * Write the record header when the target has none yet (the first write
   * to a target). service.js calls it before sync:link binds a folder.
   */
  ensureHeader() {
    return this._ensureHeader();
  }

  async _writeShard(sh) {
    await this._ensureHeader();
    await this.store.writeShard(sh);
  }

  _slot(name) {
    return slotKey(name, this.ci);
  }

  _rel(folder, name) {
    return name == null ? folder : `${folder}/${name}`;
  }

  _labelOf(name, folder) {
    return labelFile(name, folder, this.settings.chapterPatterns || []);
  }

  // ---------------------------------------------------------------- apply

  /**
   * Apply reconciled ops (planner.reconcileForApply().ops) of the fresh plan.
   * @param {object} a
   * @param {object} a.plan        the fresh plan the ops belong to
   * @param {object[]} a.ops       ordered per series: renames, writes, deletes
   * @param {object} a.device      the listing that plan was built on
   * @param {object} a.pc          the inventory (files carry sha256, mtimeNs)
   * @param {string[]} [a.ackLosses]  delete op ids the user accepted as losses
   * @param {string} [a.runId]
   * @returns {Promise<object>} the run result (status completed/cancelled/
   *   failed/disconnected, per-file records, totals), or a `blocked` refusal
   *   when the pushes can't fit (nothing was touched)
   */
  async apply(a) {
    const plan = a.plan;
    const ops = a.ops || [];
    const ack = new Set(a.ackLosses || []);
    const writes = ops.filter((o) => o.kind === "push" || o.kind === "update" || o.kind === "replace");
    const result = {
      runId: a.runId || null,
      phase: "apply",
      status: "completed",
      reason: null,
      startedAt: this.now(),
      endedAt: null,
      filesTotal: writes.length,
      bytesTotal: writes.reduce((n, o) => n + (o.size || 0), 0),
      filesDone: 0,
      bytesDone: 0,
      charged: 0,
      files: [],
      deletes: [],
      renames: [],
      skippedSeries: [],
      evictions: [],
      mismatches: [],
      warnings: [],
    };
    this.result = result;
    this.outOfRoom = false;
    this.noSpaceSeen = 0;
    this.noRoom = { files: 0, bytes: 0, emptied: [] };
    this.pcBySeries = new Map(((a.pc && a.pc.series) || []).map((s) => [pcSeriesKey(s.folder), s]));
    this.device = a.device || {};

    // Free space (rule 6, plan A1b): the run is admitted when space.js's
    // simulation of it, in the order it runs, never dips into the reserve;
    // the same simulation gives that order. The running ledger
    // (_roomFor) then holds every write to the free space measured as it
    // goes, so a run that turns out worse skips writes instead of filling
    // the target.
    let free = null;
    if (writes.length) {
      try {
        free = await this._readFree();
      } catch (e) {
        return this._finish(result, e);
      }
    }
    const sim = simulateRun(this.kind, ops, { free, reserve: this.reserveBytes, ack });
    if (writes.length && free == null) result.warnings.push({ kind: "free-space-unknown", need: sim.needed });
    else if (sim.fits === false) {
      return refuse("blocked", "the pushes don't fit in the free space", {
        blocking: [{ kind: "free-space", need: sim.needed, free, reserve: this.reserveBytes, shortBy: sim.shortBy, at: sim.shortAt }],
      });
    }
    this.room = { free, at: this.now(), written: 0 };

    try {
      // The first apply ends the target's first sync (provenance
      // loadRecordView): a Verify alone never does.
      await this._ensureHeader();
      if (!(this.store.header && this.store.header.firstApplyAt)) await this.store.updateHeader({ firstApplyAt: this.now() });
    } catch (e) {
      return this._finish(result, e);
    }
    await this._log(result, "start");
    try {
      if (this.target.nomedia && writes.length) await this._nomedia(result);
      const bySeries = new Map();
      for (const op of ops) {
        if (!bySeries.has(op.seriesKey)) bySeries.set(op.seriesKey, []);
        bySeries.get(op.seriesKey).push(op);
      }
      for (const seriesKey of sim.order) {
        this._checkCancel();
        const sp = plan.bySeriesKey.get(seriesKey);
        if (!sp) continue;
        await this._applySeries(sp, bySeries.get(seriesKey), { ack });
      }
    } catch (e) {
      return this._finish(result, e);
    }
    if (this.noRoom.files) {
      // Completed, not failed: everything that fit ran (plan A1b). The
      // renderer reads the reason (syncView lastRunView); a re-plan offers
      // what was left.
      result.reason = "out-of-room";
      result.warnings.push({ kind: "out-of-room", files: this.noRoom.files, bytes: this.noRoom.bytes, emptied: this.noRoom.emptied.slice(0, 50) });
    }
    return this._finish(result, null);
  }

  // ---------------------------------------------------------------- space

  /** Free bytes on the target now, or null when the transport can't say. A stop-class error escapes. */
  async _readFree() {
    if (typeof this.transport.freeSpace !== "function") return null;
    try {
      const b = await this._dev(() => this.transport.freeSpace());
      return Number.isFinite(b) ? b : null;
    } catch (e) {
      if (policyFor(e).action === "stop") throw e;
      return null;
    }
  }

  /** Re-measure the ledger (a failed read keeps the estimate). */
  async _refreshFree() {
    const b = await this._readFree();
    if (b != null) this.room = { free: b, at: this.now(), written: 0 };
    else this.room.at = this.now();
    return this.room.free;
  }

  /**
   * The running ledger's admission for one write's `peak` (space.js
   * opCost). The estimate is re-measured when it is stale (REFRESH_BYTES
   * written or REFRESH_MS since the last reading) and once more before
   * refusing, so space other apps took or a held-open unlinked file never
   * makes the run write past the reserve, and space a delete freed is seen.
   * Unknown free space admits everything (the no-space backstop in _send
   * remains). On admission the estimate is charged `net` at once, so a
   * batch of intents reserves its whole cost.
   */
  async _roomFor(cost, { charge = true } = {}) {
    if (this.outOfRoom) return false;
    const r = this.room;
    if (r.free == null) return true;
    let fresh = false;
    if (r.written >= REFRESH_BYTES || this.now() - r.at >= REFRESH_MS) {
      await this._refreshFree();
      fresh = true;
    }
    if (this.room.free - this.reserveBytes < cost.peak && !fresh) await this._refreshFree();
    if (this.room.free == null) return true;
    if (this.room.free - this.reserveBytes < cost.peak) return false;
    if (!charge) return true;
    this.room.free -= cost.net;
    this.room.written += Math.max(0, cost.peak);
    return true;
  }

  /** Record a write the ledger turned away (counted for the out-of-room result). */
  _noRoom(op, rec) {
    rec.outcome = "skipped";
    rec.reason = "no-room";
    this.result.filesDone += 1;
    this.noRoom.files += 1;
    this.noRoom.bytes += op.size || 0;
  }

  /**
   * A write the target refused for space (adb FAIL ENOSPC, folder ENOSPC).
   * Uncharged. When a fresh reading shows room again the shortage was
   * someone else's and the run goes on; otherwise every later write is
   * skipped (adb's SEND unlinks before writing, so even a shrinking
   * overwrite could empty a slot) while deletes still run: gated ones only
   * where their copy landed, which frees space and loses nothing.
   */
  async _onNoSpace(op, rec) {
    rec.outcome = "failed";
    rec.reason = "no-space";
    this.noRoom.files += 1;
    this.noRoom.bytes += op.size || 0;
    // adb SEND unlinked the old copy before failing: the slot is empty now
    // (provenance resolvePending reads it as absent). A folder target's temp
    // failed beside the untouched old file.
    if (this.kind === "adb" && (op.kind === "update" || op.kind === "replace")) this.noRoom.emptied.push(this._rel(rec.folder, op.name));
    await this._refreshFree();
    // One second chance only: a reading that keeps showing room while the
    // target keeps refusing (inodes, a quota, a lying statfs) would
    // otherwise empty one overwritten slot per file.
    this.noSpaceSeen = (this.noSpaceSeen || 0) + 1;
    if (this.noSpaceSeen === 1 && this.room.free != null && this.room.free - this.reserveBytes >= opCost(this.kind, op).peak) return;
    this.outOfRoom = true;
  }

  async _finish(result, e) {
    if (e) {
      // The error itself, for a caller that rethrows it (prepare.js); not
      // enumerable, so it never reaches a run log.
      Object.defineProperty(result, "cause", { value: e, enumerable: false });
      const pol = policyFor(e);
      result.status = pol.action === "stop" ? pol.status : "failed";
      result.reason = pol.reason || (e && e.kind) || "failed";
      if (pol.reason === "internal") result.error = String((e && e.stack) || e);
      result.message = e && e.message;
    }
    result.endedAt = this.now();
    await this._log(result, "end").catch(() => {});
    if (this.hashPool) await this.hashPool.flush().catch(() => {});
    return result;
  }

  async _log(result, stage) {
    if (!this.store || typeof this.store.writeLog !== "function") return;
    if (!this.store.epoch) return; // nothing recorded yet: no record to log against
    try {
      await this.store.writeLog({
        runId: result.runId,
        targetId: this.target.id,
        phase: result.phase,
        stage,
        startedAt: result.startedAt,
        endedAt: result.endedAt,
        status: stage === "end" ? result.status : "running",
        reason: result.reason,
        planned: { files: result.filesTotal, bytes: result.bytesTotal },
        done: { files: result.filesDone, bytes: result.bytesDone },
        deletes: result.deletes,
        renames: result.renames,
        prune: result.prune || undefined,
        files: result.files.filter((f) => f.outcome !== "done"),
      });
    } catch (_) {
      // a log write failure never ends a run; the record writes do that
    }
  }

  _progress(current) {
    const r = this.result;
    this.onProgress({
      phase: r.phase,
      filesDone: r.filesDone,
      filesTotal: r.filesTotal,
      bytesDone: r.bytesDone,
      bytesTotal: r.bytesTotal,
      current: current || null,
      // The Sync tab's transfer lane (UI-pass U2-7): this series' writes
      // that LANDED, not merely passed (filesDone also counts skips).
      lane: this.lane ? { ...this.lane } : null,
    });
  }

  async _nomedia(result) {
    try {
      const st = await this._dev(() => this.transport.stat(".nomedia"));
      if (st.exists) return;
      await this._dev(() => this.transport.pushBuffer(".nomedia", Buffer.alloc(0)));
    } catch (e) {
      // A full target fails this empty file too; the ledger's reading then
      // decides what the series can still write (_roomFor).
      if (policyFor(e).action === "stop" && !(e && (e.kind === "remote-fail" || e.kind === "no-space"))) throw e;
      result.warnings.push({ kind: "nomedia-failed", message: e && e.message });
    }
  }

  /** The series' shard, created unverified on first need (a folder managed by name). */
  _shardFor(sp) {
    if (sp.shardId && this.shards.has(sp.shardId)) return this.shards.get(sp.shardId);
    return null;
  }

  /** A whole series the ledger can't start: every write is a no-room skip. */
  _skipSeriesNoRoom(sp, writes) {
    this.result.skippedSeries.push({ seriesKey: sp.seriesKey, folder: sp.deviceFolder, reason: "no-room" });
    for (const op of writes) {
      const rec = { opId: op.id, kind: op.kind, folder: sp.deviceFolder, name: op.name, size: op.size, outcome: null };
      this.result.files.push(rec);
      this._noRoom(op, rec);
    }
  }

  async _applySeries(sp, sOps, { ack }) {
    const result = this.result;
    const folder = sp.deviceFolder;
    const creating = sp.state === "new" || sp.state === "gone";
    const why = segmentProblem(folder);
    if (why) {
      result.skippedSeries.push({ seriesKey: sp.seriesKey, folder, reason: `invalid-folder-${why}` });
      return;
    }
    const renames = sOps.filter((o) => o.kind === "rename");
    const writes = sOps.filter((o) => o.kind === "push" || o.kind === "update" || o.kind === "replace");
    const deletes = sOps.filter((o) => o.kind === "delete");
    // Each series starts from a measured reading (_roomFor).
    if (writes.length) await this._refreshFree();
    let shard = this._shardFor(sp);
    if (creating) {
      // A new folder only when its first chapter fits (review F6): an empty
      // folder, or one holding only sidecars, reads in Komikku as a broken
      // series, and its created shard would plan as gone next time.
      const first = writes.find((o) => !o.sidecar) || writes[0];
      if (first && !(await this._roomFor(opCost(this.kind, first), { charge: false }))) {
        this._skipSeriesNoRoom(sp, writes);
        return;
      }
      // The folder must still be absent: SEND unlinks an existing file, so
      // writing into a folder that reappeared since the plan could replace
      // files nobody reviewed.
      const st = await this._dev(() => this.transport.stat(folder));
      if (st.exists || st.error !== 2) {
        result.skippedSeries.push({ seriesKey: sp.seriesKey, folder, reason: "folder-reappeared" });
        return;
      }
      // Rewrite (gone) or create (new) the shard BEFORE mkdir: an empty
      // folder left by a kill after mkdir must not face the old entries in
      // rule 7's zero-files sanity check.
      shard = {
        shardId: sp.state === "gone" && sp.shardId ? sp.shardId : newShardId(),
        name: folder,
        identityKey: sp.binding.identityKey || null,
        urlKey: sp.binding.urlKey || null,
        pcFolder: sp.binding.pcFolder || null,
        verifiedAt: this.now(),
        verifyMode: "created",
        renameIntent: null,
        files: new Map(),
      };
      this.shards.set(shard.shardId, shard);
      await this._writeShard(shard);
      await this._hook("after-shard-rewrite", { seriesKey: sp.seriesKey, shardId: shard.shardId, folder });
      try {
        await this._dev(() => this.transport.mkdirp(folder));
      } catch (e) {
        if (!(e && e.kind === "no-space")) throw e;
        // Not even a directory fits: the target is full. The shard stays as
        // a kill after-shard-rewrite leaves it (the next plan reads the
        // folder as gone and offers it again).
        await this._refreshFree();
        this.outOfRoom = true;
        this._skipSeriesNoRoom(sp, writes);
        return;
      }
      await this._hook("after-mkdir", { seriesKey: sp.seriesKey, folder });
    } else if (!shard) {
      shard = {
        shardId: newShardId(),
        name: folder,
        identityKey: sp.binding.identityKey || null,
        urlKey: sp.binding.urlKey || null,
        pcFolder: sp.binding.pcFolder || null,
        verifiedAt: null,
        verifyMode: null,
        renameIntent: null,
        files: new Map(),
      };
      this.shards.set(shard.shardId, shard);
    }
    const pcSeries = this.pcBySeries.get(sp.seriesKey) || { files: [] };
    const pushedSlots = new Set();
    // Gate-free deletes (no gate, or a loss the user accepted) always run
    // before the writes: they free space and wait on nothing (space.js
    // models them there). Gated ones wait for the post-series listing.
    const gated = (o) => !!o.gate && !ack.has(o.id);
    const early = deletes.filter((o) => !gated(o));
    const late = deletes.filter(gated);

    // The lane names THIS series from its first step (renames and early
    // deletes run before _writes); left alone it carried the previous
    // series' lane through them (review low).
    this.lane = { seriesKey: sp.seriesKey, landed: 0, total: writes.length };
    if (!creating) await this._cleanup(folder);
    for (const op of renames) {
      this._checkCancel();
      await this._renameFile(shard, op);
    }
    if (early.length) await this._deletes(sp, shard, early, { ack, pcSeries, pushedSlots, listing: null });
    await this._writes(sp, shard, writes, pushedSlots);

    // The post-series listing: devMtime for this run's pushes, and the quick
    // verify (every pushed file present at its exact size).
    await this._hook("before-listing", { seriesKey: sp.seriesKey, folder });
    const listing = await this._dev(() => this.transport.listFolder(folder, { afterPush: true }));
    if (listing.ok) await this._reconcile(shard, listing, { quickVerify: true });
    else result.warnings.push({ kind: "post-listing-failed", folder, error: listing.error });

    if (late.length) await this._deletes(sp, shard, late, { ack, pcSeries, pushedSlots, listing: listing.ok ? listing : null });
    if (sp.evict) await this._finishEviction(sp, shard);
  }

  /**
   * "Remove from <target>" (approved design D7): once its deletes ran, the
   * series' folder goes if it is empty, and its shard with it, so a later
   * Include starts it as a new series instead of a gone folder asking for a
   * re-push name. rmdir refuses a non-empty folder on both transports, so a
   * leftover (an unticked file, a type the app doesn't manage) keeps the
   * folder and the shard's remaining entries.
   */
  async _finishEviction(sp, shard) {
    const row = { seriesKey: sp.seriesKey, folder: sp.deviceFolder, removed: false };
    this.result.evictions.push(row);
    const rm = await this._dev(() => this.transport.rmdirIfEmpty(sp.deviceFolder, { signal: this.signal }));
    row.removed = !!rm.removed;
    if (!row.removed) return;
    this.shards.delete(shard.shardId);
    await this.store.deleteShard(shard.shardId);
  }

  /** Remove the target's cleanup names (AIO bookkeeping dot-files) from a folder. */
  async _cleanup(folder) {
    const names = new Set(this.target.cleanupNames || []);
    if (!names.size) return;
    const entry = this.device.folders instanceof Map ? this.device.folders.get(folder) : null;
    const present = ((entry && entry.files) || []).filter((f) => names.has(f.name)).map((f) => this._rel(folder, f.name));
    if (!present.length) return;
    const r = await this._dev(() => this.transport.remove(present, { signal: this.signal }));
    if (!r.ok) {
      if (r.error && policyFor(r.error).action === "stop") throw r.error;
      this.result.warnings.push({ kind: "cleanup-failed", folder, names: r.failed });
    }
  }

  // ---------------------------------------------------------------- renames

  /**
   * rename-to-match inside one folder: intent → (STA2 ENOENT) mv [→ mv] →
   * one shard rewrite. A case-only rename goes through a temp name, since
   * case-insensitive storage answers a STA2 of the new case with the file
   * itself.
   */
  async _renameFile(shard, op) {
    const folder = shard.name;
    const viaName = op.viaTemp ? _tempName(".aio-rename-") : null;
    const rec = { opId: op.id, folder, from: op.from, to: op.to, outcome: "done" };
    this.result.renames.push(rec);
    shard.renameIntent = { scope: "file", from: op.from, via: viaName, to: op.to };
    await this._writeShard(shard);
    await this._hook("rename-before-mv", { folder, from: op.from, to: op.to, via: viaName });
    try {
      if (viaName) {
        const r1 = await this._dev(() => this.transport.move(this._rel(folder, op.from), this._rel(folder, viaName), { signal: this.signal }));
        if (!r1.ok) {
          rec.outcome = "refused";
          rec.reason = r1.reason;
        } else {
          await this._hook("rename-mid", { folder, from: op.from, to: op.to, via: viaName });
          const r2 = await this._dev(() => this.transport.move(this._rel(folder, viaName), this._rel(folder, op.to), { signal: this.signal }));
          if (!r2.ok) {
            // Back to where it was; the next plan sees it under its old name.
            await this._dev(() => this.transport.move(this._rel(folder, viaName), this._rel(folder, op.from), { signal: this.signal }));
            rec.outcome = "refused";
            rec.reason = r2.reason;
          }
        }
      } else {
        const r = await this._dev(() => this.transport.move(this._rel(folder, op.from), this._rel(folder, op.to), { signal: this.signal }));
        if (!r.ok) {
          rec.outcome = "refused";
          rec.reason = r.reason;
        }
      }
    } catch (e) {
      rec.outcome = "failed";
      rec.reason = (e && e.kind) || "failed";
      // The intent stays: prepare.js resolves it from the listing.
      const pol = policyFor(e);
      if (pol.action === "stop") throw e instanceof RunStop ? e : new RunStop(pol.status, pol.reason, e);
      if (pol.action === "charge") this._charge(pol.charges, this.result, e);
      return;
    }
    await this._hook("rename-after-mv", { folder, from: op.from, to: op.to });
    if (rec.outcome === "done") {
      const from = this._slot(op.from);
      const e = shard.files.get(from);
      shard.files.delete(from);
      if (e) shard.files.set(this._slot(op.to), { ...e, name: op.to });
      this._ok();
    }
    shard.renameIntent = null;
    await this._writeShard(shard);
  }

  /**
   * rule 1's "Rename on device" for a bound folder.
   * @returns {Promise<object>} the run result
   */
  async renameFolder({ shardId, to, runId }) {
    // One folder: filesTotal 1, done once the move landed (quitInfo's count).
    const result = { runId: runId || null, phase: "rename", status: "completed", reason: null, startedAt: this.now(), endedAt: null, filesTotal: 1, bytesTotal: 0, filesDone: 0, bytesDone: 0, charged: 0, files: [], deletes: [], renames: [], warnings: [] };
    this.result = result;
    const shard = this.shards.get(shardId);
    this._progress(shard ? shard.name : null);
    try {
      if (!shard) throw new RunStop("failed", "unknown-shard");
      const why = segmentProblem(to);
      if (why) throw new RunStop("failed", `invalid-name-${why}`);
      const from = shard.name;
      const caseOnly = this._slot(from) === this._slot(to);
      const via = caseOnly ? _tempName(".aio-rename-") : null;
      const rec = { folder: null, from, to, outcome: "done" };
      result.renames.push(rec);
      shard.renameIntent = { scope: "folder", from, via, to };
      await this._writeShard(shard);
      await this._log(result, "before-rename");
      await this._hook("rename-before-mv", { from, to, via });
      if (via) {
        const r1 = await this._dev(() => this.transport.move(from, via, { signal: this.signal }));
        if (!r1.ok) throw new RunStop("failed", `rename-refused-${r1.reason}`);
        await this._hook("rename-mid", { from, to, via });
        const r2 = await this._dev(() => this.transport.move(via, to, { signal: this.signal }));
        if (!r2.ok) {
          await this._dev(() => this.transport.move(via, from, { signal: this.signal }));
          throw new RunStop("failed", `rename-refused-${r2.reason}`);
        }
      } else {
        const r = await this._dev(() => this.transport.move(from, to, { signal: this.signal }));
        if (!r.ok) throw new RunStop("failed", `rename-refused-${r.reason}`);
      }
      await this._hook("rename-after-mv", { from, to });
      shard.name = to;
      shard.renameIntent = null;
      await this._writeShard(shard);
      result.filesDone = 1;
      this._progress(to);
    } catch (e) {
      if (shard && e instanceof RunStop && /^rename-refused|^invalid-name|^unknown-shard/.test(e.reason)) {
        shard.renameIntent = null;
        await this._writeShard(shard).catch(() => {});
        if (result.renames[0]) result.renames[0].outcome = "refused";
      }
      return this._finish(result, e);
    }
    return this._finish(result, null);
  }

  // ---------------------------------------------------------------- writes

  async _writes(sp, shard, writes, pushedSlots) {
    this.lane = { seriesKey: sp.seriesKey, landed: 0, total: writes.length };
    for (let i = 0; i < writes.length; i += this.intentBatch) {
      this._checkCancel();
      const batch = [];
      for (const op of writes.slice(i, i + this.intentBatch)) {
        const rec = { opId: op.id, kind: op.kind, folder: shard.name, name: op.name, size: op.size, outcome: null };
        this.result.files.push(rec);
        const ready = await this._guard(sp, op, rec);
        if (!ready) this.result.filesDone += 1;
        // The ledger admits it before its intent is written (a batch
        // reserves every member's cost).
        else if (!(await this._roomFor(opCost(this.kind, op)))) this._noRoom(op, rec);
        else batch.push({ op, rec, pc: ready });
      }
      if (!batch.length) continue;
      // Intent first: one awaited shard write for the batch. If it fails,
      // nothing in the batch is sent.
      for (const b of batch) {
        const slot = this._slot(b.op.name);
        const existing = shard.files.get(slot);
        const prev = existing ? (existing.origin === "pending" ? existing.prev || null : existing) : null;
        shard.files.set(slot, {
          name: b.op.name,
          size: b.op.size,
          sha256: b.op.sha,
          devMtime: null,
          origin: "pending",
          prev,
          prevObserved: b.op.devFile ? { size: b.op.devFile.size, devMtime: b.op.devFile.devMtime } : null,
        });
      }
      await this._writeShard(shard);
      const landed = [];
      for (const b of batch) {
        this._checkCancel();
        await this._hook("before-send", { folder: shard.name, name: b.op.name, opId: b.op.id });
        const r = await this._send(shard, b);
        if (!r) continue;
        await this._hook("after-okay", { folder: shard.name, name: b.op.name, opId: b.op.id });
        shard.files.set(this._slot(b.op.name), { name: b.op.name, size: r.bytes, sha256: r.sha256, devMtime: null, origin: "pushed" });
        pushedSlots.add(this._slot(b.op.name));
        landed.push(b);
        this.lane.landed += 1;
        this._progress({ folder: shard.name, name: b.op.name });
        if (this.intentBatch === 1) {
          await this._writeShard(shard);
          await this._hook("after-pushed", { folder: shard.name, name: b.op.name, opId: b.op.id });
        }
      }
      if (this.intentBatch > 1 && landed.length) {
        await this._writeShard(shard);
        for (const b of landed) await this._hook("after-pushed", { folder: shard.name, name: b.op.name, opId: b.op.id });
      }
    }
  }

  /**
   * Apply's re-stat guard: the PC file still has the inventory's size and
   * mtime, is not settling (planner isSettling), and no running download writes its
   * folder. Returns the stat, or null (the file is skipped, uncharged).
   */
  async _guard(sp, op, rec) {
    const pcs = this.pcBySeries.get(sp.seriesKey);
    const inv = pcs ? pcs.files.find((f) => f.path === op.pcPath) : null;
    const why = segmentProblem(op.name);
    if (why) {
      rec.outcome = "skipped";
      rec.reason = `invalid-name-${why}`;
      return null;
    }
    if (sp.folderPath && this.isPathBusy(sp.folderPath)) {
      rec.outcome = "skipped";
      rec.reason = "downloading";
      return null;
    }
    const st = op.pcPath ? await this.statPc(op.pcPath) : null;
    if (!st || !inv || st.size !== op.size || String(st.mtimeNs) !== String(inv.mtimeNs)) {
      rec.outcome = "skipped";
      rec.reason = "changed-since-review";
      return null;
    }
    if (isSettling(Number(st.mtimeMs), this.now())) {
      rec.outcome = "skipped";
      rec.reason = "settling";
      return null;
    }
    return st;
  }

  /** SEND one file under the error table. Returns {bytes, sha256} or null. */
  async _send(shard, b) {
    const { op, rec, pc } = b;
    const rel = this._rel(shard.name, op.name);
    let base = this.result.bytesDone;
    this._progress({ folder: shard.name, name: op.name });
    let r;
    try {
      r = await this._dev(() => {
        base = this.result.bytesDone;
        return this.transport.push({
          rel,
          pcPath: op.pcPath,
          signal: this.signal,
          onProgress: (n) => {
            this.result.bytesDone = base + n;
            this._progress({ folder: shard.name, name: op.name });
          },
        });
      });
    } catch (e) {
      this.result.bytesDone = base;
      this.result.filesDone += 1;
      // The ledger charged this write's cost on admission; whatever the
      // failure left, the next admission measures again.
      this.room.at = 0;
      // The entry stays `pending`: the post-series listing (or the next
      // plan) resolves what the slot holds.
      if (e && e.kind === "no-space") await this._onNoSpace(op, rec);
      else this._onFault(e, this.result, rec);
      return null;
    }
    this.result.bytesDone = base + (r.bytes || 0);
    this.result.filesDone += 1;
    rec.outcome = "done";
    rec.sha256 = r.sha256;
    if (r.sha256 !== op.sha) {
      // Hash-while-push (deviation 2): the file changed without changing
      // size or mtime. The record gets the truth; so does the PC cache, when
      // the file still has the stat the push started from.
      this.result.mismatches.push({ opId: op.id, folder: shard.name, name: op.name, planned: op.sha, sent: r.sha256 });
      const after = await this.statPc(op.pcPath);
      if (this.hashPool && after && after.size === pc.size && String(after.mtimeNs) === String(pc.mtimeNs) && r.bytes === pc.size) {
        this.hashPool.recordObserved(op.pcPath, { size: pc.size, mtimeNs: pc.mtimeNs, sha256: r.sha256 });
      }
    }
    this._ok();
    this._progress({ folder: shard.name, name: op.name });
    return r;
  }

  /**
   * Fold a fresh folder listing into the shard: devMtime for `pushed`
   * entries without one (exact size), `pending` entries resolved as far as
   * the listing allows (absent / prev survived / partial; a size that needs
   * a RECV stays pending for the next plan). With quickVerify, a `pushed`
   * entry whose file is missing is dropped and one at another size becomes
   * `partial`.
   */
  async _reconcile(shard, listing, { quickVerify = false } = {}) {
    const bySlot = new Map(listing.files.map((f) => [this._slot(f.name), f]));
    let changed = false;
    for (const [slot, e] of [...shard.files]) {
      const f = bySlot.get(slot) || null;
      if (e.origin === "pushed" && e.devMtime == null) {
        if (f && f.size === e.size) {
          shard.files.set(slot, { ...e, name: f.name, devMtime: f.devMtime });
          changed = true;
        } else if (quickVerify) {
          if (!f) shard.files.delete(slot);
          else shard.files.set(slot, { name: f.name, size: f.size, sha256: null, devMtime: f.devMtime, origin: "partial" });
          this.result.warnings.push({ kind: "quick-verify", folder: shard.name, name: e.name, found: f ? f.size : null, expected: e.size });
          changed = true;
        }
      } else if (e.origin === "pending") {
        const r = resolvePending(e, f, undefined);
        if (r.outcome === "needs-recv") continue;
        if (r.entry) shard.files.set(slot, r.entry);
        else shard.files.delete(slot);
        changed = true;
      }
    }
    if (changed) await this._writeShard(shard);
  }

  // ---------------------------------------------------------------- deletes

  /**
   * Deletes for one series, each passing (in this order): the file still is
   * what the plan saw (size, devMtime); the slot guard (rule 6: never a slot
   * of any PC file of the series, nor one pushed this run; a removal from
   * the target, EVICT_REASON, is exempt from the PC half); the delete gate
   * (a replacement delete runs only when the post-push listing shows its
   * label at an exact PC size in a file pushed this run or recorded as the
   * PC's copy, and not one this batch deletes; unless the user accepted it
   * as a loss). The
   * run log is written before and after the batch.
   */
  async _deletes(sp, shard, dels, { ack, pcSeries, pushedSlots, listing }) {
    const folder = shard.name;
    let current = listing;
    if (!current) {
      current = await this._dev(() => this.transport.listFolder(folder));
      if (!current.ok) {
        for (const op of dels) this.result.deletes.push({ opId: op.id, folder, name: op.name, outcome: "skipped", reason: "listing-failed" });
        return;
      }
    }
    const listed = new Map(current.files.map((f) => [this._slot(f.name), f]));
    const pcSlots = new Set((pcSeries.files || []).map((f) => this._slot(f.name)));
    const deleting = new Set(dels.map((o) => this._slot(o.name)));
    const go = [];
    for (const op of dels) {
      const rec = { opId: op.id, folder, name: op.name, reason: op.reason, outcome: null };
      this.result.deletes.push(rec);
      const slot = this._slot(op.name);
      const f = listed.get(slot);
      if (!f || f.name !== op.name || !op.devFile || f.size !== op.devFile.size || f.devMtime !== op.devFile.devMtime) {
        rec.outcome = "skipped";
        rec.reason = f ? "changed-since-plan" : "already-gone";
        continue;
      }
      // Rule 6's slot guard. A removal from the target lifts the PC half:
      // its series is excluded, so the PC copy is meant to stay off it.
      if ((pcSlots.has(slot) && op.reason !== EVICT_REASON) || pushedSlots.has(slot)) {
        rec.outcome = "skipped";
        rec.reason = "slot-guard";
        continue;
      }
      if (op.gate && !ack.has(op.id)) {
        // Only a file that IS the PC's copy may vouch: pushed this run, or a
        // pushed/adopted entry still unchanged. Never the op's own file nor
        // another file this batch deletes: two identical foreign copies
        // under other names would otherwise vouch for each other after a
        // failed push and both go (auto-replace plan, "Executor"). Every
        // label the gate requires must land (a range gate names several).
        // The label/size test is space.js vouchedLabel, which planner.js
        // gateCredit also uses, so the gauge credits what this passes.
        const required = gateLabels(op.gate);
        const got = new Set();
        for (const x of current.files) {
          const xs = this._slot(x.name);
          if (deleting.has(xs)) continue;
          const e = shard.files.get(xs);
          if (!pushedSlots.has(xs) && !(e && (e.origin === "pushed" || e.origin === "adopted") && isOurs(e, x))) continue;
          const l = vouchedLabel(required, x.name, x.size, (n) => this._labelOf(n, folder));
          if (l != null) got.add(l);
        }
        const landed = required.length > 0 && required.every((r) => got.has(r.label));
        if (!landed) {
          rec.outcome = "kept";
          rec.reason = "replacement-did-not-land";
          continue;
        }
      }
      go.push({ op, rec });
    }
    if (!go.length) return;
    await this._hook("before-deletes", { folder, names: go.map((g) => g.op.name) });
    for (const g of go) g.rec.outcome = "intended";
    await this._log(this.result, "before-deletes");
    const r = await this._dev(() => this.transport.remove(go.map((g) => this._rel(folder, g.op.name)), { signal: this.signal }));
    // Which files are gone is read from the device, never inferred from rm
    // (the `sent` contract).
    let after = null;
    if (r.sent || r.ok) {
      try {
        after = await this._dev(() => this.transport.listFolder(folder));
      } catch (e) {
        if (policyFor(e).action === "stop") throw e;
      }
    }
    const still = after && after.ok ? new Set(after.files.map((f) => this._slot(f.name))) : null;
    let failedHere = 0;
    for (const g of go) {
      const slot = this._slot(g.op.name);
      if (still && !still.has(slot)) {
        g.rec.outcome = "deleted";
        shard.files.delete(slot);
      } else if (r.tooLong && r.tooLong.includes(this._rel(folder, g.op.name))) {
        g.rec.outcome = "failed";
        g.rec.reason = "path-too-long-for-shell";
      } else {
        g.rec.outcome = "failed";
        g.rec.reason = still ? "still-present" : "unconfirmed";
        failedHere += 1;
      }
    }
    await this._writeShard(shard);
    await this._log(this.result, "after-deletes");
    if (r.error && policyFor(r.error).action === "stop") throw r.error;
    if (failedHere) this._charge(failedHere, this.result, r.error || null);
    else this._ok();
  }

  // ---------------------------------------------------------------- verify

  /**
   * Verify managed folders (rule 8): results are adopted / adopted-size /
   * foreign; a failed hash keeps the old entries (it never wipes them on a
   * quiet failure). Only managed folders are recorded.
   *
   * Progress counts SERIES FOLDERS in filesDone/filesTotal (like prune), and
   * the bytes in them in bytesDone/bytesTotal: a device-hash is one shell
   * call per folder that can run for minutes on a large series, so a file
   * count would sit at 0 and the bytes are what predict the time left.
   * Consumers: connectView.workLine, SyncDock ProgressDock, service.js
   * quitInfo, syncView.lastRunView.
   * @param {object} a
   * @param {Array<{folder:string, shardId:?string, binding:object, pcFiles:object[], bytes?:number}>} a.folders
   *   `bytes` = the folder's size in the device listing the plan was built
   *   from (planner.verifyBytesOf); absent counts as 0 toward bytesTotal.
   * @param {'device-hash'|'read-back'|'adopt-size'} a.mode
   * @returns {Promise<object>} the run result; result.folders has one row each
   */
  async verify({ folders, mode = "device-hash", runId }) {
    const list = folders || [];
    const sizeOf = (f) => (Number.isFinite(f.bytes) && f.bytes > 0 ? f.bytes : 0);
    const bytesTotal = list.reduce((m, f) => m + sizeOf(f), 0);
    const result = { runId: runId || null, phase: "verify", status: "completed", reason: null, startedAt: this.now(), endedAt: null, filesTotal: list.length, bytesTotal, filesDone: 0, bytesDone: 0, charged: 0, files: [], deletes: [], renames: [], warnings: [], folders: [] };
    this.result = result;
    try {
      for (const f of list) {
        this._checkCancel();
        const row = { folder: f.folder, shardId: f.shardId || null, outcome: null };
        result.folders.push(row);
        const base = result.bytesDone;
        await this._verifyFolder(f, mode, row);
        // A failed folder is done too. Snapping to base + its planned size
        // also absorbs read-back's per-file steps, which count the live
        // listing's sizes.
        result.filesDone += 1;
        result.bytesDone = base + sizeOf(f);
        this._progress({ folder: f.folder });
      }
    } catch (e) {
      return this._finish(result, e);
    }
    return this._finish(result, null);
  }

  async _verifyFolder(f, mode, row) {
    const listing = await this._dev(() => this.transport.listFolder(f.folder));
    if (!listing.ok) {
      row.outcome = "failed";
      row.reason = `listing-${listing.error}`;
      return;
    }
    // The listing the merge reads, for the caller's plan (prepare.js folds it
    // into its device facts; grep verifyListings). Kept off `row`: rows go
    // into the run log.
    this.verifyListings.set(f.folder, listing.files);
    const files = listing.files.filter((x) => !x.name.startsWith("."));
    const largest = files.reduce((m, x) => Math.max(m, x.size || 0), 0);
    this._progress({ folder: f.folder });
    let hashes = null;
    let rc = 0;
    try {
      if (mode === "adopt-size") hashes = null;
      else if (mode === "read-back" && this.transport.kind === "adb") {
        hashes = new Map();
        for (const x of files) {
          this._checkCancel();
          const r = await this._dev(() => this.transport.readBack(this._rel(f.folder, x.name), { signal: this.signal }));
          if (r.ok && r.size === x.size) hashes.set(x.name, r.sha256);
          else rc = 1;
          this.result.bytesDone += x.size || 0;
          this._progress({ folder: f.folder, name: x.name });
        }
      } else {
        const r = await this._dev(() => this.transport.hashFolder(f.folder, { largest, signal: this.signal }));
        hashes = r.hashes;
        rc = r.rc;
      }
    } catch (e) {
      const pol = policyFor(e);
      if (pol.action === "stop") throw e instanceof RunStop ? e : new RunStop(pol.status, pol.reason, e);
      row.outcome = "failed";
      row.reason = (e && e.kind) || "failed";
      if (pol.action === "charge") this._charge(pol.charges, this.result, e);
      return;
    }
    if (mode !== "adopt-size" && verifyFailed({ deviceFiles: files, hashes, rc })) {
      row.outcome = "failed";
      row.reason = rc ? `rc-${rc}` : "empty-result";
      return;
    }
    let shard = f.shardId ? this.shards.get(f.shardId) : null;
    if (!shard) {
      shard = {
        shardId: newShardId(),
        name: f.folder,
        identityKey: (f.binding && f.binding.identityKey) || null,
        urlKey: (f.binding && f.binding.urlKey) || null,
        pcFolder: (f.binding && f.binding.pcFolder) || null,
        verifiedAt: null,
        verifyMode: null,
        renameIntent: null,
        files: new Map(),
      };
      this.shards.set(shard.shardId, shard);
    }
    const merged = mergeVerify({ entries: shard.files, deviceFiles: files, hashes, pcFiles: f.pcFiles || [], mode: this.transport.kind === "folder" ? "device-hash" : mode, caseInsensitive: this.ci });
    // A slot the app was writing whose content now matches nothing is the
    // app's partial write, not a foreign file.
    for (const [slot, e] of merged) {
      const old = shard.files.get(slot);
      if (old && old.origin === "pending" && e.origin === "foreign") merged.set(slot, { ...e, origin: "partial" });
    }
    shard.files = merged;
    shard.verifiedAt = this.now();
    shard.verifyMode = this.transport.kind === "folder" ? "device-hash" : mode;
    await this._writeShard(shard);
    row.outcome = "verified";
    row.shardId = shard.shardId;
    row.files = merged.size;
    this._ok();
  }

  // ---------------------------------------------------------------- prune

  /**
   * Orphan removal (parent "Prune = rm -rf" fix): delete the files of ours
   * that hash evidence backs (pushed, adopted, partial; never adopted-size,
   * P3 decision) and still match the record, then rmdir only if empty.
   * Everything else stays and is reported. The shard goes with the folder;
   * otherwise it keeps the remaining entries.
   * @param {object} a
   * @param {Array<{name:string, shardId:string}>} a.folders  orphaned folders of the plan
   */
  async prune({ folders, runId }) {
    // filesTotal/filesDone count FOLDERS here: the progress line (and so the
    // quit dialog's "N of M", service.js quitInfo) is per folder.
    const list = folders || [];
    const result = { runId: runId || null, phase: "prune", status: "completed", reason: null, startedAt: this.now(), endedAt: null, filesTotal: list.length, bytesTotal: 0, filesDone: 0, bytesDone: 0, charged: 0, files: [], deletes: [], renames: [], warnings: [], prune: [] };
    this.result = result;
    try {
      this._progress(null);
      for (const f of list) {
        this._checkCancel();
        await this._hook("prune-before-folder", { folder: f.name });
        await this._pruneFolder(f, result);
        result.filesDone += 1;
        this._progress(f.name);
      }
    } catch (e) {
      return this._finish(result, e);
    }
    return this._finish(result, null);
  }

  async _pruneFolder(f, result) {
    const row = { folder: f.name, removed: false, deleted: [], leftovers: [], outcome: null };
    result.prune.push(row);
    const shard = this.shards.get(f.shardId);
    if (!shard || shard.name !== f.name) {
      row.outcome = "skipped";
      row.reason = "not-orphaned";
      return;
    }
    const listing = await this._dev(() => this.transport.listFolder(f.name));
    if (!listing.ok) {
      row.outcome = "skipped";
      row.reason = `listing-${listing.error}`;
      return;
    }
    const mine = [];
    for (const x of listing.files) {
      const e = shard.files.get(this._slot(x.name));
      const proven = e && (e.origin === "pushed" || e.origin === "adopted" || e.origin === "partial");
      if (proven && classifyDeviceFile(e, x) === "ours") mine.push(x);
      else row.leftovers.push(x.name);
    }
    for (const d of listing.dirs) row.leftovers.push(`${d.name}/`);
    if (mine.length) {
      for (const x of mine) result.deletes.push({ folder: f.name, name: x.name, reason: "prune", outcome: "intended" });
      await this._log(result, "before-prune");
      const r = await this._dev(() => this.transport.remove(mine.map((x) => this._rel(f.name, x.name)), { signal: this.signal }));
      const after = await this._dev(() => this.transport.listFolder(f.name));
      const still = after.ok ? new Set(after.files.map((x) => this._slot(x.name))) : null;
      for (const x of mine) {
        const rec = result.deletes.find((d) => d.folder === f.name && d.name === x.name);
        if (still && !still.has(this._slot(x.name))) {
          rec.outcome = "deleted";
          row.deleted.push(x.name);
          shard.files.delete(this._slot(x.name));
        } else {
          rec.outcome = "failed";
          row.leftovers.push(x.name);
        }
      }
      if (r.error && policyFor(r.error).action === "stop") {
        await this._writeShard(shard);
        throw r.error;
      }
    }
    const rm = row.leftovers.length ? { removed: false } : await this._dev(() => this.transport.rmdirIfEmpty(f.name, { signal: this.signal }));
    row.removed = !!rm.removed;
    if (row.removed) {
      this.shards.delete(shard.shardId);
      await this.store.deleteShard(shard.shardId);
    } else {
      await this._writeShard(shard);
    }
    row.outcome = row.removed ? "removed" : "kept";
    await this._log(result, "after-prune");
  }
}

async function _statPc(p) {
  try {
    const st = await require("fs").promises.stat(p, { bigint: true });
    if (!st.isFile()) return null;
    return { size: Number(st.size), mtimeMs: Number(st.mtimeMs), mtimeNs: String(st.mtimeNs) };
  } catch (_) {
    return null;
  }
}

module.exports = {
  RESERVE_BYTES,
  SETTLE_MS,
  CONNECT_WAIT_MS,
  DEFAULT_BUDGET,
  RunStop,
  policyFor,
  withConnectWait,
  Executor,
};
