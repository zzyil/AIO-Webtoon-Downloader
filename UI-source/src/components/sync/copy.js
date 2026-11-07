// ============================================================
// DEVICE SYNC COPY — every machine code the sync backend sends, as words
//
// One map per kind of code (reader presets, adb tracking, tablet states,
// refusal codes, …). Every Sync surface reads its wording from here; none
// spells a code out itself, so a new backend code shows up as one missing key
// in the drift test instead of as raw text on screen.
//
// Copy rules (approved design spec §3, sync-temp/design/approved/spec.md):
// sentence case; never a bare "device" — name the target, and where a name
// can't fit say "there" with the full name in a tooltip. Functions take the
// target's name for that reason.
//
// KEEP IN SYNC with the backend lists it covers (electron/sync/profiles.js
// PROFILE_IDS, contract.js REFUSAL_CODES, …). Drift guard:
// tools/_test_device_sync_copy.js, which imports this file as an ES module —
// so it must stay import-free (plain data and functions).
// ============================================================

// ── Reader presets (profiles.js PROFILES; user decision 2026-10-16) ──
// A preset fixes what is copied; only Custom exposes the file options.
export const PROFILE_COPY = Object.freeze({
  everything: {
    label: "Everything",
    hint: "Copies every chapter file (CBZ, ZIP, CBR, RAR, EPUB and PDF), plus cover.jpg and details.json, and hides covers from the gallery.",
  },
  komikku: {
    label: "Komikku compatible",
    hint: "For Komikku and Mihon's local source: archives, cover.jpg and details.json. PDFs aren't copied, and a ~ in a chapter number or a missing cover.jpg is flagged so you can fix it on the PC.",
  },
  pdf: {
    label: "PDF only",
    hint: "Copies PDF files and cover.jpg, and hides covers from the gallery.",
  },
  custom: {
    label: "Custom",
    hint: "You choose the file types, extra files, .nomedia, folder names and clean-up below.",
  },
});
export const PROFILE_ORDER = Object.freeze(["everything", "komikku", "pdf", "custom"]);

// ── Per-target fields ──
export const DELETE_POLICY_COPY = Object.freeze({
  guarded: { label: "Guarded", hint: "Removes chapters only after you confirm each series in Review." },
  "add-only": { label: "Add only", hint: "Never removes anything from the target." },
  "auto-replace": {
    label: "Auto-replace",
    hint: "Like Guarded, and also ticks swapping the target's other copies of a chapter for the PC's.",
  },
});
// The order every Removals control lists them (TargetBar, TargetEditor,
// Settings → Sync); the copy test checks it covers profiles.js DELETE_POLICIES.
export const DELETE_POLICY_ORDER = Object.freeze(["guarded", "auto-replace", "add-only"]);
// Review's "Planned with…" phrase per policy (syncView plannedWithLine).
export const DELETE_POLICY_PLANNED = Object.freeze({
  guarded: "guarded removals",
  "auto-replace": "auto-replace removals",
  "add-only": "removals off (add only)",
});
export const HASH_MODE_COPY = Object.freeze({
  cached: { label: "Cached", hint: "Reuses fingerprints from the last scan." },
  rehash: { label: "Re-hash", hint: "Reads every PC file again on each check." },
});
export const NAMING_POLICY_COPY = Object.freeze({
  "strip-hid": { label: "Without site id", example: "One Piece (hid=8f2) → One Piece" },
  "as-is": { label: "Same as the PC folder", example: "One Piece (hid=8f2) → One Piece (hid=8f2)" },
  title: { label: "Series title", example: "folder name → the title AIO recorded" },
});
export const SELECTION_MODE_COPY = Object.freeze({
  "all-except": "All, except excluded",
  only: "Only selected",
});
export const VERIFY_MODE_COPY = Object.freeze({
  "read-back": { label: "Read back", hint: "Reads each file back from the target to check it. Slower, exact." },
  "adopt-size": { label: "Trust matching sizes", hint: "Treats a file of the same name and size as already there. Fast." },
});
export const KIND_COPY = Object.freeze({ adb: "Tablet (adb)", folder: "Folder" });

