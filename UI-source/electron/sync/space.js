// ============================================================
// DEVICE SYNC — SPACE MODEL (main process, pure)
//
// Owns the ONE answer to "does this apply fit on the target, and in what
// order does it run" (plan sync-temp/plans/device-sync-auto-replace.md,
// phase A1b). Two callers, so the gauge can't promise a fit the executor
// then refuses:
//   service.js _capacity     the Sync tab gauge + Review's fit line, over
//                            planner.selectedOps
//   executor.js apply        admission (refuses blocked/free-space) and the
//                            series order it runs, over reconcileForApply ops
// The executor's running ledger (re-reads, no-room skips) charges writes
// with the same opCost.
//
// THE MODEL. A run works series by series (executor _applySeries): gate-free
// deletes first, then writes in plan order, then the gated deletes once the
// post-series listing proves their replacement landed. A series therefore
// needs `req` headroom to start and leaves `net` less headroom behind; a
// gated delete is credited at its series' end ONLY when the plan can prove
// its gate will pass (planner.js gateCredit), so a run admitted here doesn't
// lean on a removal that is going to be kept. Series run in the classic
// order for "need r to start, then consume c": freeing series (net <= 0) by
// ascending req, then consuming ones by descending (req − net); that order
// fits whenever any order of whole series does (brute-force check:
// tools/_test_device_sync_space.js). Inside a series, plan (chapter) order is
// kept so a run cut short leaves the first chapters, not the smallest.
//
// Not modelled: block rounding and filesystem metadata (the executor's
// 1 GiB reserve absorbs them), and a series that alone exceeds the free
// space (no reclaim inside a series; plan Q7).
//
// Depends on: profiles.js (isChapterFormat). No I/O.
// ============================================================

const { isChapterFormat } = require("./profiles");

const WRITE_KINDS = new Set(["push", "update", "replace"]);

function isWrite(op) {
  return !!op && WRITE_KINDS.has(op.kind);
}

/**
 * What one op takes from free space: `peak` while it runs, `net` once done
 * (negative = frees). adb SEND unlinks an existing file before writing, so
 * an overwrite peaks at the growth only and a shrinking one frees space; a
 * folder target writes a temp beside the old copy and renames it over, so
 * an overwrite peaks at the whole new size (transports.js FolderTransport
 * push). A delete frees its file; renames are free.
 * @param {'adb'|'folder'} kind
 * @returns {{peak:number, net:number}}
 */
function opCost(kind, op) {
  const size = (op && op.size) || 0;
  const old = op && op.devFile && Number.isFinite(op.devFile.size) ? op.devFile.size : 0;
  if (!op) return { peak: 0, net: 0 };
  if (op.kind === "push") return { peak: size, net: size };
  if (op.kind === "update" || op.kind === "replace") {
    return { peak: kind === "folder" ? size : Math.max(0, size - old), net: size - old };
  }
  if (op.kind === "delete") return { peak: 0, net: -old };
  return { peak: 0, net: 0 };
}

/**
 * The labels a delete gate requires, each with the PC sizes that count as
 * its landed copy. A chapter gate is {label, files}; a range gate (planner
 * _planExisting, a "Ch 1-10" file whose chapters this run brings) is
 * {labels, files[{name,size,label}]}: every label must land.
 * @returns {Array<{label:string, sizes:Set<number>}>}
 */
function gateLabels(gate) {
  if (!gate) return [];
  const files = gate.files || [];
  const labels = gate.labels || [gate.label];
  return labels.map((label) => ({ label, sizes: new Set(files.filter((f) => f.label == null || f.label === label).map((f) => f.size)) }));
}

/**
 * The gate label a device file would vouch for, by name and size alone, or
 * null. Whether the file MAY vouch (pushed this run, or a pushed/adopted
 * entry still ours, and not deleted in the same batch) is the caller's:
 * executor.js _deletes on a live listing, planner.js gateCredit on the plan.
 * @param {(name:string)=>{unit:string, label:string}} labelOf  labelFile with the DEVICE folder
 */
function vouchedLabel(required, name, size, labelOf) {
  if (!isChapterFormat(name)) return null;
  const lab = labelOf(name);
  if (!lab || lab.unit !== "chapter") return null;
  const hit = required.find((r) => r.label === lab.label && r.sizes.has(size));
  return hit ? hit.label : null;
}

/**
 * Whether the plan proves a gated delete's gate will pass, given the ids of
 * the ops that run. `op.gateCredit` (planner.js) holds, per required label,
 * the ways it can land. A way holds when its `op` (a write, or a rename onto
 * a vouching name) runs, if it names one, and none of its `unless` ops (a
 * voucher's own delete, a rename off the label, an overwrite of its slot)
 * runs.
 */
