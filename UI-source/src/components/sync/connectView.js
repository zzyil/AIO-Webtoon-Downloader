// ============================================================
// connectView — the connect prompt's cards and the rail badge (UI pass U3)
//
// Pure functions over what main pushes (hooks/useDeviceSync.js state), so
// they run in node: tools/_test_device_sync_view.js bundles this file with
// esbuild. No React, no I/O.
//
// THE CARD IS LIVE. Main's PromptMachine (electron/sync/monitor.js) only
// decides whether a card is open: `pending` (connections still planning),
// `prompts` (shown cards, each with the frozen promptCard of its connect
// plans) and the withdraw that closes one. What a card SAYS comes from the
// live per-target plan view (plans[targetId]) whenever its summary is at
// least as new as the card (the gen floor), so a deselection or a re-plan
// after a download shows at once; the frozen card fills in until then.
// Design and decisions: sync-temp/plans/device-sync-ui-pass.md, "Phase U3
// design" (U3-9..U3-14).
//
// Consumers: components/sync/ConnectPrompt.jsx (cards), App.jsx (rail
// badge). Depends on lib/utils formatBytes, copy.js, syncView.js rowView /
// targetStatus.
// ============================================================

import { formatBytes } from "@/lib/utils";
import { deviceLabel, listingSuspectCopy, refusalTitle, jobReasonCopy } from "./copy";
import { rowView, targetStatus } from "./syncView";

// A Preparing card or a spinner card appears only after this long, so a
// connect plan that lands in a second never flashes one (U3-11).
export const PREPARE_GRACE_MS = 2000;
// At most this many cards stack in the corner; the rest are "+N more".
export const MAX_CARDS = 2;

// A refused target's few words on a multi-target card's row (the full
// refusalTitle is the row's tooltip and the single-target body).
const ATTENTION_SHORT = Object.freeze({
  "record-mismatch": "a different sync history",
  // device-listing-suspect: copy.js listingSuspectCopy(r).short, by cause.
  "library-missing": "the library isn't reachable",
  blocked: "waiting on you",
  failed: "couldn't check it",
});

const DEVICE_CARD_STATES = new Set(["unauthorized", "no-permissions", "authorizing", "connecting"]);
const SPINNER_STATES = new Set(["authorizing", "connecting"]);

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many || `${one}s`}`;
}

function pct(done, total) {
  return total > 0 ? Math.max(0, Math.min(100, Math.floor((100 * (done || 0)) / total))) : null;
}

/** The numbers a card line needs, from a live summary or a frozen promptSummary. */
function liveNumbers(summary) {
  const t = summary.totals || {};
  return {
    series: t.series || 0,
    files: t.files || 0,
    bytes: t.bytes || 0,
    deletions: (t.delete && t.delete.count) || 0,
    renames: (t.rename && t.rename.count) || 0,
    losses: summary.losses || 0,
    needsVerify: summary.needsVerify || 0,
  };
}

function frozenNumbers(s) {
  return {
    series: s.series || 0,
    files: s.files || 0,
    bytes: s.bytes || 0,
    deletions: s.deletions || 0,
    renames: s.renames || 0,
    losses: s.losses || 0,
    needsVerify: s.verifyFolders || 0,
  };
}

function hasWork(n) {
  return n.files > 0 || n.deletions > 0 || n.renames > 0 || n.needsVerify > 0;
}

/**
 * What a running plan or check on the target is doing, as one short line, or
 * null. Job progress steps are prepare.js / executor phases (service.js
 * _progressOf: progress.step + filesDone/filesTotal).
 */
export function workLine(job, targetId) {
  if (!job || job.state !== "running" || job.targetId !== targetId) return null;
  // A refresh (the top-up after a review decision) runs the same steps; it
  // shows here only while there's no plan to show (after a needs-mode pick).
  if (job.phase !== "plan" && job.phase !== "verify" && job.phase !== "refresh") return null;
  const p = job.progress || {};
  const n = pct(p.filesDone, p.filesTotal);
  const at = n == null ? "" : ` (${n}%)`;
  switch (p.step) {
    case "verify": {
      // executor.js verify: filesDone/filesTotal count series folders and the
      // bytes in them; bytes predict the time left, a big series being slow.
      const b = pct(p.bytesDone, p.bytesTotal);
      const v = b != null ? b : n;
      const of = p.filesTotal > 0 ? ` (${p.filesDone || 0} of ${p.filesTotal} series${v != null ? `, ${v}%` : ""})` : "";
      return { text: `Reading back what's already there${of}`, pct: v, current: p.current && p.current.folder ? p.current.folder : null };
    }
    case "hashing":
      return { text: `Fingerprinting PC files${at}`, pct: n };
    case "listing":
      return { text: `Listing what's on it${at}`, pct: n };
    case "planning":
      return { text: "Working out what to copy", pct: null };
    default:
      return { text: "Checking what's there", pct: null };
  }
}

