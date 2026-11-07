// ============================================================
// DEVICE SYNC — PROVENANCE (pure)
//
// Owns what the record says about a device file and what that licenses:
//   * classifyDeviceFile — ours / pending / changed / foreign / unverified;
//   * resolvePending     — the next plan's verdict on a slot a push was
//                          writing when the run ended (non-UI plan,
//                          "Pending resolution" table);
//   * resolveRenameIntent — finish or revert a rename a kill interrupted;
//   * mergeVerify        — fold a Verify's hashes into a folder's entries;
//   * loadRecordView     — header/shard/selection files → one consistent
//                          view (recordEpoch filter, slot re-keying,
//                          header mismatch).
//
// ORIGINS (contract.js ORIGINS):
//   pushed       the app wrote it and the write was acknowledged;
//   adopted      Verify hashed it and the sha equals a current PC file of the
//                series;
//   adopted-size Verify matched it by size alone, on a device without
//                sha256sum, at the user's explicit choice. Stores the PC
//                file's sha. Weak: never pre-selects a delete, never covers a
//                label for another file's delete (planner.js);
//   partial      the app wrote to it and the write didn't finish (ours; its
//                update is pre-selected);
//   foreign      Verify saw it and it matches nothing on the PC;
//   pending      intent recorded before a push (carries `prev`, the entry it
//                replaces); resolved at the next plan.
// "Ours" = pushed / adopted / adopted-size / partial AND the listing's size
// and devMtime still equal the entry's. Anything else in a recorded slot has
// changed on the device (a Komikku custom cover.jpg, for one).
//
// Read by: planner.js, executor.js, prepare.js.
// ============================================================

const { slotKey } = require("./naming");
const { recordIdentity, RECORD_IDENTITY_FIELDS } = require("./profiles");

const ORIGINS_OURS = Object.freeze(new Set(["pushed", "adopted", "adopted-size", "partial"]));

/**
 * Device mtimes are compared in whole seconds, truncated. LIS2 reports
 * st_mtime seconds while `find -printf %T@` and fs.stat carry fractions, and
 * only values read the same way as the next plan's listing are comparable.
 */
