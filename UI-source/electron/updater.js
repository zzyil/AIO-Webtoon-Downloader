// ============================================================
// APP SELF-UPDATE (electron-updater) — NOT the manga chapter
// "check-for-updates" IPC family, which lives in main.js and
// checks series for new chapters. This module owns the app's
// own opt-OUT silent update flow:
//
//   check GitHub Releases in the background → download silently
//   → install on app exit (autoInstallOnAppQuit). No dialogs,
//   no startup delay, no forced restart — by design (the user
//   explicitly chose "nothing outside Settings").
//
// UPDATE MATURITY DELAY (settings.appUpdateDelayDays, default 5):
// a release is only downloaded once it's at least N days old, so a
// bad release can be pulled or superseded before any client
// installs it. Implemented by gating the DOWNLOAD (autoDownload is
// off; update-available decides eligible-vs-deferred), not the
// install — a bad build never even lands on disk. The clock is the
// feed's releaseDate, which electron-builder stamps at BUILD time:
// for the normal build→draft→publish flow that's minutes before
// the draft exists, but a draft left unpublished for days eats
// into the window — publish drafts promptly. allowDowngrade stays
// false, so a pulled release that makes an OLDER version "latest"
// can never downgrade anyone regardless of this delay.
//
// Consumed by main.js only (grep initAppUpdater). Renderer
// surface: push channel "app-update-status" + invoke handlers
// "app-update:get-status" / ":check-now" / ":apply-now" (all
// registered in main.js, exposed via preload.js). The pref lives
// at settings.appAutoUpdate and is OPT-OUT — but the polarity is
// NOT decided here: every read below is `opts.enabled === true`,
// and main.js (this module's only caller) resolves the
// absent-means-ON default with `!== false` before calling
// initAppUpdater / applySettings. Changing the default is a
// main.js edit, not an edit to this file.
//
// Cross-file couplings:
//   - .github/workflows/release.yml — "Stamp date-based app
//     version" writes a version derived from the release commit's
//     committer timestamp (UI-source/scripts/ci-date-version.js,
//     YYYY.MDD.HMM UTC) into package.json at build time. The
//     repo's checked-in version never bumps, and release TAG
//     NAMES are pure labels — the GitHub provider only uses the
//     tag as a download-path segment, never parses it (verified:
//     with allowPrerelease=false, GitHubProvider.getLatestTagName
//     returns /releases/latest's tag_name verbatim), so any tag
//     naming works. "Point update feed at this repo" targets
//     app-update.yml at whichever repo ran the build. Both steps
//     are load-bearing: the semver comparison below is
//     stamped-version vs stamped-version.
//   - UI-source/package.json — build.publish (static default for
//     local builds), nsis.artifactName / appImage.artifactName
//     (space-free names; GitHub renames spaces in release assets
//     to dots while latest.yml records dashes → 404s without this),
//     and "--publish never" on the dist scripts (with build.publish
//     present, a tagged CI build would otherwise try to publish and
//     throw on the missing GH_TOKEN).
//
// Platform support: Windows NSIS + Linux AppImage only. macOS is
// unsigned + dmg-only (Squirrel.Mac requires a signed app), and a
// deb install would update via DebUpdater's pkexec prompt — not
// silent — so both report "unsupported" with a reason instead.
//
// The heavy require("electron-updater") is deferred until the
// first check (~30s after launch, or immediately when the user
// flips the toggle) so startup cost is zero even when enabled.
// ============================================================

const { app } = require("electron");

// First check waits out the launch rush (window paint, library scan,
// python-env checks). Not configurable — nothing user-visible depends
// on when the check runs, only that it never competes with startup.
const INITIAL_DELAY_MS = 30_000;
const RECHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // 4h — long-lived sessions still catch releases

const DAY_MS = 24 * 60 * 60 * 1000;
const DEFAULT_DELAY_DAYS = 5;   // mirror of SettingsTab's DEFAULT_SETTINGS.appUpdateDelayDays
const MAX_DELAY_DAYS = 60;      // mirror of the IntInput max in SettingsTab's App Updates section