// ── adb (service.js _adbView `tracking`; monitor.js) ──
// tone: ok | info | warn | muted | bad — maps to the token colour of the line.
export function adbTrackingCopy(adb) {
  const a = adb || {};
  // No binary at all: the server error that follows ("no adb binary to start
  // the server with", adb/locate.js) is a consequence, not the news. Only
  // when the hook has loaded something (`adb` non-null) — before that the
  // fields are just absent.
  if (adb && !a.binary && a.tracking !== "tracking") {
    return { tone: "bad", icon: "alert", title: "No adb on this PC", body: "Install Android SDK Platform-Tools, then re-detect, or browse to adb.exe." };
  }
  if (a.error && a.tracking !== "tracking") {
    return { tone: "bad", icon: "alert", title: "adb couldn't start", body: `${a.error}. Pick another adb or re-detect.` };
  }
  switch (a.tracking) {
    case "tracking":
      return { tone: "ok", icon: "ok", title: "Watching for tablets", body: `adb${a.serverVersion ? ` ${formatAdbServer(a.serverVersion)}` : ""} is running. A tablet appears here as soon as you plug it in.` };
    case "connecting":
      return { tone: "info", icon: "spin", title: "Starting adb…", body: "Connecting to the adb server on this PC." };
    case "polling":
      return { tone: "warn", icon: "clock", title: "Checking every 5 s", body: "adb's live tracking isn't available, so the app checks for tablets every few seconds." };
    default:
      return { tone: "muted", icon: "idle", title: "Not watching", body: a.binary ? "adb is idle. It starts when a sync needs it." : "No adb found. Browse to adb.exe or re-detect." };
  }
}
// The server reports its protocol version (41) where `adb version` says 1.0.41.
export function formatAdbServer(v) {
  return typeof v === "number" ? `1.0.${v}` : String(v || "");
}
// adb/locate.js candidate `source` (its @returns lists them).
export const ADB_SOURCE_COPY = Object.freeze({
  configured: "chosen",
  PATH: "on PATH",
  ANDROID_HOME: "ANDROID_HOME",
  ANDROID_SDK_ROOT: "ANDROID_SDK_ROOT",
  LOCALAPPDATA: "Android SDK",
});

// ── Tablet states (adb/wire.js DEVICE_STATES + "no-permissions" = stateKind) ──
export const DEVICE_STATE_COPY = Object.freeze({
  device: { tone: "ok", label: "Connected" },
  unauthorized: { tone: "bad", label: "Allow USB debugging on the tablet" },
  "no-permissions": { tone: "bad", label: "No permission to use this USB device" },
  authorizing: { tone: "info", label: "Waiting for you to allow USB debugging" },
  connecting: { tone: "info", label: "Connecting" },
  offline: { tone: "muted", label: "Offline" },
  detached: { tone: "muted", label: "Detached" },
  bootloader: { tone: "muted", label: "In bootloader" },
  recovery: { tone: "muted", label: "In recovery" },
  rescue: { tone: "muted", label: "In rescue mode" },
  sideload: { tone: "muted", label: "In sideload mode" },
  host: { tone: "muted", label: "Not a tablet" },
});

// ── Refusals a config-op can answer (contract.js REFUSAL_CODES) ──
// Settings renders these inline next to the field that caused them.
export function configRefusalCopy(r, targetName) {
  const tn = targetName || "this target";
  switch (r && r.code) {
    case "version-conflict":
      return "The sync settings changed somewhere else. This screen now shows the current settings; make your change again.";
    case "busy":
      return `${tn} is busy with a sync job. Try again when it finishes.`;
    case "invalid":
      return r.message ? capitalize(r.message) + "." : "That value isn't allowed.";
    case "disabled":
      return "Device Sync is off.";
    case "disconnected":
      return `${tn} isn't connected.`;
    default:
      return (r && r.message) || "That didn't work.";
  }
}

function capitalize(s) {
  const t = String(s || "");
  return t ? t[0].toUpperCase() + t.slice(1) : t;
}

// ── Display helpers ──
/** The label a tablet goes by before it is a target: model, else serial. */
export function deviceLabel(d) {
  if (!d) return "";
  return d.model ? String(d.model).replace(/_/g, "-") : d.serial || "(no serial number)";
}