/**
 * One target's state on a card, live where it can be (U3-9). kind:
 *   work      a plan or read-back runs on it now ({text, pct})
 *   setup     needs-mode: a read-back is needed first ({bytes, folders, firstSync})
 *   attention refused or failed ({title})
 *   ready     something to copy / remove ({n})
 *   done      nothing left to do
 *   unknown   no plan yet (the card arrived before its target's plan push)
 */
export function targetLine({ targetId, name, entry, attention, plan, job }) {
  const tn = name || "this target";
  const w = workLine(job, targetId);
  if (w) return { targetId, name: tn, kind: "work", ...w };
  const floor = entry && entry.gen != null ? entry.gen : -Infinity;
  const pv = plan || {};
  const liveOk = pv.summary && (pv.summary.gen == null || pv.summary.gen >= floor);
  // A refusal on the plan view is newer than the card's: _refused nulls the
  // summary, and _adopt clears the refusal.
  const refusal = pv.refusal || (!liveOk && attention ? attention : null);
  if (refusal && refusal.code === "needs-mode") {
    return { targetId, name: tn, kind: "setup", bytes: refusal.bytes == null ? null : refusal.bytes, folders: Array.isArray(refusal.folders) ? refusal.folders.length : null, firstSync: !!refusal.firstSync };
  }
  if (refusal) {
    const title = refusal.code === "failed" ? `Couldn't check ${tn}: ${jobReasonCopy(refusal.reason, tn) || refusal.message || "it stopped"}` : refusalTitle(refusal.code, tn, refusal) || refusal.message || "Needs attention";
    const short = refusal.code === "device-listing-suspect" ? listingSuspectCopy(refusal, tn).short : ATTENTION_SHORT[refusal.code];
    return { targetId, name: tn, kind: "attention", title, short: short || "needs a look" };
  }
  const n = liveOk ? liveNumbers(pv.summary) : entry ? frozenNumbers(entry) : null;
  if (!n) return { targetId, name: tn, kind: "unknown" };
  return { targetId, name: tn, kind: hasWork(n) ? "ready" : "done", n, stale: liveOk ? !!pv.stale : true };
}

function copyRows(n) {
  const rows = [];
  if (n.files) rows.push({ k: "Copy", v: `${plural(n.files, "chapter")} in ${plural(n.series, "series", "series")}`, s: formatBytes(n.bytes) });
  if (n.deletions) rows.push({ k: "Remove", v: `${plural(n.deletions, "file")} (review needed)`, s: "" });
  if (n.renames) rows.push({ k: "Rename", v: plural(n.renames, "folder"), s: "" });
  return rows;
}