// Error codes that mean "the latest release simply has no update feed"
// (e.g. the pre-updater v2.0.0 release carries no latest.yml, or a repo
// has no published releases at all). Mapped to the benign "no-feed"
// state so Settings doesn't show a scary error for a normal situation.
const BENIGN_ERROR_CODES = new Set([
  "ERR_UPDATER_CHANNEL_FILE_NOT_FOUND",
  "ERR_UPDATER_LATEST_VERSION_NOT_FOUND",
]);

let autoUpdater = null;   // set by _start() — lazy require("electron-updater")
let onStatusCb = () => {};
let enabled = false;      // mirror of settings.appAutoUpdate
let delayDays = DEFAULT_DELAY_DAYS; // mirror of settings.appUpdateDelayDays (clamped)
let started = false;      // _start() ran (updater module loaded + events wired)
let startTimer = null;    // pending INITIAL_DELAY_MS timeout
let recheckTimer = null;  // RECHECK_INTERVAL_MS interval
let deferTimer = null;    // one-shot re-check when a deferred release matures before the next interval

// Single source of truth for the renderer. Pushed (a copy) on every
// transition via onStatusCb → sendToUI("app-update-status") and
// returned by getStatus() for the Settings mount snapshot.
//   state: "unsupported" | "disabled" | "idle" | "checking"
//        | "deferred" | "downloading" | "downloaded" | "up-to-date"
//        | "no-feed" | "error"
const status = {
  supported: false,
  reason: null,          // human-readable why-not when !supported
  enabled: false,
  state: "disabled",
  currentVersion: "",    // app.getVersion() — CI-stamped date version (2.0.0 on local/dev builds)
  latestVersion: null,   // set while deferred / downloading / downloaded
  percent: null,         // 0-100 while downloading
  eligibleAt: null,      // ISO timestamp when a deferred release matures
  error: null,           // message when state === "error"
};

function _clampDelayDays(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return DEFAULT_DELAY_DAYS;
  return Math.min(MAX_DELAY_DAYS, Math.max(0, Math.trunc(n)));
}

// Milliseconds until the release is old enough to install (<= 0 =
// eligible now). Missing/unparseable releaseDate fails OPEN — the delay
// is a convenience buffer, not a security gate, and only hand-crafted
// feeds lack the field (electron-builder always stamps it).
function _maturityWaitMs(info) {
  const ts = Date.parse(info?.releaseDate || "");
  if (!Number.isFinite(ts)) return 0;
  return ts + delayDays * DAY_MS - Date.now();
}

function _computeSupport() {
  if (!app.isPackaged) {
    return { supported: false, reason: "Only available in installed builds — dev runs from source." };
  }
  if (process.platform === "win32") {
    return { supported: true, reason: null };
  }
  if (process.platform === "linux") {
    // electron-builder AppImages export APPIMAGE=<path to the .AppImage>;
    // its absence means a deb (or unpacked) install, which can't be
    // replaced silently.
    return process.env.APPIMAGE
      ? { supported: true, reason: null }
      : { supported: false, reason: "Auto-update only works for the AppImage build — deb installs update through your package manager." };
  }
  if (process.platform === "darwin") {
    return { supported: false, reason: "Auto-update needs a code-signed build, and the macOS build is unsigned — download new versions from GitHub Releases." };
  }
  return { supported: false, reason: `Auto-update isn't supported on ${process.platform}.` };
}

function _setStatus(patch) {
  Object.assign(status, patch);
  try {
    onStatusCb({ ...status });
  } catch {}
}

function _check() {
  if (!autoUpdater) return;
  // A downloaded update just re-validates by sha512 on re-check — no new
  // download, but the state would flicker downloaded→checking→downloaded
  // in Settings. Skip: the interesting transition (a NEWER release than
  // the downloaded one) is rare enough to wait for the next app launch.
  if (status.state === "downloaded") return;
  // checkForUpdates() both rejects AND emits "error" — the event handler
  // owns status; the catch just silences the duplicate rejection.
  autoUpdater.checkForUpdates().catch(() => {});
}

