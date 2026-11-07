// ============================================================
// DEVICE SYNC — GLOBAL SETTINGS (pure)
//
// Owns the eight global sync settings: their defaults, the presets the UI may
// offer (SYNC_SETTING_SPECS), and resolveSyncSettings(saved), which turns
// whatever settings.json holds into a complete, valid set. Per-target fields
// live in sync-targets.json and resolve in profiles.js.
//
// get-settings (main.js) spreads syncSettingsForRenderer(saved), so the
// renderer's draft and the hydrated prop agree on resolved values and a first
// launch shows no phantom "8 changed" (the appAutoUpdate precedent). The
// renderer keeps its own copy of the defaults and presets
// (src/lib/syncSettings.js) because it can't import this file; the twin test
// tools/_test_device_sync_settings_twin.js keeps the two equal.
//
// Read by: service.js, planner.js (deletePolicy, sizeWarnPct, patterns),
// main.js get-settings.
// ============================================================

const { parseChapterPatterns } = require("./chapter-labels");

/**
 * Every global sync key: its default and the values the UI may offer.
 * `presets` doubles as the accepted set for enum keys; a stored value
 * outside it resolves to the default (a hand-edited or stale settings.json
 * must never produce an engine state no UI can show).
 */
const SYNC_SETTING_SPECS = Object.freeze({
  syncEnabled: { type: "boolean", default: false },
  syncPromptOnConnect: { type: "boolean", default: true },
  syncDeletePolicy: { type: "enum", default: "guarded", presets: Object.freeze(["guarded", "add-only", "auto-replace"]) },
  syncHashMode: { type: "enum", default: "cached", presets: Object.freeze(["cached", "rehash"]) },
  // Consecutive failures before a run aborts (CompareManga transfer_runner's 3).
  syncErrorBudget: { type: "enum", default: 3, presets: Object.freeze([1, 3, 5, 10]) },
  // Size-mismatch anomaly threshold in percent; 0 is "off".
  syncSizeWarnPct: { type: "enum", default: 30, presets: Object.freeze([0, 15, 30, 50]) },
  // One regex per line with a named group `ch` (chapter-labels.js).
  syncChapterPatterns: { type: "patterns", default: "" },
  // userData/sync/logs/service.log (diag-log.js). On by default: the installed
  // app has no console, so this is where sync faults can be read back.
  syncDiagnosticLog: { type: "boolean", default: true },
});

const SYNC_SETTING_KEYS = Object.freeze(Object.keys(SYNC_SETTING_SPECS));

function _resolveOne(spec, value) {
  switch (spec.type) {
    case "boolean":
      return typeof value === "boolean" ? value : spec.default;
    case "enum":
      return spec.presets.includes(value) ? value : spec.default;
    case "patterns":
      // Kept as text even when some lines are invalid: the labeler uses the
      // valid lines and label-preview reports the rest, so one typo can't
      // silently discard every other pattern.
      return typeof value === "string" ? value : spec.default;
    default:
      return spec.default;
  }
}

/**
 * The complete global sync settings for `saved` (history.getSettings()).
 * Absent, mistyped or out-of-set values resolve to their defaults.
 * `chapterPatterns` is the parsed form of syncChapterPatterns for the engine.
 */
function resolveSyncSettings(saved) {
  const src = saved && typeof saved === "object" ? saved : {};
  const out = {};
  for (const key of SYNC_SETTING_KEYS) out[key] = _resolveOne(SYNC_SETTING_SPECS[key], src[key]);
  const parsed = parseChapterPatterns(out.syncChapterPatterns);
  out.chapterPatterns = parsed.patterns;
  out.chapterPatternErrors = parsed.errors;
  return out;
}

/**
 * The eight keys alone, resolved: what get-settings hands the renderer.
 * Never the parsed chapterPatterns (RegExps don't survive IPC) or their
 * errors (label-preview reports those).
 */
function syncSettingsForRenderer(saved) {
  const r = resolveSyncSettings(saved);
  const out = {};
  for (const key of SYNC_SETTING_KEYS) out[key] = r[key];
  return out;
}

module.exports = { SYNC_SETTING_SPECS, SYNC_SETTING_KEYS, resolveSyncSettings, syncSettingsForRenderer };