function gateCredited(op, running) {
  const per = op.gateCredit;
  if (!Array.isArray(per) || !per.length) return false;
  const holds = (w) => !!w && (w.op == null || running.has(w.op)) && !(w.unless || []).some((id) => running.has(id));
  return per.every((ways) => (ways || []).some(holds));
}

/**
 * Per-series demand of `ops` (any order; grouped by seriesKey, first-seen
 * order kept for ties). A delete is gate-free when it has no gate or the user
 * acknowledged it as a loss (executor _deletes skips the gate for those).
 * @returns {Array<{seriesKey:string, index:number, req:number, net:number, early:number, credited:number, writes:number, firstChapterPeak:number}>}
 */
function seriesDemand(kind, ops, ack) {
  const acked = ack || new Set();
  const running = new Set((ops || []).map((o) => o.id));
  const by = new Map();
  for (const op of ops || []) {
    if (!by.has(op.seriesKey)) by.set(op.seriesKey, { seriesKey: op.seriesKey, index: by.size, ops: [] });
    by.get(op.seriesKey).ops.push(op);
  }
  const out = [];
  for (const s of by.values()) {
    let early = 0;
    let credited = 0;
    for (const op of s.ops) {
      if (op.kind !== "delete") continue;
      const size = (op.devFile && op.devFile.size) || 0;
      if (!op.gate || acked.has(op.id)) early += size;
      else if (gateCredited(op, running)) credited += size;
    }
    // req: the headroom needed before the series starts so no write's peak
    // overruns (its early deletes run first and help).
    let used = 0;
    let req = -Infinity;
    let firstChapterPeak = 0;
    let writes = 0;
    for (const op of s.ops) {
      if (!isWrite(op)) continue;
      const c = opCost(kind, op);
      req = Math.max(req, used + c.peak - early);
      used += c.net;
      if (!writes && !op.sidecar) firstChapterPeak = c.peak;
      writes += 1;
    }
    out.push({ seriesKey: s.seriesKey, index: s.index, req: writes ? Math.max(0, req) : 0, net: used - early - credited, early, credited, writes, firstChapterPeak });
  }
  return out;
}

/** The run's series order (header THE MODEL). Stable on ties. */
function orderSeries(demand) {
  const freeing = demand.filter((s) => s.net <= 0).sort((a, b) => a.req - b.req || a.index - b.index);
  const consuming = demand.filter((s) => s.net > 0).sort((a, b) => b.req - b.net - (a.req - a.net) || a.index - b.index);
  return [...freeing, ...consuming];
}

/**
 * Simulate a run of `ops` from `free` bytes.
 * @param {'adb'|'folder'} kind
 * @param {object[]} ops
 * @param {{free:?number, reserve:number, ack?:Set<string>}} o
 * @returns {{fits:?boolean, order:string[], perSeries:object[], lowest:?number, needed:number,
 *   shortBy:number, shortAt:?string, after:?number, freeable:number}}
 *   lowest: free bytes at the run's tightest moment; needed: how far free
 *   space dips below its starting value (Review's "needs"); shortBy: bytes
 *   missing above the reserve at the tightest moment, 0 when it fits;
 *   shortAt: the first series that overruns; after: free once every write
 *   ran and every PROVABLE delete freed its file; fits is null while free
 *   space is unknown.
 */
function simulateRun(kind, ops, { free, reserve, ack } = {}) {
  const perSeries = orderSeries(seriesDemand(kind, ops, ack));
  const freeable = (ops || []).filter((o) => o.kind === "delete").reduce((n, o) => n + ((o.devFile && o.devFile.size) || 0), 0);
  // Simulate from 0 headroom: dip = the deepest the run goes below its start.
  let h = 0;
  let dip = 0;
  let shortAt = null;
  const base = free == null ? null : free - (reserve || 0);
  for (const s of perSeries) {
    if (s.writes) {
      const low = h - s.req;
      if (low < dip) dip = low;
      if (base != null && shortAt == null && base + low < 0) shortAt = s.seriesKey;
    }
    h -= s.net;
  }
  const needed = dip === 0 ? 0 : -dip;
  // Only writes are held to the reserve (executor _roomFor): a run of
  // removals and renames fits even on a target already below it.
  const writes = perSeries.some((s) => s.writes);
  const short = free == null || !writes ? 0 : Math.max(0, needed - base);
  return {
    fits: free == null ? null : short === 0,
    order: perSeries.map((s) => s.seriesKey),
    perSeries,
    lowest: free == null ? null : free - needed,
    needed,
    shortBy: short,
    shortAt,
    after: free == null ? null : free + h,
    freeable,
  };
}

module.exports = { opCost, gateLabels, vouchedLabel, gateCredited, seriesDemand, orderSeries, simulateRun, isWrite };