function _start() {
  if (started || !status.supported || !enabled) return;
  started = true;

  try {
    ({ autoUpdater } = require("electron-updater"));
  } catch (err) {
    _setStatus({ state: "error", error: `Updater failed to load: ${err.message || err}` });
    return;
  }

  // autoDownload OFF: the update-available handler below owns the
  // download decision so the maturity delay can defer too-fresh releases.
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true; // the whole "silent" design — NSIS runs with /S after quit
  autoUpdater.allowPrerelease = false;
  autoUpdater.allowDowngrade = false;
  autoUpdater.disableWebInstaller = true; // we only ship full NSIS installers; silences a per-check warning
  autoUpdater.logger = {
    info: (m) => console.log("[app-update]", m),
    warn: (m) => console.warn("[app-update]", m),
    error: (m) => console.error("[app-update]", m),
    debug: () => {},
  };

  autoUpdater.on("checking-for-update", () => {
    _setStatus({ state: "checking", error: null });
  });
  // Maturity gate: a release younger than delayDays is DEFERRED, not
  // downloaded — every re-check re-evaluates it statelessly, so the
  // download starts on the first check past the window (plus a one-shot
  // timer when maturity lands before the next 4h tick, so a waiting
  // session doesn't overshoot by up to 4h).
  autoUpdater.on("update-available", (info) => {
    const waitMs = _maturityWaitMs(info);
    if (waitMs <= 0) {
      _setStatus({ state: "downloading", latestVersion: info?.version || null, percent: 0, eligibleAt: null, error: null });
      // Rejection also surfaces via the "error" event, which owns status.
      autoUpdater.downloadUpdate().catch(() => {});
      return;
    }
    _setStatus({
      state: "deferred",
      latestVersion: info?.version || null,
      percent: null,
      eligibleAt: new Date(Date.now() + waitMs).toISOString(),
      error: null,
    });
    // Guard the setTimeout against >24.8-day waits (32-bit ms overflow
    // fires immediately) by only scheduling inside one interval window.
    if (waitMs < RECHECK_INTERVAL_MS && !deferTimer) {
      deferTimer = setTimeout(() => {
        deferTimer = null;
        _check();
      }, waitMs + 60_000);
    }
  });
  autoUpdater.on("download-progress", (p) => {
    _setStatus({ state: "downloading", percent: Math.round(p?.percent ?? 0) });
  });
  autoUpdater.on("update-downloaded", (info) => {
    _setStatus({ state: "downloaded", latestVersion: info?.version || status.latestVersion, percent: 100, eligibleAt: null, error: null });
  });
  autoUpdater.on("update-not-available", () => {
    _setStatus({ state: "up-to-date", latestVersion: null, percent: null, eligibleAt: null, error: null });
  });
  autoUpdater.on("error", (err) => {
    if (BENIGN_ERROR_CODES.has(err?.code)) {
      _setStatus({ state: "no-feed", error: null });
    } else {
      _setStatus({ state: "error", error: err?.message || String(err) });
    }
  });

  _check();
  recheckTimer = setInterval(_check, RECHECK_INTERVAL_MS);
}

/**
 * Wire the updater once at startup (main.js, after createWindow).
 * opts.enabled   — the RESOLVED settings.appAutoUpdate at launch (main.js
 *                  applies the opt-out default; absent → true).
 * opts.delayDays — settings.appUpdateDelayDays (clamped here).
 * opts.onStatus  — push callback; main.js forwards to the renderer.
 * When enabled and supported, the first check is deferred
 * INITIAL_DELAY_MS so launch stays untouched.
 */