// ── File options (profiles.js CHAPTER_FORMATS / SIDECAR_NAMES / custom
// defaults). A custom target's null field resolves to these, so the editor
// shows them as the current value. ──
export const CHAPTER_FORMATS = Object.freeze(["cbz", "zip", "cbr", "rar", "epub", "pdf"]);
export const SIDECAR_NAMES = Object.freeze(["cover.jpg", "details.json"]);
export const DEFAULT_CLEANUP_NAMES = Object.freeze([".aio_series.json", ".mangafire_hid", ".series_hid", ".cover.webp"]);
export const CUSTOM_DEFAULTS = Object.freeze({
  formats: CHAPTER_FORMATS,
  sidecars: SIDECAR_NAMES,
  nomedia: true,
  namingPolicy: "strip-hid",
  renameToMatch: false,
  cleanupNames: DEFAULT_CLEANUP_NAMES,
});

// ── Ledger (planner.js SERIES_STATES / HELD_REASONS, analysis.js TAG_KINDS) ──
// null = the state shows no badge. The map is exported so the copy drift
// test (tools/_test_device_sync_copy.js) checks its OWN keys against
// planner.SERIES_STATES.
export const SERIES_STATE_COPY = Object.freeze({
  bound: null,
  "needs-verify": () => "Needs a check",
  new: () => "New series",
  gone: (tn) => `Removed on ${tn}`,
  held: null,
});
// A series row's transient state while a decision lands (syncView.liveState;
// plan sync-temp/plans/device-sync-cheap-replans.md §7). Not planner states,
// so the drift test leaves this map alone.
export const LIVE_STATE_COPY = Object.freeze({
  linking: () => "Linking…",
  verifying: () => "Verifying…",
  "verify-offline": () => "Connect to verify",
});
export function liveStateCopy(state, tn) {
  const f = LIVE_STATE_COPY[state];
  return typeof f === "function" ? f(tn) : null;
}
export function seriesStateCopy(state, tn) {
  const f = SERIES_STATE_COPY[state];
  return typeof f === "function" ? f(tn) : f;
}
export function heldReasonCopy(reason, tn) {
  const there = tn ? `on ${tn}` : "there";
  return {
    excluded: "Excluded",
    fork: "Another library folder holds the same series",
    "empty-pc-folder": "No chapters on your PC",
    "image-only": "Loose images, not chapter files",
    "folder-unreadable": `Couldn't read its folder ${there}`,
    "name-taken": `That folder name is already taken ${there}`,
    "name-invalid": `That folder name isn't allowed ${there}`,
  }[reason];
}
export const TAG_COPY = Object.freeze({
  "pc-ahead": "PC ahead",
  "device-ahead": "Target ahead",
  "pc-gaps": "Gaps on PC",
  "sub-chapter-split": "Split chapters",
  "ch0-asymmetry": "Chapter 0 differs",
  "different-start": "Different start",
  "numbering-mismatch": "Numbering differs",
  "volumes-not-comparable": "Volumes",
  mixed: "Mixed numbering",
});

/**
 * The series detail's note when auto-replace left this series' swaps
 * unticked (planner.js sp.autoHeld: the delete-blocking tags, or mixed).
 */
export function autoHeldCopy(tags, tn) {
  const names = (tags || []).map((t) => TAG_COPY[t] || t);
  if (!names.length) return null;
  return `Auto-replace didn't tick this series' swaps (${names.join(", ")}). Tick the copies on ${tn || "the target"} yourself to replace them.`;
}

/**
 * A series key as words: planner.js pcSeriesKey is `pc:<PC folder>`; the
 * library root's loose files are analysis.js ROOT_SERIES_KEY "(root)".
 */
export function seriesKeyName(key) {
  if (!key) return "";
  if (key === "(root)") return "the library's loose files";
  return key.startsWith("pc:") ? key.slice(3) : key;
}