function normalizeDevMtime(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

function isOurs(entry, devFile) {
  return (
    !!entry &&
    !!devFile &&
    ORIGINS_OURS.has(entry.origin) &&
    entry.devMtime != null &&
    devFile.size === entry.size &&
    devFile.devMtime === entry.devMtime
  );
}

/**
 * A `foreign` entry still describes the file in its slot: same size and
 * devMtime as when Verify hashed it. classifyDeviceFile calls any foreign
 * entry foreign; only an unchanged one may pre-select its own replacement
 * (planner.js auto-replace, grep foreignCurrent), since a file rewritten
 * after its Verify is as unknown as an unverified one.
 */
function isForeignCurrent(entry, devFile) {
  return (
    !!entry &&
    !!devFile &&
    entry.origin === "foreign" &&
    !!entry.sha256 &&
    entry.devMtime != null &&
    devFile.size === entry.size &&
    devFile.devMtime === entry.devMtime
  );
}

/**
 * @param {object|undefined} entry   The recorded entry for this slot.
 * @param {{size:number, devMtime:number}} devFile  The listing's file.
 * @returns {'ours'|'pending'|'changed'|'foreign'|'unverified'}
 *   unverified = no entry: the folder was never verified, or the file
 *   appeared after its Verify. Only 'ours' may ever pre-select a delete.
 */
function classifyDeviceFile(entry, devFile) {
  if (!entry) return "unverified";
  if (entry.origin === "pending") return "pending";
  if (entry.origin === "foreign") return "foreign";
  if (ORIGINS_OURS.has(entry.origin)) return isOurs(entry, devFile) ? "ours" : "changed";
  return "unverified";
}

/**
 * A `pushed` entry still without devMtime (an unplug, cancel or kill landed
 * between the acknowledged push and the post-series listing) adopts the
 * devMtime of the next listing that shows its exact size. The push was
 * acknowledged and hash-while-push knows its sha; without this every
 * interrupted series would read as "changed on device" on resume (second
 * review #7). Returns the fixed entry, or null when nothing changes.
 */
function adoptMissingDevMtime(entry, devFile) {
  if (!entry || entry.origin !== "pushed" || entry.devMtime != null) return null;
  if (!devFile || devFile.size !== entry.size) return null;
  return { ...entry, devMtime: devFile.devMtime };
}

/**
 * Resolve a `pending` entry against the listing and, when needed, one RECV
 * of the file hashed on the PC. Rows are checked in the plan's table order.
 *
 * `prevObserved` is the slot as the plan saw it ({size, devMtime} of the
 * device file the op was replacing; absent for a push into an empty slot).
 * When the listing still shows exactly that, the write never reached the
 * slot, and what was recorded before comes back unchanged: the original
 * entry, or no entry at all. Without it, a replace of a file the app never
 * wrote (unverified, foreign, changed on the device) interrupted before its
 * SEND reached adbd resolved as `partial`, i.e. ours, and a later plan could
 * pre-select its delete (found in P3).
 *
 * @param {object} entry   {name, size (planned), sha256 (planned), origin:'pending', prev?, prevObserved?}
 * @param {object|null} devFile  The listing's file at that slot, or null.
 * @param {undefined|{sha:string}|{error:string}} recv  undefined = not read yet.
 * @returns {{outcome:'absent'|'restore-prev'|'pushed'|'partial'|'needs-recv'|'unresolved', entry?:object|null}}
 *   entry is the slot's new entry (null = drop it). 'needs-recv' asks the
 *   caller to RECV and call again. 'unresolved' (the RECV failed) keeps the
 *   pending entry: it plans as a pre-selected update and is never a delete
 *   candidate, because the app was writing that slot and the PC has the file.
 */
function resolvePending(entry, devFile, recv) {
  const prev = entry.prev || null;
  const seen = entry.prevObserved || null;
  if (!devFile) return { outcome: "absent", entry: null };
  if (seen && seen.devMtime != null && devFile.size === seen.size && devFile.devMtime === seen.devMtime) {
    return { outcome: "restore-prev", entry: prev ? { ...prev } : null };
  }
  // SEND always unlinks and recreates, so an unchanged (size, devMtime)
  // means it never reached adbd (or a FolderTransport temp never got
  // renamed): the old copy survived untouched.
  if (prev && prev.devMtime != null && devFile.size === prev.size && devFile.devMtime === prev.devMtime) {
    return { outcome: "restore-prev", entry: { ...prev } };
  }
  const sizeFits = devFile.size === entry.size || (prev && devFile.size === prev.size);
  if (!sizeFits) {
    return { outcome: "partial", entry: _partial(entry, devFile, null) };
  }
  if (recv === undefined) return { outcome: "needs-recv" };
  if (!recv || recv.error || !recv.sha) return { outcome: "unresolved", entry: { ...entry } };
  if (recv.sha === entry.sha256 && devFile.size === entry.size) {
    return {
      outcome: "pushed",
      entry: { name: entry.name, size: devFile.size, sha256: entry.sha256, devMtime: devFile.devMtime, origin: "pushed" },
    };
  }
  if (prev && recv.sha === prev.sha256 && devFile.size === prev.size) {
    return { outcome: "restore-prev", entry: { ...prev, devMtime: devFile.devMtime } };
  }
  return { outcome: "partial", entry: _partial(entry, devFile, recv.sha) };
}

function _partial(entry, devFile, sha) {
  return { name: entry.name, size: devFile.size, sha256: sha, devMtime: devFile.devMtime, origin: "partial" };
}

/**
 * Finish or revert a rename a kill interrupted, from the parent listing's
 * EXACT names. A STA2 can't decide it: case-insensitive storage answers for
 * both case variants of a case-only rename.
 *
 * @param {{from:string, via?:string, to:string}} intent
 * @param {Set<string>|string[]} presentNames  Exact names in the parent listing.
 * @returns {{state:'done'|'not-started'|'mid'|'gone'|'conflict', name:?string, finish?:{src:string, dst:string}}}
 *   name is where the object lives now (null when gone). 'mid' means the
 *   temp name holds it and `finish` completes the rename. 'conflict' (both
 *   from and to exist as different entries) keeps `from`: the destination
 *   was required to be absent before the mv, so something else created it.
 */
function resolveRenameIntent(intent, presentNames) {
  const present = presentNames instanceof Set ? presentNames : new Set(presentNames || []);
  const hasFrom = present.has(intent.from);
  const hasTo = present.has(intent.to);
  const hasVia = !!intent.via && present.has(intent.via);
  if (hasVia) return { state: "mid", name: intent.via, finish: { src: intent.via, dst: intent.to } };
  if (hasTo && !hasFrom) return { state: "done", name: intent.to };
  if (hasFrom && !hasTo) return { state: "not-started", name: intent.from };
  if (hasFrom && hasTo) return { state: "conflict", name: intent.from };
  return { state: "gone", name: null };
}

/**
 * True when a folder's Verify must be treated as failed and its old entries
 * kept: a non-zero rc, or an empty result while the listing shows files.
 * CompareManga's verify ignored rc and wiped the record on a quiet failure
 * (sync_to_tablet.py:373).
 */
function verifyFailed({ deviceFiles, hashes, rc }) {
  if (rc != null && rc !== 0) return true;
  const n = Array.isArray(deviceFiles) ? deviceFiles.length : 0;
  const h = hashes ? hashes.size : 0;
  return n > 0 && h === 0;
}

/**
 * Fold one folder's Verify result into its entries.
 *
 * device-hash / read-back: a hashed file whose sha equals any current PC
 * file of the series is `adopted` (a still-valid `pushed` entry with that sha
 * stays `pushed`); any other hashed file is `foreign`. A listed file the
 * hash output skipped keeps a still-valid entry, else has none.
 * adopt-size: no hashes. A still-valid ours entry is kept; a file whose slot
 * holds a PC file of the same size becomes `adopted-size` carrying the PC
 * sha; anything else is `foreign` with no sha.
 * Entries for files no longer listed are dropped (the listing is the truth).
 *
 * @returns {Map<string, object>} slot → entry
 */
function mergeVerify({ entries, deviceFiles, hashes, pcFiles, mode, caseInsensitive }) {
  const old = entries instanceof Map ? entries : new Map();
  const pcShas = new Set((pcFiles || []).map((p) => p.sha256).filter(Boolean));
  const pcBySlot = new Map((pcFiles || []).map((p) => [slotKey(p.name, caseInsensitive), p]));
  const out = new Map();
  for (const d of deviceFiles || []) {
    const slot = slotKey(d.name, caseInsensitive);
    const e = old.get(slot);
    const stillOurs = isOurs(e, d);
    if (mode === "adopt-size") {
      if (stillOurs) {
        out.set(slot, e);
        continue;
      }
      const p = pcBySlot.get(slot);
      if (p && p.size === d.size && p.sha256) {
        out.set(slot, { name: d.name, size: d.size, sha256: p.sha256, devMtime: d.devMtime, origin: "adopted-size" });
      } else {
        out.set(slot, { name: d.name, size: d.size, sha256: null, devMtime: d.devMtime, origin: "foreign" });
      }
      continue;
    }
    const sha = hashes ? hashes.get(d.name) : undefined;
    if (!sha) {
      if (stillOurs) out.set(slot, e);
      continue;
    }
    if (stillOurs && e.origin === "pushed" && e.sha256 === sha) {
      out.set(slot, e);
    } else {
      const origin = pcShas.has(sha) ? "adopted" : "foreign";
      out.set(slot, { name: d.name, size: d.size, sha256: sha, devMtime: d.devMtime, origin });
    }
  }
  return out;
}

/**
 * One consistent view of a target's record files.
 *
 * - Shards and the selection whose recordEpoch differs from the header's are
 *   ignored (staleShardIds lists them for garbage collection). Forget and
 *   rule 12's clear write a new epoch in one atomic header write, so a
 *   half-finished delete of old shards can never resurrect their entries.
 * - Entries are re-keyed by slotKey(entry.name, ci) on load, so a re-probed
 *   caseInsensitive flag changes nothing on disk.
 * - mismatch lists the header fields that disagree with the target as it is
 *   now; the planner refuses with record-mismatch while it is non-empty.
 *
 * @param {object} args
 * @param {object|null} args.header       targets/<id>/header.json, null when absent.
 * @param {Array<object>} args.shards     Parsed folders/<shardId>.json ({shardId, ...}).
 * @param {object|null} args.selection    targets/<id>/selection.json.
 * @param {object} args.target            The resolved target config.
 * @param {{canonicalRoot?:string, caseInsensitive?:boolean}} [args.measured]  This connection's probe.
 */
function loadRecordView({ header, shards, selection, target, measured }) {
  const m = measured || {};
  if (!header || !header.recordEpoch) {
    return {
      epoch: null,
      header: null,
      firstSync: true,
      mismatch: [],
      caseInsensitive: !!m.caseInsensitive,
      shards: [],
      staleShardIds: (shards || []).map((s) => s.shardId),
      selection: { ops: {}, rebinds: {} },
    };
  }
  const epoch = header.recordEpoch;
  const ci = typeof m.caseInsensitive === "boolean" ? m.caseInsensitive : !!header.caseInsensitive;
  const want = recordIdentity(target);
  const mismatch = [];
  for (const f of RECORD_IDENTITY_FIELDS) {
    const have = header[f] == null ? null : String(header[f]);
    const now = want[f] == null ? null : String(want[f]);
    if (have !== now) mismatch.push(f);
  }
  if (m.canonicalRoot && header.canonicalRoot && m.canonicalRoot !== header.canonicalRoot) {
    mismatch.push("canonicalRoot");
  }
  const live = [];
  const staleShardIds = [];
  for (const s of shards || []) {
    if (!s || s.recordEpoch !== epoch) {
      if (s && s.shardId) staleShardIds.push(s.shardId);
      continue;
    }
    const files = new Map();
    for (const e of Object.values(s.files || {})) {
      if (!e || typeof e.name !== "string") continue;
      const key = slotKey(e.name, ci);
      if (!files.has(key)) files.set(key, e);
    }
    live.push({
      shardId: s.shardId,
      name: s.name,
      identityKey: s.identityKey || null,
      urlKey: s.urlKey || null,
      pcFolder: s.pcFolder || null,
      verifiedAt: s.verifiedAt || null,
      verifyMode: s.verifyMode || null,
      renameIntent: s.renameIntent || null,
      files,
    });
  }
  const sel = selection && selection.recordEpoch === epoch ? selection : null;
  return {
    epoch,
    header,
    // First sync lasts until the first apply (executor.js sets firstApplyAt),
    // not until the first shard: prepare.js's coverage Verify writes shards
    // on a first sync, and rule 2's "weak matches block too" must not end
    // before the user applied anything.
    firstSync: live.length === 0 || !header.firstApplyAt,
    mismatch,
    caseInsensitive: ci,
    shards: live,
    staleShardIds,
    selection: {
      ops: (sel && sel.ops && typeof sel.ops === "object" ? sel.ops : {}) || {},
      rebinds: (sel && sel.rebinds && typeof sel.rebinds === "object" ? sel.rebinds : {}) || {},
    },
  };
}

module.exports = {
  ORIGINS_OURS,
  normalizeDevMtime,
  isOurs,
  isForeignCurrent,
  classifyDeviceFile,
  adoptMissingDevMtime,
  resolvePending,
  resolveRenameIntent,
  verifyFailed,
  mergeVerify,
  loadRecordView,
};