function summaryText(l) {
  switch (l.kind) {
    case "work":
      return { v: l.text, s: "", warn: false };
    case "setup":
      return { v: l.firstSync ? "new here: set up first" : "needs a check first", s: l.bytes != null ? `~${formatBytes(l.bytes)}` : "", warn: true };
    case "attention":
      return { v: `needs attention: ${l.short}`, tip: l.title, s: "", warn: true };
    case "ready": {
      const parts = [];
      if (l.n.files) parts.push(`${plural(l.n.files, "chapter")} · ${formatBytes(l.n.bytes)}`);
      if (l.n.deletions) parts.push(`${plural(l.n.deletions, "removal")} to review`);
      if (l.n.renames && !parts.length) parts.push(plural(l.n.renames, "folder rename"));
      return { v: parts.join(" · ") || "changes to review", s: "", warn: false };
    }
    case "done":
      return { v: "up to date", s: "", warn: false };
    default:
      return { v: "checking…", s: "", warn: false };
  }
}

function targetName(targets, id) {
  const t = (targets || []).find((x) => x.id === id);
  return (t && t.name) || null;
}

function headName({ serial, targetIds, targets, devices }) {
  if (targetIds.length === 1) return targetName(targets, targetIds[0]) || "Your target";
  // Several targets share a card only on one tablet (a folder target is its
  // own connection): name the tablet.
  const d = serial ? (devices || []).find((x) => x.serial === serial) : null;
  if (d) return deviceLabel(d);
  return serial ? "Your tablet" : targetIds.map((id) => targetName(targets, id)).filter(Boolean).join(" and ") || "Your targets";
}

/** A shown prompt → its card (kind 'prompt'). */
function promptCardView(p, ctx) {
  const card = p.card || {};
  const entries = new Map((card.targets || []).map((s) => [s.targetId, s]));
  const atts = new Map((card.attention || []).map((a) => [a.targetId, a]));
  const ids = (p.targetIds || []).filter((id) => (ctx.targets || []).some((t) => t.id === id));
  const lines = ids.map((id) =>
    targetLine({ targetId: id, name: targetName(ctx.targets, id), entry: entries.get(id), attention: atts.get(id), plan: (ctx.plans || {})[id], job: ctx.job }),
  );
  const head = headName({ serial: p.serial, targetIds: ids, targets: ctx.targets, devices: ctx.devices });
  const single = lines.length === 1 ? lines[0] : null;
  let rows;
  let body = null;
  let note = null;
  let progress = null;
  let title = `${head} connected`;
  if (single) {
    if (single.kind === "setup") {
      if (single.firstSync) title = `${head} is new here`;
      rows = [{ k: "Check", v: single.folders != null ? `${plural(single.folders, "folder")} already on it` : "What's already on it", s: single.bytes != null ? `~${formatBytes(single.bytes)}` : "" }];
      body = single.firstSync
        ? `Before the first sync, every chapter already on ${head} is read back once so nothing is copied twice.`
        : `${head} can't fingerprint files itself, so what's on it is read back once before syncing.`;
    } else if (single.kind === "work") {
      rows = [];
      body = single.text;
      progress = single.pct;
    } else if (single.kind === "attention") {
      rows = [];
      body = single.title;
    } else if (single.kind === "ready") {
      rows = copyRows(single.n);
      if (single.n.deletions) note = `${plural(single.n.deletions, "file")} would be removed; Review shows which.`;
      if (single.n.losses) note = `${note ? `${note} ` : ""}${plural(single.n.losses, "chapter")} would be on neither side afterwards.`;
    } else if (single.kind === "done") {
      rows = [];
      body = `Nothing left to copy to ${head}.`;
    } else {
      rows = [];
      body = "Checking what's there…";
    }
  } else {
    rows = lines.map((l) => ({ k: l.name, targetId: l.targetId, ...summaryText(l) }));
  }
  // The primary button (design D2: never Sync now). Set up wins for a single
  // target that needs a read-back; otherwise Review lands on the first target
  // with changes (U3-2), or the first needing attention ("Open Sync").
  let primary = null;
  if (single && single.kind === "setup") {
    const gb = single.bytes != null ? ` (reads ~${formatBytes(single.bytes)})` : "";
    primary = { action: "setup", targetId: single.targetId, label: `${single.firstSync ? "Set up" : "Check"}${gb}` };
  } else {
    const ready = lines.find((l) => l.kind === "ready");
    const attn = lines.find((l) => l.kind === "setup" || l.kind === "attention");
    if (ready) primary = { action: "review", targetId: ready.targetId, label: "Review" };
    else if (attn) primary = { action: "review", targetId: attn.targetId, label: "Open Sync" };
  }
  const tone = single && single.kind === "attention" ? "bad" : "default";
  return { key: p.promptId, kind: "prompt", promptId: p.promptId, targetIds: ids, tone, title, rows, body, note, progress, primary, lines };
}