/**
 * Review's mass-removal confirmation (planner.js evaluateSelection
 * massDelete). It separates what the PC's copy REPLACES from what is simply
 * removed, since "remove 18,000 files" read as a loss when nearly all were
 * swaps (user, 2026-10-27):
 *   overwrites  a same-name copy the PC's file is written over;
 *   swaps       a gated delete: the old copy under another name, removed
 *               only once the PC's copy of that chapter is on the target;
 *   deletes − swaps  removals nothing replaces (extras, extra files, losses).
 * The line ends with the limit crossed: half the chapter files of a series
 * (MASS_RATIO, MASS_MIN) or MASS_TOTAL_DELETES files in one sync.
 */
export function massDeleteCopy(md, tn) {
  const num = (n) => n.toLocaleString("en-US");
  const over = md.overwrites || 0;
  const swaps = md.swaps || 0;
  const replaced = over + swaps;
  const other = Math.max(0, (md.deletes || 0) - swaps);
  const copies = `${num(replaced)} ${replaced === 1 ? "copy" : "copies"}`;
  const files = (n, w = "") => `${num(n)} ${w}${n === 1 ? "file" : "files"}`;
  let title;
  if (replaced && other) title = `Replace ${copies} on ${tn} with the PC's and remove ${files(other, "other ")} in one sync`;
  else if (replaced) title = `Replace ${copies} on ${tn} with the PC's in one sync`;
  else title = `Remove ${files(other)} from ${tn} in one sync`;
  const parts = [];
  if (over) parts.push(`${num(over)} overwritten in place`);
  if (swaps) parts.push(`${num(swaps)} under another name, removed only once the PC's copy is on ${tn}`);
  const split = replaced ? `${parts.join("; ")}. ` : "";
  const why = md.series ? `Half or more of the chapter files in ${num(md.series)} series.` : "200 or more files in one sync.";
  return { title, sub: split + why };
}
// Strip states (planner.js chapterStrip; colours in spec §2), for tooltips.
export const STRIP_STATE_COPY = Object.freeze({
  synced: "on the target",
  push: "will copy",
  warn: "needs a decision",
  delete: "will remove",
  absent: "not copied",
  other: "volumes, ranges or unnumbered files",
});
export function anomalyCopy(kind, tn) {
  const there = tn || "the target";
  return {
    "fork-collision": "Two library folders are the same series",
    "merge-in-library": "This series is split across library folders",
    "format-mismatch": `A chapter has a different format on ${there}`,
    "size-mismatch": `A chapter's size differs on ${there}`,
    "single-bundled-file": "One file holds the whole series",
    "empty-pc-folder": "The PC folder has no chapters",
    "malformed-json": "details.json can't be read",
    "image-only": "The folder holds loose images",
    "reader-compat": "Komikku may not read some files",
    "unparsed-files": "Some file names aren't chapter numbers",
    "name-too-long": `The folder name is too long for ${there}`,
    "name-invalid": `The folder name isn't allowed on ${there}`,
    "name-taken": `A folder with that name already exists on ${there}`,
    "folder-unreadable": `Couldn't read the folder on ${there}`,
    "loose-root-files": `Files sit loose in the sync folder on ${there}`,
  }[kind];
}
// reader-compat issues (analysis.js data.issues).
export const READER_ISSUE_COPY = Object.freeze({
  "no-cover": "no cover.jpg, so Komikku shows no cover",
  pdf: "PDFs, which Komikku can't open",
  tilde: "a ~ in a chapter number, which Komikku reads as another chapter",
});

