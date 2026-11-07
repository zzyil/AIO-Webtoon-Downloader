// ============================================================
// DEVICE SYNC — IPC CONTRACT (main process, canonical)
//
// Owns every name that crosses the main/renderer boundary for Device Sync:
// the invoke channels, the one push channel, the event kinds, and the enums
// the payloads carry (op kinds, origins, job phases and statuses, refusal
// codes, verify modes). Every refusal any sync:* handler returns is built by
// refuse() below, so the renderer sees exactly one shape.
//
// Read by: sync/service.js (registers CHANNELS, emits EVENT_KINDS), the pure
// core modules (planner.js, provenance.js, job-record.js) for the enums.
//
// DUPLICATED LITERALS: electron/preload.js runs sandboxed and cannot
// require() this file, and src/hooks/useDeviceSync.js is bundled by Vite, so
// both spell the channel names and event kinds out again. The drift guard is
// tools/_test_device_sync_contract.js. grep: deviceSync
// ============================================================

// Invoke channels, `sync:` + name (the search:/app-update: namespace style).
const CHANNEL_NAMES = Object.freeze([
  "get-state",
  "config-op",
  "locate-adb",
  "list-devices",
  "browse-remote",
  "plan",
  "series-detail",
  "set-selection",
  "verify",
  "apply",
  "prune",
  "rename-device-folder",
  "cancel",
  "prompt-response",
  "library-status",
  "label-preview",
  "export-report",
  "device-cover",
  "find-sources:start",
  "find-sources:cancel",
  "find-sources:get",
  "find-sources:update-row",
  // P4 decision: Link (rule 2) writes the record, not sync-targets.json, so
  // it is its own channel rather than a config-op op.
  "link",
  // UI pass U0: the Start review's one payload (planner.planReview + gen,
  // capacity, policy), so the review never assembles itself from ledger rows.
  "review",
]);

const CHANNELS = Object.freeze(
  Object.fromEntries(CHANNEL_NAMES.map((n) => [n, `sync:${n}`])),
);

// While syncEnabled is false every channel answers refuse("disabled") except
// these: get-state reports enabled:false from settings alone, label-preview
// is pure, and both cancels must always be able to stop a job.
const CHANNELS_WHILE_DISABLED = Object.freeze(
  new Set(["get-state", "label-preview", "cancel", "find-sources:cancel"].map((n) => CHANNELS[n])),
);

// The one push channel. Kebab-case like every other push channel in main.js.
const EVENT_CHANNEL = "sync-event";

// UI pass U0, "ask the system, don't copy it": every piece of state the Sync
// UI shows is pushed when it changes, so the renderer never re-reads
// get-state to catch up (the re-read raced the identity clear and lost job
// results). `plans` {targetId, summary, refusal, stale, lastRun, gen,
// selSeq, lastPlan, updating, links, removed?} is per target and coalesced to one event per
// tick (selSeq: the last set-selection seq applied; lastPlan: the last plan
// job's {runId, status, reason, at} — UI-pass U2-1/U2-3; `stale` is true
// while the plan is out of date, `updating` while that is only a review
// decision being re-derived or topped up, not a full re-plan; `links` the
// Links not yet in the plan, {suggestionId, seriesKey, deviceFolder, bound});
// `adb`
// {adb} carries the binary, server and tracking state; `settings`
// {enabled, settings} follows every applySettings.
const EVENT_KINDS = Object.freeze([
  "device",
  "job",
  "config",
  "prompt",
  "find",
  "prewarm",
  "library-status",
  "plans",
  "adb",
  "settings",
]);

const OP_KINDS = Object.freeze(["push", "update", "replace", "rename", "delete"]);

// Provenance of a recorded device file (parent rule 3 plus `pending` and
// `adopted-size`; see provenance.js for what each one licenses).
const ORIGINS = Object.freeze([
  "pushed",
  "adopted",
  "adopted-size",
  "partial",
  "foreign",
  "pending",
]);

// `refresh` (plan sync-temp/plans/device-sync-cheap-replans.md): an automatic
// job that tops up a plan derived from cached facts after a review decision
// (a Link, a newly mirrored file); service.js grep _startRefresh.
const JOB_PHASES = Object.freeze(["plan", "verify", "apply", "prune", "rename", "refresh"]);

// The close listener asks before quitting only while one of these runs; a
// plan or verify is simply cancelled (main.js, grep deviceSync).
const QUIT_ASK_PHASES = Object.freeze(new Set(["apply", "prune", "rename"]));

const JOB_STATUSES = Object.freeze(["completed", "cancelled", "failed", "disconnected"]);

const VERIFY_MODES = Object.freeze(["device-hash", "read-back", "adopt-size"]);

const REFUSAL_CODES = Object.freeze([
  "disabled",
  "busy",
  "disconnected",
  "library-missing",
  "record-mismatch",
  "device-listing-suspect",
  "needs-mode",
  "blocked",
  "invalid",
  "version-conflict",
]);

const _REFUSAL_SET = new Set(REFUSAL_CODES);

/**
 * The one refusal shape every sync:* handler returns: {ok:false, code,
 * message, ...extra}. An unknown code is a programming error and throws, so a
 * typo can't invent a code the renderer has no branch for.
 */
function refuse(code, message, extra) {
  if (!_REFUSAL_SET.has(code)) throw new Error(`unknown sync refusal code: ${code}`);
  return { ...(extra || {}), ok: false, code, message: String(message || code) };
}

/**
 * @typedef {Object} SyncOp  One planned device change (planner.js builds it).
 * @property {string} id      Stable: `<kind>:<folder>/<name>` (rename: `…/<from>-><to>`).
 * @property {string} kind    One of OP_KINDS.
 * @property {string} seriesKey
 * @property {string} folder  Device folder name, byte-exact.
 * @property {string} name    Device file name the op writes or removes.
 * @property {string} [from]  rename only.
 * @property {string} [to]    rename only.
 * @property {number} size    Bytes the op transfers (0 for delete/rename).
 * @property {string|null} sha  Content the op is about; the selection is keyed by (id, sha).
 * @property {boolean} preselected
 * @property {string} reason
 * @property {boolean} settling  Counted but not offered (file younger than the settle age).
 */

/**
 * @typedef {Object} SyncRefusal
 * @property {false} ok
 * @property {string} code     One of REFUSAL_CODES.
 * @property {string} message
 */

module.exports = {
  CHANNEL_NAMES,
  CHANNELS,
  CHANNELS_WHILE_DISABLED,
  EVENT_CHANNEL,
  EVENT_KINDS,
  OP_KINDS,
  ORIGINS,
  JOB_PHASES,
  QUIT_ASK_PHASES,
  JOB_STATUSES,
  VERIFY_MODES,
  REFUSAL_CODES,
  refuse,
};
