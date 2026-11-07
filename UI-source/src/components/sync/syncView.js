// ============================================================
// syncView — the Sync tab's pure view model (UI pass U2)
//
// Everything the tab DERIVES from what main pushes, in plain functions so it
// can be tested in node (tools/_test_device_sync_view.js bundles this file
// with esbuild). No React, no I/O, no sync logic: the planner already
// decided every state (planner.js planSummary / seriesDetail / planReview);
// this only counts, filters, sorts and words what it sent.
//
// Field meanings it relies on (planner.js planSummary rows):
//   counts.push/update/replace/rename/delete = SELECTED ops of that kind;
//   counts.offered / preselected = ops the row's box acts on / ticks itself;
//   strip `warn` segments = offered ops left unticked that need a decision.
// So a row's "?N" pill is its warn-segment count, not offered − selected
// (an unticked push draws as `absent`, which isn't a decision).
//
// Consumers: components/sync/SyncTab.jsx and the pieces it renders (grep
// "from \"./syncView\""). Depends on lib/utils formatBytes, copy.js words.
// ============================================================

import { formatBytes } from "@/lib/utils";
import { DELETE_POLICY_PLANNED, DEVICE_STATE_COPY, DROPPED_REASON_COPY, HASH_MODE_COPY, jobReasonCopy, lastRunTitle, skipReasonCopy } from "./copy";

export const TILE_IDS = Object.freeze(["push", "delete", "synced", "only", "attn"]);
export const SORTS = Object.freeze([
  { value: "copy", label: "Most to copy" },
  { value: "remove", label: "Most to remove" },
  { value: "size", label: "Largest" },
  { value: "name", label: "Name" },
]);
const WRITING_PHASES = new Set(["apply", "prune", "rename"]);

/** Per-row numbers the ledger draws. */
export function rowView(row) {
  const c = (row && row.counts) || {};
  const writes = (c.push || 0) + (c.update || 0) + (c.replace || 0);
  const deletes = c.delete || 0;
  const selected = writes + deletes + (c.rename || 0);
  const offered = c.offered || 0;
  const preselected = c.preselected || 0;
  const warn = (row.strip || []).reduce((n, s) => n + (s.state === "warn" ? s.count : 0), 0);
  const held = row.state === "held";
  // offered 0 → no box; nothing ticked → off; all → on; else mixed.
  const box = offered === 0 ? null : selected === 0 ? false : selected >= offered ? true : "mixed";
  return {
    writes,
    deletes,
    selected,
    offered,
    preselected,
    warn,
    held,
    box,
    inSync: !held && row.state === "bound" && selected === 0 && warn === 0,
    attention: Array.isArray(row.attention) ? row.attention : [],
  };
}

/**
 * What a click on the row's box sends (selectionQueue bulk 'series'), or
 * {openDetail:true} when ticking would change nothing: bulk 'series' on
 * restores the plan's pre-selection, and a row whose ops are all unticked
 * by design (a loss, a replacement) has none to restore (planner.js
 * _offeredCounts). Any ticked row turns fully off; mixed included, because
 * "on" means "back to the plan's choice", which a mixed row may already be.
 */
export function rowToggle(row) {
  const v = rowView(row);
  if (v.box == null) return null;
  if (v.selected > 0) return { bulk: "series", seriesKey: row.seriesKey, selected: false };
  if (v.preselected > 0) return { bulk: "series", seriesKey: row.seriesKey, selected: true };
  return { openDetail: true };
}

/** The five tiles' numbers (spec §4). */
export function tileCounts(summary) {
  const rows = (summary && summary.series) || [];
  const t = (summary && summary.totals) || {};
  const n = (k) => (t[k] && t[k].count) || 0;
  let copySeries = 0;
  let synced = 0;
  let attn = 0;
  for (const r of rows) {
    const v = rowView(r);
    if (v.writes > 0) copySeries += 1;
    if (v.inSync) synced += 1;
    if (v.attention.length) attn += 1;
  }
  return {
    push: { n: n("push") + n("update") + n("replace"), series: copySeries },
    delete: { n: n("delete") },
    synced: { n: synced },
    only: { n: ((summary && summary.deviceOnly) || []).length },
    attn: { n: attn },
  };
}