function initAppUpdater(opts = {}) {
  if (typeof opts.onStatus === "function") onStatusCb = opts.onStatus;
  const sup = _computeSupport();
  enabled = opts.enabled === true;
  delayDays = _clampDelayDays(opts.delayDays);

  const state = !sup.supported ? "unsupported" : enabled ? "idle" : "disabled";
  _setStatus({
    supported: sup.supported,
    reason: sup.reason,
    enabled,
    state,
    currentVersion: app.getVersion(),
  });

  if (sup.supported && enabled) {
    startTimer = setTimeout(_start, INITIAL_DELAY_MS);
  }
}

/**
 * Live sync from the save-settings IPC handler. Enable mid-session →
 * arm immediately (the user just clicked; no artificial delay).
 * Disable mid-session → stop future checks AND flip
 * autoInstallOnAppQuit off, so an already-downloaded update does NOT
 * install on quit — that flag is read at quit time, making this the
 * real opt-out. A delay-only change while a release sits deferred
 * re-evaluates immediately (lowering the window to 0 + Save is the
 * "update me now" escape hatch).
 */
function applySettings(prefs = {}) {
  const nextDelay = _clampDelayDays(prefs.delayDays);
  const delayChanged = nextDelay !== delayDays;
  delayDays = nextDelay;

  const next = prefs.enabled === true;
  if (next === enabled) {
    if (delayChanged && enabled && started && status.state === "deferred") _check();
    return;
  }
  enabled = next;

  if (!status.supported) {
    // Pref persists (it applies on supported installs); nothing to arm here.
    _setStatus({ enabled });
    return;
  }

  if (enabled) {
    if (startTimer) { clearTimeout(startTimer); startTimer = null; }
    if (started) {
      if (autoUpdater) autoUpdater.autoInstallOnAppQuit = true;
      if (!recheckTimer) recheckTimer = setInterval(_check, RECHECK_INTERVAL_MS);
      _setStatus({ enabled, state: status.state === "disabled" ? "idle" : status.state });
      _check();
    } else {
      _setStatus({ enabled, state: "idle" });
      _start();
    }
  } else {
    if (startTimer) { clearTimeout(startTimer); startTimer = null; }
    if (recheckTimer) { clearInterval(recheckTimer); recheckTimer = null; }
    if (deferTimer) { clearTimeout(deferTimer); deferTimer = null; }
    if (autoUpdater) autoUpdater.autoInstallOnAppQuit = false;
    _setStatus({ enabled, state: "disabled" });
  }
}

/** Settings "Check now" button. UI gates on enabled+supported; mirror it here. */
function checkNow() {
  if (!status.supported) return { ok: false, reason: status.reason };
  if (!enabled) return { ok: false, reason: "Enable automatic updates first." };
  if (!started) {
    if (startTimer) { clearTimeout(startTimer); startTimer = null; }
    _start(); // runs the first check itself
    return { ok: true };
  }
  _check();
  return { ok: true };
}

/**
 * Settings "Restart & update now" button. Silent install + relaunch.
 * Caller (main.js apply-now handler) cancels running downloads first —
 * same cleanup window-all-closed does, so cancelled runs land in the
 * resume bar as usual. quitAndInstall sets its own internal flag, so
 * the 'quit' hook won't double-install.
 */
function applyNow() {
  if (!autoUpdater || status.state !== "downloaded") {
    return { ok: false, reason: "No downloaded update to apply." };
  }
  autoUpdater.quitAndInstall(true, true); // isSilent, isForceRunAfter (relaunch)
  return { ok: true };
}

/**
 * For quit paths that must NOT trigger the pending install — currently
 * only reinstall-python's app.relaunch()+app.exit(0): app.exit skips
 * before-quit/will-quit but still emits 'quit', so without this a
 * downloaded update would silently install WHILE the relaunched app is
 * starting (the NSIS silent installer then kills the fresh instance).
 */
function suppressInstallOnQuit() {
  if (autoUpdater) autoUpdater.autoInstallOnAppQuit = false;
}

function getStatus() {
  return { ...status };
}

module.exports = {
  initAppUpdater,
  applySettings,
  checkNow,
  applyNow,
  suppressInstallOnQuit,
  getStatus,
};
