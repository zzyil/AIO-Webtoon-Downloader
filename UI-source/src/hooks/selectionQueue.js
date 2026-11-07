// ============================================================
// selectionQueue — the Sync tab's client for sync:set-selection, as a pure
// per-target reducer (UI-pass plan U2-3 / U2-10).
//
// The selection is the SERVICE's (service.js rt.selOps); this only sends
// edits to it and says which checkboxes are still in flight.
//   * FIFO: per-op toggles that arrive while the previous entry is still
//     queued merge into it (last click on an op wins); bulk requests (series,
//     group, covered-extras, split-parts, rebind) are their own entries,
//     because one set-selection takes `changes` OR a bulk, never both
//     (planner.js changeSelection).
//   * ONE IN FLIGHT: the hook sends takeNext()'s request only when nothing
//     is in flight. Every request carries `seq` and the `gen` the click was
//     made on; an older gen is refused `blocked {plan-changed}` by the
//     service, so a toggle can't land on a new plan's op of the same id.
//   * PENDING until the service's view carries it: a pending mark clears when
//     a `plans` push's `selSeq` reaches its request (onView), whatever order
//     reply and event arrive in (service.js emits on setImmediate). A
//     refused reply drops its marks at once and records `error`.
//   * `seq` starts above the view's selSeq, so a renderer reload can't
//     reuse numbers the service already applied.
//
// Consumers: useDeviceSync.js (owns one per target in its state and does
// the I/O + timers). Tested through the hook: tools/_test_device_sync_hook.js [11].
// ============================================================

export const BATCH_MS = 150;

export const EMPTY = Object.freeze({ queue: [], inflight: null, nextSeq: 1, nextEntry: 1, ops: {}, series: {}, error: null });

/**
 * Mark: {desired, seq, entry, seriesKey?}; seq 0 = queued, not yet sent.
 * `entry` is the id of the queue entry that owns it: only sending THAT entry
 * stamps it. Stamping any seq-0 mark for the item's keys let an earlier
 * request stamp (and its view then clear) a later click's mark for the same
 * op, so the row showed the wrong state un-pended (review low).
 * Op marks carry their row's seriesKey (pendingRows reads it) instead of the
 * request holding a series list.
 */
function _mark(map, key, desired, seriesKey, entry) {
  return { ...map, [key]: seriesKey ? { desired, seq: 0, entry, seriesKey } : { desired, seq: 0, entry } };
}

/**
 * Queue per-op toggles made on plan `gen`.
 * @param {{opId:string, selected:boolean}[]} changes
 * @param {string} [seriesKey]  the row whose box shows them pending
 */
export function enqueueChanges(st, changes, gen, seriesKey) {
  if (!changes || !changes.length) return st;
  const queue = st.queue.slice();
  let last = queue[queue.length - 1];
  let nextEntry = st.nextEntry || 1;
  if (last && last.type === "changes" && last.gen === gen) {
    last = { ...last, changes: { ...last.changes } };
    queue[queue.length - 1] = last;
  } else {
    last = { type: "changes", gen, changes: {}, id: nextEntry++ };
    queue.push(last);
  }
  let ops = st.ops;
  for (const c of changes) {
    last.changes[c.opId] = !!c.selected;
    ops = _mark(ops, c.opId, !!c.selected, seriesKey, last.id);
  }
  return { ...st, queue, ops, nextEntry, error: null };
}

/** Queue one bulk request (its own entry). `desired` drives the row box. */
export function enqueueBulk(st, req, gen, seriesKey) {
  const id = st.nextEntry || 1;
  const item = { type: "bulk", gen, req, series: seriesKey ? [seriesKey] : [], id };
  const series = seriesKey ? _mark(st.series, seriesKey, req.selected !== false, undefined, id) : st.series;
  return { ...st, queue: [...st.queue, item], series, nextEntry: id + 1, error: null };
}

/**
 * The next request to send, or null (empty, or one already in flight).
 * @returns {{st:object, request:object|null}}  request = set-selection payload minus targetId
 */
export function takeNext(st, viewSelSeq) {
  if (st.inflight || !st.queue.length) return { st, request: null };
  const [item, ...rest] = st.queue;
  const seq = Math.max(st.nextSeq, (Number.isInteger(viewSelSeq) ? viewSelSeq : 0) + 1);
  const stamp = (map, keys) => {
    let out = map;
    for (const k of keys) if (out[k] && out[k].seq === 0 && out[k].entry === item.id) out = { ...out, [k]: { ...out[k], seq } };
    return out;
  };
  const ops = item.type === "changes" ? stamp(st.ops, Object.keys(item.changes)) : st.ops;
  const series = item.type === "bulk" ? stamp(st.series, item.series) : st.series;
  const request =
    item.type === "changes"
      ? { gen: item.gen, seq, changes: Object.entries(item.changes).map(([opId, selected]) => ({ opId, selected })) }
      : { ...item.req, gen: item.gen, seq };
  return { st: { ...st, queue: rest, inflight: { seq, item }, nextSeq: seq + 1, ops, series }, request };
}

function _dropSeq(map, pred) {
  let changed = false;
  const out = {};
  for (const [k, v] of Object.entries(map)) {
    if (pred(v)) changed = true;
    else out[k] = v;
  }
  return changed ? out : map;
}

/** The in-flight request's reply. A refusal drops its marks and records the error. */
export function onReply(st, seq, reply) {
  if (!st.inflight || st.inflight.seq !== seq) return st;
  const next = { ...st, inflight: null };
  if (reply && reply.ok) return next;
  const drop = (v) => v.seq === seq;
  return { ...next, ops: _dropSeq(st.ops, drop), series: _dropSeq(st.series, drop), error: reply || { ok: false, code: "invalid", message: "no answer" } };
}

/** A pushed plan view: marks whose request the service has applied clear. */
export function onView(st, selSeq) {
  if (!Number.isInteger(selSeq)) return st;
  const done = (v) => v.seq > 0 && v.seq <= selSeq;
  const ops = _dropSeq(st.ops, done);
  const series = _dropSeq(st.series, done);
  return ops === st.ops && series === st.series ? st : { ...st, ops, series };
}

/** Rows with anything pending: their own bulk mark, or an op mark naming them. */
export function pendingRows(st) {
  const out = new Set();
  if (!st) return out;
  for (const k of Object.keys(st.series)) out.add(k);
  for (const v of Object.values(st.ops)) if (v.seriesKey) out.add(v.seriesKey);
  return out;
}

/** Nothing queued, in flight or waiting for its view: Review's Start may run. */
export function isSettled(st) {
  return !st || (!st.inflight && !st.queue.length && !Object.keys(st.ops).length && !Object.keys(st.series).length);
}

const WRITING_PHASES = ["apply", "prune", "rename"];

/** True while a job that has taken its op list runs on this target (the service refuses `busy`). */
export function selectionLocked(job, targetId) {
  return !!job && job.state === "running" && job.targetId === targetId && WRITING_PHASES.includes(job.phase);
}