export function rowMatchesTile(row, tile) {
  if (!tile) return true;
  const v = rowView(row);
  switch (tile) {
    case "push":
      return v.writes > 0;
    case "delete":
      return v.deletes > 0;
    case "synced":
      return v.inSync;
    case "attn":
      return v.attention.length > 0;
    case "only":
      return false; // the tile shows the device-only section alone
    default:
      return true;
  }
}

function matchesQuery(row, q) {
  if (!q) return true;
  const s = q.toLowerCase();
  return [row.title, row.folder, row.deviceFolder].some((x) => typeof x === "string" && x.toLowerCase().includes(s));
}

const byName = (a, b) => String(a.title || a.folder).localeCompare(String(b.title || b.folder), undefined, { numeric: true, sensitivity: "base" });

/**
 * The ledger's two sections: `active` (sorted as asked) and `held` (design
 * D8: series not synced to the target, always last, by name).
 */
export function ledgerRows(summary, { tile = null, query = "", sort = "copy" } = {}) {
  const rows = ((summary && summary.series) || []).filter((r) => rowMatchesTile(r, tile) && matchesQuery(r, query.trim()));
  const active = [];
  const held = [];
  // A series being removed (design D7) is held 'excluded' by the planner but
  // has work pending, so it stays in the main list where the user looks for
  // pending removals (U2-12), not in the dimmed section.
  for (const r of rows) (r.state === "held" && !r.removing ? held : active).push(r);
  const key = {
    copy: (r) => [rowView(r).writes, (r.counts && r.counts.bytes) || 0],
    remove: (r) => [rowView(r).deletes, 0],
    size: (r) => [(r.counts && r.counts.bytes) || 0, 0],
  }[sort];
  active.sort((a, b) => {
    if (key) {
      const [a1, a2] = key(a);
      const [b1, b2] = key(b);
      if (b1 !== a1) return b1 - a1;
      if (b2 !== a2) return b2 - a2;
    }
    return byName(a, b);
  });
  held.sort(byName);
  return { active, held };
}

/** The dock's totals line: what Review would run. */
export function dockTotals(summary) {
  const t = (summary && summary.totals) || {};
  const rows = (summary && summary.series) || [];
  return {
    series: rows.filter((r) => rowView(r).selected > 0).length,
    files: t.files || 0,
    bytes: t.bytes || 0,
    removals: (t.delete && t.delete.count) || 0,
    // Renames move no bytes and aren't in `files`, but they are work: a
    // rename-only selection must still reach Review (review R5).
    renames: (t.rename && t.rename.count) || 0,
  };
}

/** Everything Start would run: files written, removals and renames. */
export function workCount(totals) {
  const t = totals || {};
  return (t.files || 0) + ((t.delete && t.delete.count) || 0) + ((t.rename && t.rename.count) || 0);
}

/**
 * Chapters still downloading (service.js settle timer re-plans at settleAt):
 * {seconds, chapters, titles} while settleAt is ahead of `now`, else null.
 */
export function settleInfo(summary, now) {
  if (!summary || !summary.settleAt || summary.settleAt <= now) return null;
  const rows = (summary.series || []).filter((r) => r.counts && r.counts.settling > 0);
  const chapters = rows.reduce((n, r) => n + r.counts.settling, 0);
  if (!chapters) return null;
  return { seconds: Math.max(1, Math.ceil((summary.settleAt - now) / 1000)), chapters, titles: rows.map((r) => r.title || r.folder) };
}

export function settleLine(info) {
  if (!info) return null;
  const what = info.titles.length === 1 ? `of ${info.titles[0]} ` : "";
  const many = info.chapters === 1 ? "chapter" : "chapters";
  const verb = info.chapters === 1 ? "is" : "are";
  return `${info.chapters} ${many} ${what}${verb} still downloading. They join the plan in ${info.seconds} s.`;
}

/**
 * Bytes per second over the last `windowMs` of progress samples
 * [{at, bytes}] (job progress `at` from service.js _progressOf), or null
 * when there isn't enough to say.
 */
export function throughput(samples, windowMs = 8000) {
  if (!Array.isArray(samples) || samples.length < 2) return null;
  const last = samples[samples.length - 1];
  let first = samples[0];
  for (const s of samples) if (last.at - s.at <= windowMs) { first = s; break; }
  const dt = last.at - first.at;
  if (dt < 500 || last.bytes < first.bytes) return null;
  return ((last.bytes - first.bytes) * 1000) / dt;
}