/** A connection still planning → a Preparing card, once past the grace. */
function preparingCardView(pd, ctx) {
  const ids = (pd.targetIds || []).filter((id) => (ctx.targets || []).some((t) => t.id === id));
  const head = headName({ serial: pd.serial, targetIds: ids, targets: ctx.targets, devices: ctx.devices });
  const running = ids.map((id) => workLine(ctx.job, id)).find(Boolean);
  return {
    key: pd.promptId,
    kind: "preparing",
    promptId: pd.promptId,
    targetIds: ids,
    tone: "default",
    title: `${head} connected`,
    rows: [],
    body: running ? `Preparing: ${running.text.charAt(0).toLowerCase()}${running.text.slice(1)}` : "Preparing: waiting for another sync job to finish",
    progress: running ? running.pct : null,
    primary: null,
  };
}

/** A configured tablet stuck before `device` → its card (U3-12). */
function deviceCardView(d, ids, ctx) {
  const st = d.stateKind || d.state;
  const head = ids.length === 1 ? targetName(ctx.targets, ids[0]) || deviceLabel(d) : deviceLabel(d);
  const base = { key: `dev:${d.serial}:${st}`, kind: "device", serial: d.serial, state: st, targetIds: ids, rows: [], note: null, progress: null };
  switch (st) {
    case "unauthorized":
      return { ...base, tone: "bad", title: `${head} needs permission`, body: `Unlock ${head} and tap Allow on the "Allow USB debugging?" prompt. Tick "Always allow from this computer" so it isn't asked again.`, primary: { action: "recheck", label: "I've allowed it" } };
    case "no-permissions":
      return { ...base, tone: "bad", title: `${head} can't be reached`, body: "Windows can't open the USB connection. Install the tablet maker's USB driver, then unplug and replug the cable.", primary: { action: "recheck", label: "Re-check" } };
    case "authorizing":
      return { ...base, tone: "default", title: head, spinner: "Waiting for you to allow USB debugging on the tablet…", body: null, primary: null };
    default:
      return { ...base, tone: "default", title: head, spinner: "Connecting…", body: null, primary: null };
  }
}

/**
 * Every card the corner shows, in order: tablets that need a hand first, then
 * shown prompts (oldest first), then connections still preparing. Returns
 * {cards, more}: at most MAX_CARDS cards, `more` = how many didn't fit.
 *
 * ctx: {prompts, pending, plans, job, devices, targets, promptOnConnect,
 *   now, deviceSince: {key → ms first seen}, dismissedDevices: Set<key>,
 *   hiddenTargetId}. `hiddenTargetId` is the target the Sync tab shows now:
 *   its cards would cover the tab's own dock (U3-13), so they wait, unanswered.
 */