// ── Op reasons (planner.js; per op kind) ──
export function opReasonCopy(kind, reason, tn) {
  const there = tn || "the target";
  const m = {
    push: { new: "New on your PC", "removed-on-device": `Removed on ${there}; copy it back`, missing: `Missing on ${there}` },
    update: { repair: "Unfinished copy; copy again", "changed-on-pc": "Changed on your PC", "unresolved-pending": "An earlier copy didn't finish" },
    replace: { "changed-on-device": `Changed on ${there}`, foreign: "Not copied by this app", unverified: "Not checked yet" },
    rename: { "rename-to-match": "Match the PC name" },
    delete: { sidecar: "Extra file", replacement: "Replaced by the PC copy", extra: "Not on your PC", "removed-from-target": `Stopped syncing to ${there}` },
  };
  return (m[kind] || {})[reason];
}
// Review-dialog group titles (spec §4 review; mockup review()): one plain
// sentence per (kind, reason). A pair missing here falls back to
// "<Kind>: <opReasonCopy>" in ReviewDialog, so a new planner reason still
// reads, just less naturally; the copy test keeps this table complete.
export function reviewGroupTitle(kind, reason, tn) {
  const there = tn || "the target";
  const m = {
    push: { new: "Copy new series", missing: `Copy chapters not on ${there} yet`, "removed-on-device": `Copy back a series removed on ${there}` },
    update: { "changed-on-pc": "Update files changed on your PC", repair: "Copy unfinished files again", "unresolved-pending": "Copy again files an earlier sync didn't finish" },
    replace: { "changed-on-device": `Replace files changed on ${there}`, foreign: "Replace files this app didn't copy", unverified: "Replace files not checked yet" },
    rename: { "rename-to-match": "Rename files to match your PC" },
    delete: {
      sidecar: "Remove extra files",
      replacement: "Remove files replaced on your PC",
      extra: "Remove chapters that aren't on your PC",
      "removed-from-target": `Remove series you stopped syncing to ${there}`,
    },
  };
  return (m[kind] || {})[reason];
}
export const OP_REASONS = Object.freeze({
  push: ["new", "removed-on-device", "missing"],
  update: ["repair", "changed-on-pc", "unresolved-pending"],
  replace: ["changed-on-device", "foreign", "unverified"],
  rename: ["rename-to-match"],
  delete: ["sidecar", "replacement", "extra", "removed-from-target"],
});

// ── Device-only folders, link suggestions, the Library chip ──
export const DEVICE_ONLY_COPY = Object.freeze({
  orphaned: "Was synced, now not in your library",
  preserved: "Never synced by this app; always kept",
});
export const SUGGESTION_TIER_COPY = Object.freeze({
  identity: "Same series id",
  content: "Same chapter files",
  record: "Sync history matches",
  name: "Same name",
  similar: "Similar name",
});
export const LIBRARY_STATUS_COPY = Object.freeze({
  synced: "In sync",
  pending: "Changes to copy",
  absent: "Not on the target yet",
  removed: "Removed on the target",
});
export const PREWARM_PAUSE_COPY = Object.freeze({
  download: "paused while a download runs",
  "check-all": "paused while checking for updates",
  search: "paused while a search runs",
  "sync-job": "paused while a sync job runs",
});

// ── device-listing-suspect, worded by its `cause` (prepare.js / planner.js
// set it; one text for every cause misled when the root itself was missing).
// {title, short (a connect-card row), body}. ──
export function listingSuspectCopy(r, tn) {
  const f = r && r.folder ? ` (${r.folder})` : "";
  const keep = "Nothing will be removed until a listing can be trusted.";
  switch (r && r.cause) {
    case "root-unlistable":
      return {
        title: `The sync folder on ${tn} can't be opened`,
        short: "its sync folder can't be opened",
        body: `The folder this target syncs to can't be listed on ${tn}. It may have been renamed or removed, the SD card may be out, or a storage permission prompt may be waiting on ${tn}. ${keep} Check the folder in this target's settings.`,
      };
    case "missed-folder":
      return {
        title: `${tn}'s listing left out a folder`,
        short: "its listing left out a folder",
        body: `A folder missing from ${tn}'s listing exists when checked directly${f}. ${keep} Re-check; if it happens again, unplug ${tn} and plug it back in.`,
      };
    case "stat-failed":
      return {
        title: `Couldn't check a folder on ${tn}`,
        short: "a folder couldn't be checked",
        body: `${tn} couldn't say whether a synced folder is still there${f}. ${keep} Re-check; if it happens again, unplug ${tn} and plug it back in.`,
      };
    case "listings-differ":
      return {
        title: `Two listings of ${tn} disagree`,
        short: "its listings disagree",
        body: `${tn} listed its folders differently twice in a row, which usually means storage is still mounting. ${keep} Re-check in a moment.`,
      };
    case "empty-root":
      return {
        title: `${tn} shows no series folders`,
        short: "shows no series folders",
        body: `${tn} shows no series folders, but its sync history lists some. A half-mounted SD card, a locked tablet or a storage permission prompt can cause this. ${keep}`,
      };
    default:
      // few-files (planner device.trusted === false) and any older refusal.
      return {
        title: `${tn} listed far fewer files than expected`,
        short: "listed fewer files than expected",
        body: `${tn} listed far fewer files than its sync history. A half-mounted SD card or a storage permission prompt can cause this. ${keep}`,
      };
  }
}