/**
 * Remaining ms at the AVERAGE rate over `samples` ({at, bytes}, one step of
 * one run), or null until there's enough to go on (3 samples over 15 s).
 * For progress that moves in coarse steps (a read-back advances per series),
 * where throughput()'s 8 s window mostly sees no movement.
 */
export function averageEta(samples, progress) {
  if (!progress || !(progress.bytesTotal > 0) || !Array.isArray(samples) || samples.length < 3) return null;
  const a = samples[0];
  const b = samples[samples.length - 1];
  const dt = b.at - a.at;
  if (dt < 15000 || !(b.bytes > a.bytes)) return null;
  const left = progress.bytesTotal - (progress.bytesDone || 0);
  return left > 0 ? (left * dt) / (b.bytes - a.bytes) : 0;
}

/** Remaining ms at `rate` bytes/s, or null. */
export function etaMs(progress, rate) {
  if (!progress || !rate || !(progress.bytesTotal > 0)) return null;
  const left = progress.bytesTotal - (progress.bytesDone || 0);
  return left > 0 ? (left / rate) * 1000 : 0;
}

/**
 * The running job on this target, by kind, or null. `planning` is a full
 * plan (or a Verify job, which prepares too); `refreshing` is the automatic
 * top-up after a review decision (service.js _startRefresh), which leaves
 * the shown plan in place.
 */
export function jobOn(job, targetId) {
  if (!job || job.state !== "running" || job.targetId !== targetId) return null;
  return { job, writing: WRITING_PHASES.has(job.phase), planning: job.phase === "plan" || job.phase === "verify", refreshing: job.phase === "refresh" };
}

// ── Decisions without full re-plans (plan sync-temp/plans/device-sync-cheap-replans.md §7) ──
// The plan view (service.js _planView) carries `links`: Links the shown plan
// doesn't reflect yet, bound to the record or still queued behind a job.
// `updating` is a decision on its way in (a derive or a refresh), as opposed
// to `stale` alone, a full plan owed (`stale` is true for either).

/** {folders, series}: Sets of the device folders and series keys with a Link in flight. */
export function linkIndex(pv) {
  const folders = new Set();
  const series = new Set();
  for (const l of (pv && Array.isArray(pv.links) && pv.links) || []) {
    if (l && typeof l.deviceFolder === "string") folders.add(l.deviceFolder);
    if (l && typeof l.seriesKey === "string") series.add(l.seriesKey);
  }
  return { folders, series };
}

/**
 * The transient state a series row shows in place of its planner badge, or
 * null (copy.js LIVE_STATE_COPY words it):
 *   linking         a Link of this series isn't in the plan yet
 *   verifying       needs-verify while a decision is landing (the refresh
 *                   that Link asked for checks the folder)
 *   verify-offline  needs-verify with the target away; the reconnect's plan
 *                   checks it
 * needs-verify with neither is a check that failed or wasn't asked for: the
 * planner's own "Needs a check" stays.
 */
export function liveState(row, { links, updating, refreshing, connected } = {}) {
  if (!row) return null;
  if (links && links.series.has(row.seriesKey)) return "linking";
  if (row.state !== "needs-verify") return null;
  if (!connected) return "verify-offline";
  return updating || refreshing ? "verifying" : null;
}

/**
 * The selection dock's status line {kind: busy|settle|ok, text} or null.
 * A full plan (a plan or Verify job running, or one owed) outranks a
 * decision landing: the full plan carries the decision too.
 */
export function dockStatus({ run, pv, settle, otherRunning, summary, tn }) {
  if (run && run.planning) return { kind: "busy", text: `Planning ${tn}…` };
  if (pv && pv.stale && !pv.updating) return { kind: "busy", text: "Something changed; re-planning…" };
  if (run && run.refreshing) {
    const p = run.job.progress || {};
    const folder = p.current && p.current.folder;
    if (p.step === "verify" && folder) return { kind: "busy", text: `Updating the plan: checking ${folder} on ${tn}…` };
    if (p.step === "hashing") return { kind: "busy", text: "Updating the plan: fingerprinting PC files…" };
    return { kind: "busy", text: "Updating the plan…" };
  }
  if (pv && pv.updating) return { kind: "busy", text: "Updating the plan…" };
  if (settle) return { kind: "settle", text: settleLine(settle) };
  if (otherRunning) return { kind: "busy", text: "Another target's sync is running; Start waits for it." };
  if (summary) return { kind: "ok", text: `Plan ${pv.gen} · built ${clock(summary.asOf)}` };
  return null;
}

