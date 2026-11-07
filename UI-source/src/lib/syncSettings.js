// ============================================================
// DEVICE SYNC SETTINGS — renderer mirror (defaults + preset lists)
//
// Owns the eight global Device Sync keys as SettingsTab sees them: the
// defaults spread into SettingsTab's DEFAULT_SETTINGS (grep SYNC_DEFAULTS)
// and the option lists its Sync category renders (SYNC_PRESETS).
//
// KEEP IN SYNC with ../../electron/sync/sync-settings.js SYNC_SETTING_SPECS —
// that CommonJS module is the SOURCE OF TRUTH (the service resolves saved
// settings through it, and main.js get-settings spreads its resolved keys via
// syncSettingsForRenderer). This copy exists because the Vite bundle can't
// import electron modules, and so Settings renders correctly while sync is
// off (get-state carries `specs` then too, but the UI must not depend on an
// IPC round-trip for its own defaults). Drift guard:
// tools/_test_device_sync_settings_twin.js (imports this file as an ES module,
// so it must stay import-free).
// ============================================================

export const SYNC_DEFAULTS = Object.freeze({
  syncEnabled: false,
  syncPromptOnConnect: true,
  syncDeletePolicy: "guarded",
  syncHashMode: "cached",
  syncErrorBudget: 3,
  syncSizeWarnPct: 30,
  syncChapterPatterns: "",
  syncDiagnosticLog: true,
});

// Every preset the backend accepts, in its order (decision 6: all exposed).
// syncSizeWarnPct 0 is "off".
export const SYNC_PRESETS = Object.freeze({
  syncDeletePolicy: Object.freeze(["guarded", "add-only", "auto-replace"]),
  syncHashMode: Object.freeze(["cached", "rehash"]),
  syncErrorBudget: Object.freeze([1, 3, 5, 10]),
  syncSizeWarnPct: Object.freeze([0, 15, 30, 50]),
});

export const SYNC_SETTING_KEYS = Object.freeze(Object.keys(SYNC_DEFAULTS));