// ── Refusal titles (contract.js REFUSAL_CODES; bodies are built per surface).
// `r` (optional) is the refusal itself, for codes worded by their cause. ──
export function refusalTitle(code, tn, r) {
  if (code === "device-listing-suspect") return listingSuspectCopy(r || { code }, tn).title;
  return {
    disabled: "Device Sync is off",
    busy: "Another sync job is running",
    disconnected: `${tn} disconnected`,
    "library-missing": "Your library folder isn't reachable",
    "record-mismatch": `The folder on ${tn} isn't the one this app synced to`,
    "device-listing-suspect": `${tn} listed far fewer files than expected`,
    "needs-mode": `How should ${tn} be checked?`,
    blocked: "Start is waiting on you",
    invalid: "That request wasn't valid",
    "version-conflict": "Target settings changed elsewhere",
  }[code];
}
// A `blocked` refusal's checklist item (refusal.blocking[].kind).
export const BLOCKING_KINDS = Object.freeze(["loss", "mass-delete", "mass-repush", "listing-sanity", "refused", "plan-changed", "free-space"]);
export function blockingCopy(b, tn) {
  switch (b && b.kind) {
    case "loss":
      return "Confirm removing chapters that aren't on your PC";
    case "mass-delete":
      return `Confirm removing ${b.count} files from ${tn}`;
    case "mass-repush":
      return `Confirm copying ${b.count} ${b.count === 1 ? "folder" : "folders"} back to ${tn}`;
    case "listing-sanity":
      return `Re-check: ${tn} listed fewer files than its history says`;
    case "refused":
      return refusalTitle(b.refusal && b.refusal.code, tn, b.refusal) || "The plan was refused";
    case "plan-changed":
      return "The plan changed since you reviewed it. Review again.";
    case "free-space":
      // b.at: the first series the run can't fit (executor.js apply, space.js shortAt).
      return b.at ? `${tn} doesn't have enough free space (runs out at ${seriesKeyName(b.at)})` : `${tn} doesn't have enough free space`;
    default:
      return "Something needs your attention";
  }
}

// ── Job results (contract.js JOB_STATUSES; executor.js policyFor reasons).
// Templated reasons (rename-refused-*, invalid-name-*) match by prefix. ──
export const JOB_REASONS = Object.freeze([
  "cancelled", "device-lost", "offline", "still-connecting", "unauthorized", "server-unavailable", "no-space",
  "read-only", "record-write-failed", "error-budget", "internal", "pc-file-unreadable", "unknown-shard",
  "not-a-bound-folder", "needs-mode", "same-name", "out-of-room",
]);
export function jobReasonCopy(reason, tn) {
  const r = String(reason || "");
  const fixed = {
    cancelled: "you cancelled",
    "device-lost": `${tn} was unplugged`,
    offline: `${tn} went offline`,
    "still-connecting": `${tn} was still connecting`,
    unauthorized: `${tn} asked for USB debugging permission again`,
    "server-unavailable": "adb stopped responding",
    "no-space": `${tn} ran out of space`,
    "read-only": "the folder became read-only",
    "record-write-failed": "couldn't save sync history (files are fine)",
    "error-budget": "too many files failed",
    internal: "something went wrong inside sync (see log)",
    "pc-file-unreadable": "a PC file couldn't be read",
    "unknown-shard": "the sync history didn't match this folder",
    "not-a-bound-folder": "a folder wasn't the one this app synced to",
    "needs-mode": `${tn} needs a check mode first`,
    "same-name": "the folder already has that name",
    "folder-reappeared": "its folder came back on the target meanwhile",
    // A completed apply that skipped writes for space (executor.js _roomFor),
    // and a whole new series it never started (_skipSeriesNoRoom).
    "out-of-room": `not everything fit on ${tn}`,
    "no-room": `it didn't fit on ${tn}`,
  };
  if (fixed[r]) return fixed[r];
  if (r.startsWith("rename-refused-destination-exists")) return "a folder with the new name already exists";
  if (r.startsWith("rename-refused-source-missing")) return "the folder to rename is gone";
  if (r.startsWith("rename-refused-")) return "the rename was refused";
  if (r.startsWith("invalid-name-") || r.startsWith("invalid-folder-")) return `a name isn't allowed on ${tn}`;
  return r || "it stopped";
}
/**
 * A skipped file's reason in words. The executor templates some reasons
 * (`invalid-name-${segmentProblem}`, executor.js _guard), so a lookup in
 * SKIP_REASON_COPY alone printed them raw.
 */