/**
 * A target's connection as the tab words it. {connected, tone, label}
 * tone: ok | info | warn | bad | muted (the status dot).
 */
export function targetStatus(t, devices, folderPresence) {
  if (!t) return { connected: false, tone: "muted", label: "No target" };
  if (t.kind === "folder") {
    const p = (folderPresence || {})[t.id];
    if (p === true) return { connected: true, tone: "ok", label: "Connected" };
    if (p === false) return { connected: false, tone: "muted", label: "Folder not found" };
    return { connected: false, tone: "muted", label: "Checking the folder…" };
  }
  const d = (devices || []).find((x) => x.serial === t.serial);
  if (!d) return { connected: false, tone: "muted", label: "Not connected" };
  const st = d.stateKind || d.state;
  const c = DEVICE_STATE_COPY[st] || { tone: "muted", label: st };
  return { connected: st === "device", tone: c.tone, label: c.label, state: st };
}

/** The connected targets first (the picker's order), else config order. */
export function pickTargets(targets, devices, folderPresence) {
  const list = (targets || []).map((t, i) => ({ t, i, s: targetStatus(t, devices, folderPresence) }));
  list.sort((a, b) => (b.s.connected ? 1 : 0) - (a.s.connected ? 1 : 0) || a.i - b.i);
  return list.map((x) => ({ target: x.t, status: x.s }));
}

/**
 * CapacityGauge's numbers from a capacity, or null when free space is unknown.
 * net = what the run takes once done; dip = the extra it holds mid-run;
 * showDip = the dip is at least 1% of the bar (smaller can't be seen).
 */
export function gaugeView(capacity) {
  const c = capacity || {};
  if (c.free == null) return null;
  const net = typeof c.after === "number" ? Math.max(0, c.free - c.after) : Math.max(0, (c.needed || 0) - (c.freeable || 0));
  const after = typeof c.after === "number" ? c.after : c.free - net;
  const lowest = typeof c.lowest === "number" ? Math.min(c.lowest, after) : after;
  const dip = Math.max(0, Math.min(after, c.free) - lowest);
  const total = c.total && c.total > 0 ? c.total : c.free;
  const used = c.total ? Math.max(0, c.total - c.free) : 0;
  return {
    free: c.free,
    total,
    used,
    net,
    dip,
    after,
    lowest,
    reserve: c.reserve || 0,
    short: c.fits === false,
    shortBy: c.shortBy || 0,
    showDip: c.fits !== false && dip > 0 && dip >= total * 0.01,
  };
}

/** Review's read-only "Planned with…" line (design D6). */
export function plannedWithLine(policy, tn) {
  const p = policy || {};
  const removals = DELETE_POLICY_PLANNED[p.deletePolicy] || DELETE_POLICY_PLANNED.guarded;
  const pc = `${((HASH_MODE_COPY[p.hashMode] || HASH_MODE_COPY.cached).label || "Cached").toLowerCase()} PC files`;
  const dev =
    p.verifyMode === "read-back"
      ? `${tn} read back`
      : p.verifyMode === "adopt-size"
        ? `matching sizes on ${tn} trusted`
        : `${tn}'s sync history`;
  return `Planned with ${removals}, ${pc}, and ${dev}.`;
}