export function connectCards(ctx) {
  const now = ctx.now == null ? Date.now() : ctx.now;
  const targets = ctx.targets || [];
  const hidden = (ids) => ctx.hiddenTargetId != null && ids.includes(ctx.hiddenTargetId);
  const out = [];
  if (ctx.promptOnConnect !== false) {
    for (const d of ctx.devices || []) {
      const st = d.stateKind || d.state;
      if (!DEVICE_CARD_STATES.has(st)) continue;
      const ids = targets.filter((t) => t.kind === "adb" && t.serial === d.serial).map((t) => t.id);
      if (!ids.length) continue; // only tablets that are targets (U3-12)
      const key = `dev:${d.serial}:${st}`;
      if (ctx.dismissedDevices && ctx.dismissedDevices.has(key)) continue;
      const since = ctx.deviceSince ? ctx.deviceSince[key] : null;
      if (SPINNER_STATES.has(st) && (since == null || now - since < PREPARE_GRACE_MS)) continue;
      if (hidden(ids)) continue;
      out.push(deviceCardView(d, ids, ctx));
    }
  }
  for (const p of ctx.prompts || []) {
    const c = promptCardView(p, ctx);
    if (!c.targetIds.length || hidden(c.targetIds)) continue;
    out.push(c);
  }
  for (const pd of ctx.pending || []) {
    if (pd.since != null && now - pd.since < PREPARE_GRACE_MS) continue;
    const c = preparingCardView(pd, ctx);
    if (!c.targetIds.length || hidden(c.targetIds)) continue;
    out.push(c);
  }
  return { cards: out.slice(0, MAX_CARDS), more: Math.max(0, out.length - MAX_CARDS) };
}

/** Is a last run worth the red dot: failed or cut off, not dismissed, not a plan-changed block. */
function lastRunAlarms(lastRun, dismissedRunId) {
  if (!lastRun || lastRun.runId === dismissedRunId) return false;
  if (lastRun.status !== "failed" && lastRun.status !== "disconnected") return false;
  const r = lastRun.summary && lastRun.summary.refusal;
  if (r && r.code === "blocked" && Array.isArray(r.blocking) && r.blocking.length && r.blocking.every((b) => b.kind === "plan-changed")) return false;
  return true;
}

/**
 * The Sync rail button's badge (U3-14). {count, dot: 'red'|'pulse'|null}.
 *   count  distinct series with ticked changes on CONNECTED targets (a stale
 *          summary still counts, so it doesn't blink across re-plans);
 *          capped for display by the caller ("99+").
 *   red    a connected target's plan refused (except needs-mode, which is a
 *          question, not a fault), a plan that failed with nothing to show,
 *          a failed / cut-off last run not dismissed, or a configured
 *          tablet that is unauthorized / has no USB permission.
 *   pulse  a job runs that isn't an automatic plan or a refresh (a user's
 *          Re-check does).
 * Red wins over pulse; the count shows beside either.
 */
export function railBadge({ targets, plans, devices, folderPresence, job, dismissed }) {
  const keys = new Set();
  let red = false;
  for (const t of targets || []) {
    if (t.kind === "adb") {
      const d = (devices || []).find((x) => x.serial === t.serial);
      const st = d && (d.stateKind || d.state);
      if (st === "unauthorized" || st === "no-permissions") red = true;
    }
    if (!targetStatus(t, devices, folderPresence).connected) continue;
    const pv = (plans || {})[t.id] || {};
    if (pv.refusal && pv.refusal.code !== "needs-mode") red = true;
    if (!pv.summary && !pv.refusal && pv.lastPlan && pv.lastPlan.status === "failed") red = true;
    if (lastRunAlarms(pv.lastRun, dismissed ? dismissed[t.id] : null)) red = true;
    for (const r of (pv.summary && pv.summary.series) || []) if (rowView(r).selected > 0) keys.add(r.seriesKey);
  }
  const running = job && job.state === "running";
  // A refresh is always automatic (a decision's top-up), so it never pulses.
  const pulse = !!running && !((job.phase === "plan" && job.auto) || job.phase === "refresh");
  return { count: keys.size, dot: red ? "red" : pulse ? "pulse" : null };
}

/** The pill's text: the count, or "99+". */
export function badgeText(count) {
  return count > 99 ? "99+" : String(count);
}