export function skipReasonCopy(r, tn) {
  const k = String(r || "");
  if (SKIP_REASON_COPY[k]) return SKIP_REASON_COPY[k];
  if (k === "invalid-name-segment-too-long") return `the name is too long for ${tn || "the target"}`;
  if (k.startsWith("invalid-name-")) return `the name isn't allowed on ${tn || "the target"}`;
  return k || "skipped";
}
// Why reconcileForApply dropped a reviewed op before the apply ran
// (planner.js reconcileForApply `dropped[].reason`).
export const DROPPED_REASON_COPY = Object.freeze({
  vanished: "no longer in the plan",
  changed: "changed after you reviewed it",
  "no-chapter": "only extra files were left to copy",
});
export const SKIP_REASON_COPY = Object.freeze({
  downloading: "still downloading",
  "changed-since-review": "changed after you reviewed it",
  settling: "too new; waiting for it to settle",
  "pc-file-unreadable": "couldn't read the PC file",
  "listing-failed": "couldn't list the folder",
  "changed-since-plan": "changed after the plan",
  "already-gone": "already gone",
  "slot-guard": "your PC has a file with that name",
  // executor.js _roomFor: the space ledger turned the write away.
  "no-room": "didn't fit; left for the next sync",
});

// ── Warnings (prepare / planner / executor / pc-inventory `kind`) ──
export function warningCopy(w, tn) {
  const there = tn || "the target";
  const f = (w && w.folder) || "";
  return {
    "shard-corrupt": "Part of the sync history couldn't be read; those folders are checked again",
    "header-corrupt": "The sync history header couldn't be read",
    "undecodable-names": `Some names on ${there} aren't valid text and are left alone${f ? ` (${f})` : ""}`,
    "verify-failed": `Couldn't check ${f || "a folder"} on ${there}`,
    "free-space-unknown": `Couldn't read the free space on ${there}`,
    "pc-unhashed": "A PC file couldn't be fingerprinted",
    "rename-conflict": `A rename on ${there} clashes with an existing name`,
    "rename-unresolved": `An earlier rename on ${there} didn't finish`,
    "pending-unresolved": `An earlier copy to ${there} didn't finish`,
    "folder-unreadable": `Couldn't read ${f || "a library folder"} on your PC`,
    "file-unreadable": `Couldn't read a file in ${f || "a library folder"}`,
    "alias-conflict": "Two series link to the same folder",
    "duplicate-op": "A change was planned twice (ignored)",
    "pc-name-collision": "Two PC files map to one name",
    // executor.js apply: writes skipped or refused for space (plan A1b).
    "out-of-room": `${(w && w.files) || "Some"} ${w && w.files === 1 ? "file" : "files"} didn't fit on ${there}${w && w.emptied && w.emptied.length ? `; ${w.emptied.length} old ${w.emptied.length === 1 ? "copy was" : "copies were"} removed before the copy failed` : ""}`,
    "nomedia-failed": `Couldn't write .nomedia on ${there}`,
    "post-listing-failed": `Couldn't list ${f || "a folder"} after copying`,
    "cleanup-failed": `Couldn't remove bookkeeping files in ${f || "a folder"}`,
    "quick-verify": `A copied file's size on ${there} doesn't match`,
  }[w && w.kind];
}