/** "14:32" for an epoch ms (the user's clock). */
export function clock(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** Writes that landed in a run summary; older summaries without `files` fall back to filesDone. */
function _landed(s) {
  return s.files && typeof s.files === "object" ? s.files.done || 0 : s.filesDone || 0;
}

function count(n, one, many) {
  return `${n} ${n === 1 ? one : many || `${one}s`}`;
}

/**
 * The last-run card's content (spec §5, mockup LAST_RUNS) for a `lastRun`
 * record ({runId, phase, status, at, summary}). `refusal` is set when an
 * apply was refused at start (U2-13: blocked/free-space live on the job).
 */
export function lastRunView(run, tn, titleOf) {
  if (!run) return null;
  const s = run.summary || {};
  const phase = run.phase || "apply";
  const name = (k) => (titleOf && titleOf(k)) || String(k || "").replace(/^pc:/, "");
  const deleted = (s.deletes && s.deletes.deleted) || 0;
  const bytes = formatBytes(s.bytesDone || 0);
  let tone = "ok";
  let icon = "check";
  let line = "";
  let note = "";
  let action = null;
  if (run.status === "completed") {
    if (phase === "prune") line = `${count((s.prune && s.prune.removed) || deleted, "folder")} removed`;
    else if (phase === "rename") line = s.reason === "same-name" ? "The folder already had that name" : "Folder renamed";
    // executor.js verify counts series folders in filesDone.
    else if (phase === "verify") line = `${count(s.filesDone || 0, "series", "series")} checked`;
    // Files that LANDED (service.js _runSummary `files` by outcome):
    // filesDone also counts skips, so a run that left files read as copying
    // them (plan A1b, review F7).
    else line = `${count(_landed(s), "file")} · ${bytes} copied${deleted ? ` · ${deleted} removed` : ""}`;
    note = `Finished ${clock(run.at)}`;
    if (s.reason === "out-of-room") {
      tone = "warn";
      icon = "alert-triangle";
      action = "recheck-space";
    }
  } else if (run.status === "cancelled") {
    tone = "muted";
    icon = "stop";
    line = `${_landed(s)} of ${count(s.filesTotal || 0, "file")} copied (${bytes}) before you cancelled`;
    note = `${clock(run.at)} · finished files stay on ${tn} and count as synced`;
  } else if (run.status === "disconnected") {
    tone = "warn";
    icon = "unlink";
    line = `${_landed(s)} of ${count(s.filesTotal || 0, "file")} copied`;
    note = `${clock(run.at)} · plug it back in and the plan picks up from there`;
    action = "recheck";
  } else {
    tone = "bad";
    icon = "x";
    const why = s.reason ? jobReasonCopy(s.reason, tn) : s.refusal ? null : s.message;
    const at = s.failed && s.failed[0] ? ` at ${s.failed[0].folder} / ${s.failed[0].name}` : "";
    line = why ? `${why[0].toUpperCase()}${why.slice(1)}${at}` : s.refusal ? "Start was refused" : "It stopped";
    note = `${clock(run.at)}${_landed(s) ? ` · ${count(_landed(s), "file")} copied before it stopped` : ""}`;
    action = s.reason === "no-space" ? "recheck-space" : "recheck";
  }
  const skippedSeries = (s.skippedSeries || []).map((x) => `${name(x.seriesKey || x.folder)} (${jobReasonCopy(x.reason, tn)})`);
  const skippedFiles = Object.entries(s.skippedReasons || {}).map(([r, n]) => `${count(n, "file")} ${skipReasonCopy(r, tn)}`);
  // Reviewed work the apply never ran (service.js H.apply `dropped`,
  // reconcileForApply) and prune folders that weren't orphaned any more
  // (`notOrphaned`): both were in the review, so their absence must show.
  const droppedBy = {};
  for (const d of s.dropped || []) droppedBy[d.reason] = (droppedBy[d.reason] || 0) + 1;
  const droppedLines = Object.entries(droppedBy).map(([r, n]) => `${count(n, "reviewed change")} not run: ${DROPPED_REASON_COPY[r] || r}`);
  const notOrphaned = (s.notOrphaned || []).length ? [`${count(s.notOrphaned.length, "folder")} kept, no longer unused: ${s.notOrphaned.join(", ")}`] : [];
  const skipped = [...(skippedSeries.length ? [`${count(skippedSeries.length, "series", "series")} skipped: ${skippedSeries.join(", ")}`] : []), ...skippedFiles, ...droppedLines, ...notOrphaned];
  return {
    tone,
    icon,
    title: lastRunTitle(phase, run.status, tn, s.reason),
    line,
    note,
    skipped,
    action,
    refusal: s.refusal && s.refusal.code ? s.refusal : null,
  };
}

/**
 * Lane fill (spec §6 transfer lane) for every series of a running apply:
 * the current lane from progress.lane, and 100 for lanes it already left.
 * `memo` is {runId, done:Set} kept by the caller across progress events.
 */
export function laneFills(job, memo) {
  if (!job || job.state !== "running" || job.phase !== "apply") return { memo: null, fills: {} };
  const m = memo && memo.runId === job.runId ? memo : { runId: job.runId, done: new Set(), cur: null };
  const lane = job.progress && job.progress.lane;
  const fills = {};
  let next = m;
  if (lane && lane.seriesKey) {
    if (m.cur && m.cur !== lane.seriesKey && !m.done.has(m.cur)) next = { ...m, done: new Set([...m.done, m.cur]) };
    if (next.cur !== lane.seriesKey) next = { ...next, cur: lane.seriesKey };
    fills[lane.seriesKey] = lane.total > 0 ? Math.round((lane.landed / lane.total) * 100) : 0;
  }
  for (const k of next.done) fills[k] = 100;
  return { memo: next, fills };
}

/** A loss tick's identity: the sorted op ids it was ticked over. */
export function lossSig(ids) {
  return (ids || []).map(String).sort().join("\n");
}

/**
 * Is series `l`'s loss tick still valid? `acks.losses` is a Map(seriesKey →
 * lossSig at tick time); it counts only while the series' CURRENT loss ids
 * are exactly those (U2-2, review R3): a selection change that adds a loss
 * to a ticked series re-opens its tick instead of riding on it.
 */
export function lossAcked(acks, l) {
  const m = acks && acks.losses;
  return !!m && typeof m.get === "function" && m.get(l.seriesKey) === lossSig(l.lossOpIds);
}

/**
 * Review footer state (spec §4 Review): which acknowledgements are still
 * open. `acks` = {losses:Map<seriesKey, lossSig>, massDelete:number|null,
 * massRepush:number|null} as ticked; a mass tick counts only while it equals
 * the review's current count (U2-2: the service checks the same).
 * viewSelSeq: the pushed plan view's selSeq; a review answered before the
 * latest selection change landed (review.selSeq behind it) can't start
 * (U2-11, review R6). changedNotice: an undismissed "the plan changed"
 * notice (R7). updating: the plan view's `updating` (a decision landing, no
 * full plan owed), which words a stale gate "updating" instead.
 */
export function reviewGate(review, acks, { stale, updating, settled, busy, viewGen, viewSelSeq, changedNotice } = {}) {
  if (!review || !review.ok) return { left: 0, canStart: false, why: "loading" };
  const a = acks || {};
  let left = 0;
  for (const l of review.losses || []) if (!lossAcked(a, l)) left += 1;
  if (review.massDelete && review.massDelete.required && a.massDelete !== review.massDelete.count) left += 1;
  if (review.massRepush && review.massRepush.required && a.massRepush !== review.massRepush.count) left += 1;
  const files = workCount(review.totals);
  const selBehind = viewSelSeq != null && review.selSeq !== viewSelSeq;
  const why = stale || review.stale
    ? updating
      ? "updating"
      : "stale"
    : busy || review.busy
      ? "busy"
      : !settled
        ? "saving"
        : (viewGen != null && viewGen !== review.gen) || selBehind
          ? "changed"
          : changedNotice
            ? "notice"
            : review.capacity && review.capacity.fits === false
            ? "space"
            : !files
              ? "empty"
              : left
                ? "acks"
                : null;
  return { left, canStart: !why, why };
}

/**
 * What a review SHOWS the user, as a string: every offered op with its tick,
 * size and blockers, the losses, the mass counts, rebinds, the policy and
 * whether it fits. Two reviews with equal signatures ask the user the same
 * question, so a re-plan that only bumps `gen` (a library notify or settle
 * re-plan that found nothing new) must not raise "The plan changed" or clear
 * the ticks: found by the tab E2E, where such a re-plan landed between the
 * loss tick and Start. ReviewDialog keys both on this (grep reviewSig).
 * `asOf`, `gen`, `selSeq` and capacity bytes are left out on purpose: they
 * move on every plan without changing what Start would do.
 */
export function reviewSig(review) {
  if (!review || !review.ok) return null;
  const ops = [];
  for (const g of review.groups || []) {
    for (const o of g.ops || []) ops.push([g.kind, g.reason, o.opId, !!o.selected, o.size, (o.blockedBy || []).length, !!o.loss]);
  }
  const md = review.massDelete || {};
  const mr = review.massRepush || {};
  return JSON.stringify([
    ops,
    (review.losses || []).map((l) => [l.seriesKey, lossSig(l.lossOpIds)]),
    [!!md.required, md.count || 0, !!mr.required, mr.count || 0],
    review.rebinds || null,
    review.policy || null,
    !!(review.capacity && review.capacity.fits !== false),
    !!review.needsVerify,
  ]);
}

/** The apply payload for a review and its ticks (service.js H.apply / _ackArgs). */
export function applyPayload(targetId, review, acks) {
  const a = acks || {};
  const ackLosses = [];
  // Only a still-valid tick sends ids, and they are the ids it was ticked
  // over (equal to the current ones by lossAcked), never a later list.
  for (const l of review.losses || []) if (lossAcked(a, l)) ackLosses.push(...l.lossOpIds);
  const out = { targetId, gen: review.gen, ackLosses };
  // The selection the review showed (U2-11): the service refuses
  // plan-changed when the target's selSeq has moved past it. null is sent
  // too: it is the "no selection change yet" value the service starts from
  // (service.js _rt), and dropping it skipped the check for a first review.
  if (review.selSeq !== undefined) out.selSeq = review.selSeq;
  if (review.massDelete && review.massDelete.required && a.massDelete != null) out.ackMassDelete = a.massDelete;
  if (review.massRepush && review.massRepush.required && a.massRepush != null) out.ackMassRepush = a.massRepush;
  return out;
}

/**
 * Series-detail rows grouped by action (spec §4 Series detail), in display
 * order. Decision groups (replace, removals, losses) open by default.
 */
const DETAIL_GROUPS = [
  { id: "push", kinds: ["push", "update"], tone: "push" },
  { id: "replace", kinds: ["replace"], tone: "warn", open: true },
  { id: "rename", kinds: ["rename"], tone: "other" },
  { id: "delete", kinds: ["delete"], tone: "delete", open: true },
  { id: "settling", kinds: ["settling"], tone: "absent" },
  { id: "in-sync", kinds: ["in-sync"], tone: "synced" },
  { id: "device-only", kinds: ["device-only"], tone: "other" },
];
export function detailGroups(rows) {
  const out = [];
  for (const g of DETAIL_GROUPS) {
    const list = (rows || []).filter((r) => g.kinds.includes(r.kind));
    if (!list.length) continue;
    const ops = list.filter((r) => r.opId);
    const sel = ops.filter((r) => r.selected);
    out.push({
      ...g,
      rows: list,
      count: list.length,
      bytes: list.reduce((n, r) => n + (r.size || 0), 0),
      selectedBytes: sel.reduce((n, r) => n + (r.size || 0), 0),
      box: ops.length === 0 ? null : sel.length === 0 ? false : sel.length === ops.length ? true : "mixed",
      opIds: ops.map((r) => r.opId),
      open: !!g.open || list.some((r) => r.loss || r.lossIfSelected),
    });
  }
  return out;
}

/** A detail group's toggle as set-selection changes (per op: these are one series' rows). */
export function groupChanges(group, selected) {
  return group.rows.filter((r) => r.opId && !!r.selected !== selected).map((r) => ({ opId: r.opId, selected }));
}

/** The cover image path for a PC folder name, from the Library's entries. */
export function coverIndex(entries) {
  const m = new Map();
  for (const e of entries || []) {
    const folder = e && typeof e.folderPath === "string" ? e.folderPath.split(/[\\/]/).filter(Boolean).pop() : null;
    const p = e && (e.thumbPath || e.coverImagePath);
    if (folder && p) m.set(folder, p);
  }
  return m;
}

/** library entries → Map(folder name → the series folder's full PC path). */
export function pcPathIndex(entries) {
  const m = new Map();
  for (const e of entries || []) {
    const folder = e && typeof e.folderPath === "string" ? e.folderPath.split(/[\\/]/).filter(Boolean).pop() : null;
    if (folder) m.set(folder, e.folderPath);
  }
  return m;
}

/**
 * The detail header's "PC → target" line (spec §4 series detail). `root` is
 * the target's stored root (canonical, from browse-remote), joined with the
 * root's own separator so an adb path stays POSIX and a folder target stays
 * native. A series not on the target yet says so instead of inventing a path.
 */
export function seriesPathLine({ folder, deviceFolder, pcPath, root, tn }) {
  const pc = pcPath || folder || "";
  if (!deviceFolder) return `${pc} → (not on ${tn} yet)`;
  if (!root) return `${pc} → ${deviceFolder}`;
  const sep = root.includes("/") || !root.includes("\\") ? "/" : "\\";
  return `${pc} → ${root.replace(/[\\/]+$/, "")}${sep}${deviceFolder}`;
}

/** A chapter label as the UI prints it: "7" → "Ch.007", "10.5" → "Ch.010.5"; other labels as-is. */
export function chLabel(label) {
  if (label == null || label === "") return "";
  const m = String(label).match(/^(-?)(\d+)(\.\d+)?$/);
  return m ? `Ch.${m[1]}${m[2].padStart(3, "0")}${m[3] || ""}` : String(label);
}