// ── Needs attention (planner.js _attention codes; the "Needs attention" tile
// and a row's tooltip). The scrape in tools/_test_device_sync_copy.js reads
// the codes from _attention's own `out.push("…")` calls. ──
export function attentionCopy(code, tn) {
  const there = tn || "the target";
  return {
    held: "Can't be synced yet",
    anomaly: "Something to check before syncing",
    loss: "Removes chapters that aren't on your PC",
    suggestion: `A folder on ${there} may already be this series`,
    "needs-verify": `Needs a check against ${there} first`,
    "rebind-fell-back": `Its old folder name on ${there} is taken; it copies back under the PC name`,
    "replace-unticked": `A different copy on ${there} needs a decision`,
  }[code];
}

// ── Refusal cards (Sync tab, spec §5 "Refusal panel"; mockup refusals()).
// tone: info | warn | bad | muted. icon: a name SyncTab maps to a lucide
// icon (this file stays import-free). Bodies only; titles are refusalTitle.
// `r` is the refusal object ({code, message, fields?, detail?, internal?}). ──
export function refusalCardCopy(r, tn) {
  const code = r && r.code;
  if (r && r.internal) return { tone: "bad", icon: "x-circle", body: "This is a bug, not something you did. The details are in the log folder." };
  switch (code) {
    case "needs-mode":
      return { tone: "info", icon: "shield-check", body: `${tn} can't fingerprint files itself, so pick how sync proves what's already there. Your choice is remembered for this target.` };
    case "blocked":
      return { tone: "warn", icon: "list-checks", body: null };
    case "record-mismatch":
      return {
        tone: "bad",
        icon: "alert-triangle",
        body: `The sync history on ${tn} was written for a different ${r && Array.isArray(r.fields) && r.fields.includes("serial") ? "tablet" : "folder"}. It may have been restored, moved or copied from somewhere else, so nothing is changed until you choose.`,
      };
    case "device-listing-suspect":
      return { tone: "warn", icon: "file-warning", body: listingSuspectCopy(r, tn).body };
    case "disconnected":
      return { tone: "muted", icon: "unlink", body: "Plug it back in (and unlock it). The plan picks up when it reappears." };
    case "busy":
      return { tone: "muted", icon: "loader", body: "This one can start when it finishes." };
    case "library-missing":
      return { tone: "bad", icon: "folder-x", body: "The library folder doesn't exist or can't be read. Check the drive, or change the library folder in Settings → General." };
    case "version-conflict":
      return { tone: "info", icon: "refresh-cw", body: "Settings for this target were saved somewhere else first. Your edit wasn't applied; the current settings are loaded." };
    case "invalid":
      return { tone: "bad", icon: "x-circle", body: r && r.message ? `${capitalize(r.message)}. Nothing was changed.` : "Nothing was changed." };
    case "disabled":
      return { tone: "muted", icon: "circle-dashed", body: "Turn it on in Settings → Sync." };
    default:
      return { tone: "bad", icon: "alert-circle", body: (r && r.message) || "Sync stopped." };
  }
}

// ── Last-run cards (spec §5; mockup LAST_RUNS). The numbers are formatted by
// the caller; this only picks words and tone.
// No `plan` or `refresh` entry: neither writes a last run (service.js
// _afterJob: grep "the last apply/verify card stays").
// ──
export const LAST_RUN_PHASE_COPY = Object.freeze({
  apply: { done: (tn) => `Synced to ${tn}`, verb: "copied" },
  prune: { done: (tn) => `Removed folders from ${tn}`, verb: "removed" },
  rename: { done: (tn) => `Renamed a folder on ${tn}`, verb: "renamed" },
  verify: { done: (tn) => `Checked ${tn}`, verb: "checked" },
});
export function lastRunTitle(phase, status, tn, reason) {
  const p = LAST_RUN_PHASE_COPY[phase] || LAST_RUN_PHASE_COPY.apply;
  if (status === "completed" && reason === "out-of-room") return `Synced what fit on ${tn}`;
  if (status === "completed") return p.done(tn);
  if (status === "cancelled") return "Sync cancelled";
  if (status === "disconnected") return `${tn} disconnected mid-sync`;
  if (reason === "error-budget") return "Sync stopped after too many failed files";
  return "Sync stopped";
}
