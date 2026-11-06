// ============================================================
// ELECTRON MAIN PROCESS
//
// This runs in Node.js. It creates the app window and acts as
// the bridge between the React UI (preload.js) and the Python
// downloader (downloader.js).
//
// STARTUP FLOW:
//   1. App launches
//   2. If packaged + no Python env → show setup wizard
//   3. Setup downloads Python, installs deps, downloads Chromium
//   4. When setup completes → show main window
//   5. On future launches, skip straight to step 4
//
// IPC CHANNEL NAMES must match preload.js exactly.
// ============================================================

const { app, BrowserWindow, ipcMain, nativeTheme, dialog, shell, protocol, net, session } = require("electron");
const { pathToFileURL } = require("url");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const { Downloader } = require("./downloader");
const { Searcher } = require("./searcher");
// Resource Limits (Settings → Max network / Max CPU usage). Resolved HERE, the
// universal live-settings spawn chokepoint, so manual/search/library/resume all
// honor the CURRENT limit. Hard-override semantics — see resource-limits.js.
const {
  applyNetworkLimit,
  cpuPercentForLevel,
  searchParallelismForLevel,
  resumeThrottleFlags,
} = require("./resource-limits");
const { HistoryManager } = require("./history");
const { PythonSetup, isSetupComplete, deleteEnv, PYTHON_VERSION } = require("./setup");
const { scanLibrary, generateMissingThumbnails, downloadMissingCovers, cleanupOrphanCovers, getChaptersOnDevice, getImageChaptersOnDevice, groupEntriesBySeries } = require("./library");
// Label spelling for set-chapters-ignored and the merge comes from ONE place,
// series-ledger.js (required below), so the two writers can't drift.
const { mergeSeriesFolders } = require("./series-merge");
const { createUpdateCheckRecord, resultRow: updateCheckResultRow } = require("./update-check-record");
// Per-kind ledger views (chapters top-level, volumes under "volumes") and the
// volume file-name rules. Twin of series_ledger.py / item_kinds.py.
const seriesLedger = require("./series-ledger");
// App self-update (opt-OUT, settings.appAutoUpdate) — cheap require; the
// heavy electron-updater load is deferred inside updater.js. NOT the manga
// chapter update-check family below (check-for-updates etc.). Polarity is
// owned HERE: updater.js is `opts.enabled === true` throughout and this file
// is its only caller, so the `!== false` (absent-means-ON) reads below are
// the single place the default lives.
const appUpdater = require("./updater");

// ── Device Sync (electron/sync/service.js; off unless settings.syncEnabled) ──
// The require is isolated so a load-time throw anywhere in sync/ can't abort
// this file; every hook below is also wrapped, on top of the facade's own
// never-throw contract, so a sync defect can't break a path every user runs
// with sync off. grep: deviceSync
let initDeviceSync = null;
// get-settings' resolved sync keys (sync/sync-settings.js). A load failure
// leaves the keys out; the renderer then shows its own defaults.
let syncSettingsForRenderer = () => ({});
try {
  ({ initDeviceSync } = require("./sync/service"));
  ({ syncSettingsForRenderer } = require("./sync/sync-settings"));
} catch (e) {
  console.error("[deviceSync] load failed", e);
}
let deviceSync = null;

// Single-instance lock (app-wide, for Device Sync: two instances would both
// own port-5037 sessions and the same userData/sync record). Electron keys
// the lock on the userData directory AT CALL TIME, so anything that moves
// userData must run before this line.
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) app.quit();

// ── DEV MODE DEFAULTS ──
// When running from source (npm run electron:dev), the app uses
// your system Python and the aio-dl.py / project root that ship in
// this checkout — derived from __dirname so the defaults follow the
// repo wherever it's cloned. Used to bake in an absolute path to the
// original developer's OneDrive folder, which mkdirSync would silently
// re-create on any other machine and leave the user staring at a
// stranger's home path in Settings.
//
// __dirname at runtime is UI-source/electron/, so:
//   ../..  → repo root  (where aio-dl.py + sites/ live in dev)
const DEV_WORKING_DIR = path.resolve(__dirname, "..", "..");
const DEV_SCRIPT_PATH = path.resolve(__dirname, "..", "..", "aio-dl.py");

let DEV_PYTHON_CMD = "python";
const devVenvPython = process.platform === "win32"
  ? path.join(DEV_WORKING_DIR, "venv", "Scripts", "python.exe")
  : path.join(DEV_WORKING_DIR, "venv", "bin", "python");

if (fs.existsSync(devVenvPython)) {
  DEV_PYTHON_CMD = devVenvPython;
} else if (process.platform !== "win32") {
  DEV_PYTHON_CMD = "python3";
}

// ── GLOBALS ──
const IS_PACKAGED = app.isPackaged;

function resolveAppIconPath() {
  const resourceIcon = process.platform === "win32" ? "icon.ico" : "icon.png";
  const candidates = IS_PACKAGED
    ? [
        path.join(process.resourcesPath, resourceIcon),
        path.join(process.resourcesPath, "icon.png"),
      ]
    : [
        path.join(__dirname, "..", "build-resources", resourceIcon),
        path.join(__dirname, "..", "build-resources", "icon.png"),
      ];
  return candidates.find((candidate) => fs.existsSync(candidate));
}

const APP_ICON_PATH = resolveAppIconPath();

// DEFAULT_DOWNLOAD_DEFAULTS removed 2026-05-13: SettingsTab.jsx now owns
// the defaults dict. The architectural triad (main.js + useDownloader.js
// + SettingsTab.jsx) must agree — if you re-introduce defaults here,
// also update the other two.

// Fix for dark gradient banding (dithering) on high-DPI / 4K monitors.
// Without this, Electron may use limited color depth which causes
// visible color stepping in dark backgrounds.
app.commandLine.appendSwitch("force-color-profile", "srgb");

// Register a custom protocol for serving local files to the renderer.
// The Library tab uses this to load cached thumbnail images from disk
// (stored in %APPDATA%/aio-downloader-ui/thumb-cache/). The renderer
// can't access file:// URLs directly, so we serve them via localfile://.
// MUST be called before app.whenReady().
protocol.registerSchemesAsPrivileged([{
  scheme: "localfile",
  privileges: {
    secure: true,
    supportFetchAPI: true,
    stream: true,
    corsEnabled: true,
  },
}]);

let mainWindow = null;
let setupWindow = null;
let downloader = null;
let searcher = null;
let history = null;
let currentSetup = null;  // The PythonSetup instance (only during first-run)

// These are computed after app.whenReady() because app.getPath()
// needs the app to be fully initialized first.
let pythonEnvDir = null;     // Where the downloaded Python lives
let pythonSrcDir = null;     // Where aio-dl.py + sites/ ship (read-only)
let playwrightDir = null;    // Where Playwright's Chromium is stored
let vcRuntimeDir = null;     // Where bundled MSVC++ runtime DLLs ship (read-only).
                             // Setup copies these into the embed Python dir so
                             // C++ extensions like greenlet (playwright dep) can
                             // load. Without this, _greenlet.pyd → "DLL load
                             // failed" because Python embed distro lacks
                             // MSVCP140.dll. Only relevant in packaged mode.

// The resolved paths used for spawning downloads.
// In packaged mode these point to the bundled/downloaded Python.
// In dev mode they point to your system Python.
let defaultPythonCmd = DEV_PYTHON_CMD;
let defaultScriptPath = DEV_SCRIPT_PATH;
let defaultWorkingDir = DEV_WORKING_DIR;

// AbortController for the in-flight `Check All` parallel sweep. Module-scoped
// because both the check-all and cancel-check-all IPC handlers need to see
// the same handle. Null = no scan running. The check-all handler installs a
// fresh controller per scan and the renderer-driven cancel handler aborts it.
// Cross-file: UI-source/electron/preload.js exposes cancelCheckAllUpdates;
// UI-source/src/components/UpdatesCenter.jsx calls it from the Cancel button.
let _checkAllAbortCtrl = null;

// ── Check All sweep record ──
// The AUTHORITY for what the Updates Center shows, and it lives here rather
// than in the renderer because the renderer keeps losing it: App.jsx renders
// tab bodies conditionally, so LibraryTab unmounts the instant the user clicks
// another tab while the sweep keeps running in this process. A remounting tab
// re-reads this via the get-update-check-state IPC instead of starting over.
// All the state/derivation rules live in ./update-check-record.js — read its
// header before changing anything here.
const _updateCheck = createUpdateCheckRecord(sendToUI);

// Quit-confirmation gate. The mainWindow "close" listener (createWindow)
// preventDefaults while a download is actually RUNNING and asks the renderer
// (src/components/ConfirmQuitDialog.jsx) instead of using
// dialog.showMessageBox — a native modal breaks Electron's renderer focus/
// input handling, which is why every confirmation in this app is a React
// affordance (see ResumeBar.jsx's hand-rolled delete confirm).
// quitConfirmed short-circuits the listener once the user (or the safety
// valve, or the silent-update path) has answered. Module-scoped because the
// listener in createWindow() and the IPC handlers in setupIPC() share them.
let quitConfirmed = false;
let quitSafetyTimer = null;

function clearQuitSafetyTimer() {
  if (quitSafetyTimer) {
    clearTimeout(quitSafetyTimer);
    quitSafetyTimer = null;
  }
}

// ── PATH COMPUTATION ──

function computePaths() {
  if (IS_PACKAGED) {
    // Python runtime: downloaded on first run into user's app data folder.
    // This folder persists across app updates and is writable.
    pythonEnvDir = path.join(app.getPath("userData"), "python-env");
    playwrightDir = path.join(pythonEnvDir, "playwright-browsers");

    // Python source: aio-dl.py + sites/ ship inside the installer as
    // "extraResources". They live in the app's resources/ folder (read-only).
    pythonSrcDir = path.join(process.resourcesPath, "python-src");

    // VC++ runtime DLLs (msvcp140, vcruntime140_1, etc.) ship as a separate
    // extraResources entry — see package.json. Setup copies them next to
    // python.exe so C++-using extensions (greenlet primarily) can load.
    vcRuntimeDir = path.join(process.resourcesPath, "vcruntime");

    // The embedded Python is inside the python-env folder. Layout differs:
    //   Windows (python.org embed):  <env>/python/python.exe
    //   Unix (python-build-standalone install_only): <env>/python/bin/python3
    // setup.js's pythonExe getter follows the same convention.
    if (process.platform === "win32") {
      defaultPythonCmd = path.join(pythonEnvDir, "python", "python.exe");
    } else {
      defaultPythonCmd = path.join(pythonEnvDir, "python", "bin", "python3");
    }

    // aio-dl.py is shipped with the app (not in the env folder)
    defaultScriptPath = path.join(pythonSrcDir, "aio-dl.py");

    // Comics are saved to the user's Documents folder (not inside the
    // app install directory, because that's read-only).
    defaultWorkingDir = path.join(app.getPath("documents"), "AIO Downloader");
  }
  // In dev mode, the defaults set above are used as-is.
}

function getConfiguredOutputRoot(workingDir) {
  const root = workingDir || defaultWorkingDir;
  let outputDir = process.env.AIO_OUTPUT_DIR || "manga";
  try {
    const configPath = path.join(root, "aio_config.json");
    if (!process.env.AIO_OUTPUT_DIR && fs.existsSync(configPath)) {
      const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
      if (config && typeof config.output_dir === "string" && config.output_dir.trim()) {
        outputDir = config.output_dir.trim();
      }
    }
  } catch {}
  return path.isAbsolute(outputDir) ? outputDir : path.join(root, outputDir);
}

function readHidMarker(folderPath) {
  for (const marker of [".series_hid", ".mangafire_hid"]) {
    const markerPath = path.join(folderPath, marker);
    if (!fs.existsSync(markerPath)) continue;
    try {
      const value = fs.readFileSync(markerPath, "utf8").trim();
      if (value) return value;
    } catch {}
  }
  return null;
}

function runMetadataCli(args, stdinData = null) {
  const metadataScript = path.join(path.dirname(defaultScriptPath), "metadata_cli.py");
  const settings = history?.getSettings?.() || {};
  // metadata_cli always runs against the bundled defaultScriptPath dir (below),
  // so we only need pythonCmd + workingDir from the resolver here.
  const { pythonCmd, workingDir } = resolveSpawnPaths(settings);
  return new Promise((resolve, reject) => {
    const proc = spawn(pythonCmd, [metadataScript, ...args], {
      cwd: workingDir,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: path.dirname(defaultScriptPath),
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    proc.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr || `metadata_cli exited with ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout || "{}"));
      } catch (err) {
        reject(new Error(`Invalid metadata JSON: ${err.message}`));
      }
    });
    // UIE-3: if the child exits early, writing to its stdin raises EPIPE.
    // Without a listener that surfaces as an unhandled 'error' that crashes
    // the Electron main process — swallow it (the 'close' handler above
    // already rejects with the child's exit code/stderr).
    proc.stdin.on("error", (e) => {
      console.warn("metadata_cli stdin write error:", e.message || e);
    });
    if (stdinData) proc.stdin.write(stdinData);
    proc.stdin.end();
  });
}

/**
 * Ensure the python-src directory is in the embedded Python's ._pth file.
 *
 * WHY THIS IS NEEDED (Windows embed only):
 *   The embeddable Python ships with a ._pth file (e.g. python313._pth)
 *   that completely controls sys.path. When this file exists, Python
 *   IGNORES the PYTHONPATH environment variable entirely. The cwd is
 *   set to the user's output folder (Documents/AIO Downloader), but
 *   aio-dl.py needs to import from sites/ which is in resources/python-src/.
 *   Without adding that path to ._pth, you get:
 *     ModuleNotFoundError: No module named 'sites'
 *
 * This runs on every startup (not just first-run setup) because the app
 * install path can change on updates and the ._pth file persists in the
 * separate python-env folder.
 *
 * NO-OP ON UNIX:
 *   python-build-standalone uses a normal site-packages layout with no
 *   ._pth file — standard CPython sys.path discovery applies, which
 *   respects PYTHONPATH. We set PYTHONPATH=pythonSrcDir at spawn time
 *   in initDownloader() and the _checkSeriesUpdates IPC handler instead.
 */
function ensurePythonSrcInPth() {
  if (process.platform !== "win32") return;
  if (!IS_PACKAGED || !pythonSrcDir) return;

  const pythonDir = path.join(pythonEnvDir, "python");
  if (!fs.existsSync(pythonDir)) return;

  try {
    const files = fs.readdirSync(pythonDir);
    const pthFile = files.find((f) => /^python\d+\._pth$/.test(f));
    if (!pthFile) return;

    const pthPath = path.join(pythonDir, pthFile);
    let content = fs.readFileSync(pthPath, "utf8");

    // Check if python-src path is already present (exact match)
    if (content.includes(pythonSrcDir)) return;

    // Remove any old/stale python-src lines (from previous install locations)
    const lines = content.split("\n").filter(
      (line) => !line.includes("python-src")
    );

    // Add the current python-src path
    lines.push(pythonSrcDir);
    content = lines.join("\n") + "\n";

    fs.writeFileSync(pthPath, content);
    console.log(`Added python-src to ${pthFile}: ${pythonSrcDir}`);
  } catch (err) {
    console.error("Failed to update ._pth file:", err.message);
  }
}

// ============================================================
// HELPER: resolve the spawn paths (python / script / workingDir)
// ============================================================
//
// Every IPC handler that spawns aio-dl.py resolves the same three paths
// from saved settings, each falling back to the runtime-computed default
// when the saved value is empty/missing. The empty-string fallback is
// load-bearing: get-settings deliberately does NOT merge the resolved
// paths, and the renderer saves "" for un-customized fields — so
// `settings.X || defaultX` is the ONLY place the default gets applied on
// the spawn side (see the get-settings handler's comment + history.js's
// volatile-path filter for the AppImage/Gatekeeper stale-path story).
// Kept as one helper so all spawn sites stay in lockstep; callers that
// only need workingDir just destructure that field.
function resolveSpawnPaths(settings) {
  return {
    pythonCmd: settings.pythonCmd || defaultPythonCmd,
    scriptPath: settings.scriptPath || defaultScriptPath,
    workingDir: settings.workingDir || defaultWorkingDir,
  };
}

// ============================================================
// HELPER: build the extra environment for a spawned Python process
// ============================================================
//
// PLAYWRIGHT_BROWSERS_PATH (all platforms, packaged only): points
// patchright at the bundled Chromium that setup.js downloaded, so the
// Playwright-driven handlers don't re-download into a system path. Gated on
// fs.existsSync(playwrightDir) — if the browser dir isn't on disk yet
// (setup incomplete), we DON'T pin a nonexistent path and let patchright
// fall back to its own resolution. This existsSync guard is the "most
// complete" form; _checkSeriesUpdates used to omit it (drift, unified
// 2026-07 dedup sweep).
//
// PYTHONPATH (packaged Unix only): puts resources/python-src/ on sys.path
// so aio-dl.py can `import sites` / `import aio_search_cli`. On Windows the
// embed Python ignores PYTHONPATH and ensurePythonSrcInPth() writes the
// path into ._pth instead.
//
// opts.unbuffered — when true, folds in PYTHONUNBUFFERED="1". Direct-spawn
// callers (like _checkSeriesUpdates) that don't add it separately at spawn
// time need it here; the Downloader/Searcher spawn path adds PYTHONUNBUFFERED
// itself, so those callers omit it. NOTE: this is deliberately NOT the same
// env as runMetadataCli, which sets an unconditional PYTHONPATH=<script dir>
// and no Playwright var — that call site stays hand-rolled on purpose.
function buildPythonEnv(opts = {}) {
  const env = {};
  if (opts.unbuffered) env.PYTHONUNBUFFERED = "1";
  if (IS_PACKAGED && playwrightDir && fs.existsSync(playwrightDir)) {
    env.PLAYWRIGHT_BROWSERS_PATH = playwrightDir;
  }
  if (IS_PACKAGED && process.platform !== "win32" && pythonSrcDir) {
    env.PYTHONPATH = pythonSrcDir;
  }
  return env;
}

// ============================================================
// HELPER: send IPC message to a window
// ============================================================

function sendToWindow(win, channel, data) {
  if (win && !win.isDestroyed()) {
    win.webContents.send(channel, data);
  }
}

function sendToUI(channel, data) {
  sendToWindow(mainWindow, channel, data);
}

// Device Sync library hook: debounced re-plan of connected targets. Never
// throws, so it's safe inside handlers whose answer must not change.
function notifySyncLibraryChanged(reason) {
  try {
    deviceSync?.notifyLibraryChanged(reason);
  } catch (e) {
    console.error("[deviceSync] notifyLibraryChanged", e);
  }
}

// ============================================================
// SETUP WIZARD (first-run only)
// ============================================================

/**
 * Opens the setup window and runs PythonSetup.
 * Returns a Promise that resolves when setup is complete.
 * The user can click "Retry" if a step fails.
 */
function runSetupFlow() {
  return new Promise((resolve) => {
    // Create the setup window. Sized to fit the full error+buttons+log-toggle
    // chain even when an error message is long — at 420px the log toggle was
    // pushed off-screen on long errors (e.g. the MSVCP140 DLL ImportError),
    // leaving the user unable to expand the log panel.
    setupWindow = new BrowserWindow({
      width: 540,
      height: 580,
      resizable: false,
      maximizable: false,
      frame: false,  // We draw our own title bar in setup.html
      icon: APP_ICON_PATH,
      backgroundColor: "#0f1117",
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });

    setupWindow.loadFile(path.join(__dirname, "setup.html"));

    // Function that creates a PythonSetup instance and runs it.
    // Called on first load and again if the user clicks "Retry".
    const startSetup = () => {
      currentSetup = new PythonSetup({
        envDir: pythonEnvDir,
        // pythonSrcDir is passed so setup.js can (a) add it to ._pth during
        // _configurePython and (b) run the end-to-end smoke test that imports
        // `sites` and `aio_search_cli` from the bundle. ensurePythonSrcInPth()
        // also runs on every launch — this setup-time write just bootstraps
        // it for the first-run smoke test.
        pythonSrcDir,
        // vcRuntimeDir holds the bundled MSVC++ runtime DLLs (msvcp140 etc.).
        // setup.js copies these next to the embed python.exe so C++-using
        // extensions (greenlet → playwright path) load successfully. Without
        // this, _greenlet.pyd fails with "DLL load failed" because Python's
        // embed distro doesn't ship MSVCP140.dll.
        vcRuntimeDir,
        requirementsPath: path.join(pythonSrcDir, "requirements.txt"),

        // Forward progress to the setup window
        onStep: (data) => sendToWindow(setupWindow, "setup-step", data),
        onLog: (line) => sendToWindow(setupWindow, "setup-log", line),
        onProgress: (pct) => sendToWindow(setupWindow, "setup-progress", pct),

        onComplete: () => {
          sendToWindow(setupWindow, "setup-complete");
          // Give the user a moment to see "Setup complete!" before switching
          setTimeout(() => {
            if (setupWindow && !setupWindow.isDestroyed()) {
              setupWindow.close();
              setupWindow = null;
            }
            currentSetup = null;
            resolve();
          }, 2000);
        },

        onError: (msg) => {
          // Show the error in the setup window — user can click Retry
          sendToWindow(setupWindow, "setup-error", msg);
        },
      });

      currentSetup.run();
    };

    // Store for the retry-setup IPC handler
    ipcMain.removeHandler("retry-setup");
    ipcMain.handle("retry-setup", async () => {
      startSetup();
    });

    // Start setup once the window has finished loading
    setupWindow.webContents.on("did-finish-load", () => {
      startSetup();
    });

    // If the user closes the setup window, quit the app
    setupWindow.on("closed", () => {
      setupWindow = null;
      // If setup isn't complete yet, quit
      if (!isSetupComplete(pythonEnvDir)) {
        app.quit();
      }
    });
  });
}

// ============================================================
// MAIN WINDOW
// ============================================================

// ── Which renderer the main window loads ──
// The Vite dev server ONLY when the app is unpackaged AND AIO_DEV_SERVER_URL
// names it; everything else loads dist/index.html. `npm run electron:dev`
// (scripts/electron-dev.js) starts Vite and sets the variable to the port Vite
// actually bound. A bare `electron .` next to a hand-started `npm run dev`
// therefore loads dist: set AIO_DEV_SERVER_URL or use electron:dev.
// Why not the old "unpackaged → try localhost:5173, fall back to dist": any
// Vite on 5173, e.g. another checkout's, was loaded silently instead of this
// build, so the E2E harnesses (tools/_e2e_*.js) tested the wrong code, and
// electron:preview (NODE_ENV=production) never previewed dist. NODE_ENV no
// longer picks the renderer.
// Localhost only: the preload exposes powerful IPC and there is no
// will-navigate guard, so the URL must never name another machine.
// Returns the URL to load, or null for dist.
function devServerUrl() {
  const raw = process.env.AIO_DEV_SERVER_URL;
  if (!raw) {
    if (process.env.NODE_ENV === "development" && !IS_PACKAGED) {
      console.warn("[main] NODE_ENV=development without AIO_DEV_SERVER_URL: loading dist. `npm run electron:dev` runs the dev server.");
    }
    return null;
  }
  if (IS_PACKAGED) {
    console.warn("[main] AIO_DEV_SERVER_URL is ignored in a packaged app");
    return null;
  }
  let u;
  try {
    u = new URL(raw);
  } catch {
    console.error(`[main] AIO_DEV_SERVER_URL is not a URL (${raw}); loading dist`);
    return null;
  }
  // WHATWG URL keeps the brackets on an IPv6 hostname.
  const localHost = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
  if (!/^https?:$/.test(u.protocol) || !localHost) {
    console.error(`[main] AIO_DEV_SERVER_URL must be http(s) on localhost (${raw}); loading dist`);
    return null;
  }
  return u.href;
}

// Shown instead of a blank window when an unpackaged run has no build.
const MISSING_DIST_HTML =
  "<!doctype html><meta charset=utf-8><title>No build</title>" +
  "<body style='font:14px sans-serif;padding:2em'><h2>No renderer build</h2>" +
  "<p><code>UI-source/dist/index.html</code> is missing. Run <code>npm run build</code>, " +
  "or <code>npm run electron:dev</code> for the dev server.</p>";

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 750,
    minWidth: 800,
    minHeight: 550,
    frame: true,
    icon: APP_ICON_PATH,
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#181b22" : "#fafafa",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // Dev server or dist: see devServerUrl. No silent fallback from one to the
  // other — a failed dev load stays visible and logged. The URL is logged on
  // every load; tools/_e2e_launch.js asserts the same thing from the outside.
  mainWindow.webContents.on("did-finish-load", () => {
    console.log(`[main] renderer: ${mainWindow.webContents.getURL()}`);
  });
  const devUrl = devServerUrl();
  const distIndex = path.join(__dirname, "..", "dist", "index.html");
  if (devUrl) {
    mainWindow.loadURL(devUrl).catch((err) => {
      console.error(`[main] dev server ${devUrl} failed to load: ${err.message}`);
    });
  } else if (fs.existsSync(distIndex)) {
    mainWindow.loadFile(distIndex);
  } else {
    console.error(`[main] ${distIndex} is missing; run npm run build`);
    mainWindow.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(MISSING_DIST_HTML));
  }

  // ── Confirm on close while a download is running ──
  // ONLY when something is actively running. Queued items now survive the
  // close (download_queue.json — grep queue:save), so a backlog is not worth a
  // prompt, and prompting on it would fire on nearly every close while a queue
  // drains. See the quitConfirmed declaration for why this is a renderer
  // dialog rather than dialog.showMessageBox.
  // Device Sync asks only while an apply, prune or rename runs; a plan or
  // verify is just cancelled by the quit (sync/contract.js QUIT_ASK_PHASES).
  // payload.sync is {target, phase, unit, done, total} or null (service.js
  // quitInfo); `running` keeps its shape, and ConfirmQuitDialog renders both.
  mainWindow.on("close", (e) => {
    if (quitConfirmed) return;
    const downloadsRunning = !!downloader && downloader.runningCount() > 0;
    let syncAsk = false;
    try {
      syncAsk = !!deviceSync?.needsQuitAsk();
    } catch {}
    if (!downloadsRunning && !syncAsk) return;
    e.preventDefault();
    let sync = null;
    try {
      sync = syncAsk ? deviceSync.quitInfo() : null;
    } catch {}
    sendToUI("confirm-quit", { running: downloader?.getRunning?.() ?? [], sync });
    // Safety valve: a wedged renderer (crashed React tree, blocked main thread)
    // must never leave the window un-closable. 15s is far longer than the
    // dialog needs to paint, and "quit:cancel" clears it the moment the user
    // actually answers.
    clearQuitSafetyTimer();
    quitSafetyTimer = setTimeout(() => {
      quitSafetyTimer = null;
      quitConfirmed = true;
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
    }, 15_000);
  });

  applyTheme();
  nativeTheme.on("updated", applyTheme);
}

function applyTheme() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const isDark = nativeTheme.shouldUseDarkColors;
  mainWindow.webContents.executeJavaScript(
    `document.documentElement.classList.${isDark ? "add" : "remove"}("dark")`
  );
  sendToUI("theme-changed", isDark ? "dark" : "light");
}

// ============================================================
// DOWNLOADER INITIALIZATION
// ============================================================

function initDownloader() {
  // Build the extra environment variables for the Python process
  // (PLAYWRIGHT_BROWSERS_PATH + Unix PYTHONPATH — see buildPythonEnv).
  // No PYTHONUNBUFFERED here: the Downloader/Searcher _spawn path adds it
  // at spawn time, so the constructors want just the Playwright/PYTHONPATH pair.
  const extraEnv = buildPythonEnv();

  downloader = new Downloader({
    extraEnv,
    onLog: (downloadId, line, level) => {
      sendToUI("download-log", { downloadId, line, level });
    },
    onProgress: (downloadId, progress) => {
      sendToUI("download-progress", { downloadId, progress });
    },
    onComplete: (downloadId, result) => {
      history.updateEntry(downloadId, result);
      sendToUI("download-complete", { downloadId, result });
      // Must not throw: downloader.js resolves the close wait after this.
      notifySyncLibraryChanged("download");
    },
  });

  // Cross-site search subprocess invoker — separate from `downloader` because
  // search is a single blocking request/response (not a long-lived stream
  // with progress events) and lives on different IPC channels.
  //
  // Searcher gets the SAME extraEnv as Downloader (PLAYWRIGHT_BROWSERS_PATH
  // in packaged mode). Without this, search would still run but Playwright-
  // using handlers (comix, violetscans, rizzfables, mangathemesia with
  // use_playwright=True) would silently fail and drop out of the candidate
  // list — making search results in installed builds inferior to dev builds.
  searcher = new Searcher({
    extraEnv,
    onLog: (searchId, line, level) => {
      sendToUI("search-log", { searchId, line, level });
    },
  });
}

// ============================================================
// IPC HANDLERS
// ============================================================

function setupIPC() {
  // ── Get settings ──
  // Returns ONLY what was actually saved on disk plus genuine user-pref
  // defaults (verboseAlways, logUpdateInterval, defaults, searchOpts).
  //
  // CRITICAL: This handler used to merge defaultPythonCmd / defaultScriptPath
  // / defaultWorkingDir into the response. That round-trip caused AppImage
  // (random /tmp/.mount_*/ paths), macOS Gatekeeper App Translocation, and
  // .app-launched-from-DMG users to persist volatile paths to settings.json
  // — paths that no longer existed on the next launch and produced ENOENT
  // on every spawn. The renderer now hydrates the path FIELDS from the new
  // get-resolved-paths IPC below; when settings.json has an empty/missing
  // value, every existing spawn site falls back to defaultX via
  // `settings.X || defaultX` (empty string is falsy), so the consumer side
  // works unchanged. See history.js:saveSettings for the defense-in-depth
  // write-side volatile-path filter that prevents the regression from
  // re-emerging via a future code path that does happen to send a stale
  // path back.
  ipcMain.handle("get-settings", async () => {
    const saved = history.getSettings();
    let syncKeys = {};
    try {
      syncKeys = syncSettingsForRenderer(saved);
    } catch (e) {
      console.error("[deviceSync] get-settings", e);
    }
    return {
      ...saved,
      // Device Sync's eight global keys, resolved like appAutoUpdate below
      // and for the same reason (no phantom dirty count on first launch).
      // Owner: sync/sync-settings.js SYNC_SETTING_SPECS.
      ...syncKeys,
      // Genuine user-pref defaults — these have meaningful "missing →
      // assume default" semantics and stay merged here. SettingsTab.jsx
      // owns the DEFAULT_DOWNLOAD_DEFAULTS dict; we just pass through
      // whatever the user has actually saved (or an empty object so
      // SettingsTab can hydrate from its own defaults on first load).
      defaults: saved.defaults || {},
      verboseAlways: saved.verboseAlways !== false,
      // App self-update is OPT-OUT: absent means ON. Resolved here rather than
      // left to SettingsTab's DEFAULT_SETTINGS so main is the single source of
      // truth (the updater arms off `saved` at startup, before the renderer
      // exists) AND so countDirtySettings stays honest — the renderer's draft
      // and the hydrated prop agree on the same resolved boolean instead of
      // reporting a phantom pending change for a key that was never saved.
      appAutoUpdate: saved.appAutoUpdate !== false,
      logUpdateInterval: saved.logUpdateInterval || 100,
      isPackaged: IS_PACKAGED,
      // NOTE: intentionally NOT merging pythonCmd / scriptPath / workingDir.
      // The renderer calls get-resolved-paths separately for display, and
      // saves an empty string when the user hasn't customized those fields.
    };
  });

  // ── Get resolved paths (display only) ──
  // Returns the currently-resolved Python command, aio-dl.py script path,
  // and working directory. The renderer uses these for placeholder text
  // in the Settings UI so users see what's auto-resolved without those
  // values getting saved to settings.json. Pure-read; no side effects.
  // This handler is the structural fix to the AppImage / Gatekeeper
  // App Translocation / DMG-direct stale-path bug — see get-settings
  // above for the full story.
  ipcMain.handle("get-resolved-paths", async () => {
    return {
      pythonCmd: defaultPythonCmd,
      scriptPath: defaultScriptPath,
      workingDir: defaultWorkingDir,
    };
  });

  // ── Save settings ──
  // Validation lives in history.saveSettings — see that method's
  // docstring for the volatile-path filter that rejects bad writes
  // even if main.js / the renderer ever regresses.
  ipcMain.handle("save-settings", async (_event, newSettings) => {
    history.saveSettings(newSettings);
    // Live-sync the app self-updater with the (post-merge) saved prefs:
    // enabling arms a check immediately, disabling also flips
    // autoInstallOnAppQuit off so a downloaded update won't install on
    // quit after an opt-out, and a maturity-delay change re-evaluates a
    // deferred release. See electron/updater.js:applySettings.
    const merged = history.getSettings();
    appUpdater.applySettings({
      // `!== false` — opt-OUT. Must match the get-settings resolution above
      // and the initAppUpdater call at startup; updater.js itself only ever
      // sees a resolved boolean.
      enabled: merged.appAutoUpdate !== false,
      delayDays: merged.appUpdateDelayDays,
    });
    // Fire-and-forget with its own catch: a sync defect never turns a
    // successful save into a rejected IPC call. Starts or stops the monitor
    // and pre-warm; turning sync off cancels a running job first.
    try {
      Promise.resolve(deviceSync?.applySettings(merged)).catch((e) => console.error("[deviceSync] applySettings", e));
    } catch (e) {
      console.error("[deviceSync] applySettings", e);
    }
    return { ok: true };
  });

  // ── Start a download ──
  ipcMain.handle("start-download", async (_event, { url, args }) => {
    const settings = history.getSettings();
    const { pythonCmd, scriptPath, workingDir } = resolveSpawnPaths(settings);

    // Create the working directory on-demand (not at startup, so we don't
    // leave an empty "AIO Downloader" folder if the user never downloads)
    try { fs.mkdirSync(workingDir, { recursive: true }); } catch {}

    // Resource Limits (hard override): a Max-network preset REPLACES the
    // image-concurrency family; a Max-CPU preset sets --max-cpu-percent (only
    // when < 100 so Unlimited spawns stay byte-identical to before). Applied
    // here — the single main-side spawn chokepoint — so every download path
    // (New tab, search result, library UpdatesCenter) honors the current level.
    let throttledArgs = applyNetworkLimit(args, settings.networkLimit);
    const cpuPct = cpuPercentForLevel(settings.cpuLimit);
    if (cpuPct < 100) {
      throttledArgs = { ...throttledArgs, maxCpuPercent: cpuPct };
    }

    const downloadId = downloader.start({
      pythonCmd,
      scriptPath,
      workingDir,
      url,
      args: throttledArgs,
    });

    return { downloadId };
  });

  // ── Cancel a running download ──
  ipcMain.handle("cancel-download", async (_event, downloadId) => {
    downloader.cancel(downloadId);
    return { ok: true };
  });

  // ── Cross-site search ──
  // Single blocking call: spawn aio-dl.py --search, accumulate stdout,
  // return parsed JSON when child exits. stderr lines stream live to the
  // UI via 'search-log' events. UI shows results when this resolves.
  // requestId (renderer-minted, see hooks/useSearch.js) is passed on as the
  // log lines' searchId; anything that isn't a short string is ignored and
  // the Searcher mints its own.
  ipcMain.handle("search:run", async (_event, { query, opts, requestId }) => {
    const settings = history.getSettings();
    const { pythonCmd, scriptPath, workingDir } = resolveSpawnPaths(settings);
    try { fs.mkdirSync(workingDir, { recursive: true }); } catch {}

    // Resource Limits: a Max-network preset caps the search fan-out
    // (--search-parallelism). Hard override — the preset wins when a level is
    // active; Unlimited passes the caller's value through (null → Python default 6).
    const throttledOpts = {
      ...opts,
      searchParallelism: searchParallelismForLevel(
        opts ? opts.searchParallelism : undefined,
        settings.networkLimit,
      ),
    };

    try {
      const result = await searcher.runSearch({
        pythonCmd,
        scriptPath,
        workingDir,
        query,
        opts: throttledOpts,
        searchId: typeof requestId === "string" && requestId.length <= 64 ? requestId : undefined,
      });
      return { ok: true, result };
    } catch (err) {
      return { ok: false, error: err.message, cancelled: !!err.cancelled };
    }
  });

  ipcMain.handle("search:cancel", async () => {
    const wasRunning = searcher.cancel();
    return { ok: true, wasRunning };
  });

  // ── Resume a download ──
  ipcMain.handle("resume-download", async (_event, { url, tmpDir, format, epubLayout }) => {
    const settings = history.getSettings();
    const { pythonCmd, scriptPath, workingDir } = resolveSpawnPaths(settings);

    // Resource Limits on resume: honor the CURRENT limit, NOT the value saved in
    // run_params.json — the resume may run in a different environment (slower
    // link, busier machine). resumeThrottleFlags emits concrete current values
    // (network 4 knobs + --max-cpu-percent); downloader.resume appends them to
    // the --restore-parameters spawn line and aio-dl.py keeps explicit CLI dests
    // over the restored ones (grep _user_set_dests). None are resume-gating, so
    // this never re-downloads completed chapters.
    const resourceFlags = resumeThrottleFlags(settings);

    const downloadId = downloader.resume({
      pythonCmd,
      scriptPath,
      workingDir,
      url,
      tmpDir,
      format,
      epubLayout,
      resourceFlags,
    });

    return { downloadId };
  });

  // ── Delete a temp folder ──
  ipcMain.handle("delete-temp", async (_event, tmpDir) => {
    try {
      if (fs.existsSync(tmpDir)) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // ── Scan for resumable downloads ──
  ipcMain.handle("scan-resumable", async () => {
    const settings = history.getSettings();
    const { workingDir } = resolveSpawnPaths(settings);
    const resumable = downloader.scanResumable(workingDir);

    const allHistory = history.getAll();
    return resumable.map((item) => {
      const histEntry = allHistory.find((h) => h.hid === item.hid);
      return {
        ...item,
        url: item.url || histEntry?.url || null,
        title: item.title || histEntry?.title || null,
      };
    });
  });

  // ── Get download history ──
  ipcMain.handle("get-history", async () => {
    return history.getAll();
  });

  // ── Download queue snapshot (survives app close) ──
  // Pure pass-through to history.js, which owns the file + shape (grep
  // saveQueueSnapshot there). Renderer side: useDownloader.js hydrates on
  // mount and writes from a debounced effect — grep queueHydratedRef.
  ipcMain.handle("queue:get", async () => {
    return history.getQueueSnapshot();
  });

  ipcMain.handle("queue:save", async (_event, snapshot) => {
    history.saveQueueSnapshot(snapshot);
    return { ok: true };
  });

  // ── Quit confirmation (ConfirmQuitDialog.jsx) ──
  // The window "close" listener in createWindow() preventDefaults while a
  // download is running and pushes "confirm-quit"; these two are the answers.
  // The reinstall-python handler is deliberately UNAFFECTED: it uses
  // app.exit(0), which tears the process down without ever emitting a window
  // "close" event, so no prompt can appear there.
  ipcMain.handle("quit:confirm", async () => {
    clearQuitSafetyTimer();
    quitConfirmed = true;
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
    return { ok: true };
  });

  ipcMain.handle("quit:cancel", async () => {
    clearQuitSafetyTimer();
    return { ok: true };
  });

  // ── Reveal a folder in the OS file manager (Explorer / Finder / Files) ──
  ipcMain.handle("open-folder", async (_event, folderPath) => {
    shell.openPath(folderPath);
    return { ok: true };
  });

  // ── Folder picker dialog ──
  ipcMain.handle("pick-folder", async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ["openDirectory"],
    });
    if (result.canceled) return null;
    return result.filePaths[0];
  });

  // ── File picker dialog ──
  ipcMain.handle("pick-file", async (_event, filters) => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ["openFile"],
      filters: filters || [{ name: "Python Scripts", extensions: ["py"] }],
    });
    if (result.canceled) return null;
    return result.filePaths[0];
  });

  ipcMain.handle("metadata:read", async (_event, filePath) => {
    return runMetadataCli(["read", filePath]);
  });

  ipcMain.handle("metadata:update", async (_event, filePath, data, coverPath) => {
    const r = await runMetadataCli(
      ["update", filePath, ...(coverPath ? ["--cover-path", coverPath] : [])],
      JSON.stringify(data || {})
    );
    notifySyncLibraryChanged("metadata");
    return r;
  });

  // ── comix.to sign-in ──
  // Spawns `aio-dl.py --comix-login`, which opens comix.to in the downloader's
  // own persistent Chromium profile and blocks until the user signs in (or the
  // 300s wait elapses). The session is written into that profile and inherited
  // by every later download/search process.
  //
  // The user types their credentials into the real comix.to page in a real
  // browser window. Nothing about them is read, stored or forwarded by this app
  // — we only wait for the site's own `auth` key to appear. Cross-file:
  // sites/comix.py:open_login_window, aio-dl.py --comix-login.
  //
  // stdio is inherited-free (piped and discarded): the banner the child prints
  // is for CLI users, and the renderer already knows what it asked for. Only the
  // exit code matters here — 0 = signed in, 2 = not completed.
  ipcMain.handle("comix:login", async () => {
    const settings = history.getSettings();
    const { pythonCmd, scriptPath, workingDir } = resolveSpawnPaths(settings);
    return new Promise((resolve) => {
      let proc;
      try {
        proc = spawn(pythonCmd, [scriptPath, "--comix-login"], {
          cwd: workingDir,
          env: { ...process.env, PYTHONUNBUFFERED: "1" },
          stdio: ["ignore", "pipe", "pipe"],
        });
      } catch (err) {
        resolve({ ok: false, reason: err.message || String(err) });
        return;
      }
      let stderr = "";
      proc.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
      proc.on("error", (err) => {
        resolve({ ok: false, reason: err.message || String(err) });
      });
      proc.on("close", (code) => {
        resolve({
          ok: code === 0,
          reason: code === 0 ? "" : (stderr.trim() || `exited with ${code}`),
        });
      });
    });
  });

  // ── Scan library (read all downloaded manga) ──
  // Returns the list immediately. Missing thumbnails are generated
  // in the background using mupdf (WASM) in the main process.
  // As each thumbnail completes, a 'library-thumb-ready' event is
  // sent to the renderer so it can update that card's cover image.
  ipcMain.handle("scan-library", async () => {
    const settings = history.getSettings();
    const { workingDir } = resolveSpawnPaths(settings);
    const mangaDir = getConfiguredOutputRoot(workingDir);
    const thumbCacheDir = path.join(app.getPath("userData"), "thumb-cache");

    let entries;
    try {
      // Fast scan — just reads folder listings, no heavy processing
      entries = scanLibrary(mangaDir, thumbCacheDir);
    } catch (err) {
      console.error("scanLibrary failed:", err);
      return [];
    }

    // Sweep cover_<hash>.jpg files no entry references anymore. A series
    // whose cover URL changes (publisher relicense, CDN reshuffle) leaves
    // an orphan file with the old hash; without this sweep, the thumb-
    // cache directory grows unbounded across months of metadata refreshes.
    // Cheap (~one readdir + a handful of unlinks); runs synchronously so
    // a subsequent download phase doesn't race with our deletes.
    try {
      const removed = cleanupOrphanCovers(entries, thumbCacheDir);
      if (removed > 0) {
        console.log(`Library: cleaned up ${removed} orphan cover file(s).`);
      }
    } catch (err) {
      console.warn("Library: orphan-cover cleanup failed:", err.message);
    }

    // ── Phase 1: Download official cover images from the web ──
    // These are small (50-200 KB) JPEGs from the manga site — much faster
    // than rendering a 500 MB PDF with mupdf. Downloads covers for any
    // entry that has a cover URL but hasn't cached the web version yet
    // (even if an old PDF-rendered thumb already exists).
    try {
      const needWebCovers = entries
        .filter((e) => e.seriesMeta?.cover && !e.webCoverCached)
        .map((e) => ({ coverUrl: e.seriesMeta.cover, folderPath: e.folderPath }));

      // Track which folders got a web cover so we skip them in phase 2
      const coveredFolders = new Set();

      if (needWebCovers.length > 0) {
        downloadMissingCovers(needWebCovers, thumbCacheDir, (folderPath, coverPath) => {
          coveredFolders.add(folderPath);
          sendToUI("library-thumb-ready", { folderPath, thumbPath: coverPath });
        }).then(() => {
          // ── Phase 2: Fall back to mupdf PDF rendering ──
          // Only for entries with no thumb at all (no web cover URL or
          // download failed, AND no existing PDF thumb).
          const needPdfThumbs = entries
            .filter((e) => e.coverPdfPath && !e.thumbPath && !coveredFolders.has(e.folderPath))
            .map((e) => ({ pdfPath: e.coverPdfPath, folderPath: e.folderPath }));

          if (needPdfThumbs.length > 0) {
            generateMissingThumbnails(needPdfThumbs, thumbCacheDir, (folderPath, thumbPath) => {
              sendToUI("library-thumb-ready", { folderPath, thumbPath });
            }).catch((err) => {
              console.error("Thumbnail generation error:", err);
            });
          }
        }).catch((err) => {
          console.error("Cover download error:", err);
        });
      } else {
        // No web covers needed — go straight to mupdf fallback
        const needThumbs = entries
          .filter((e) => e.coverPdfPath && !e.thumbPath)
          .map((e) => ({ pdfPath: e.coverPdfPath, folderPath: e.folderPath }));

        if (needThumbs.length > 0) {
          generateMissingThumbnails(needThumbs, thumbCacheDir, (folderPath, thumbPath) => {
            sendToUI("library-thumb-ready", { folderPath, thumbPath });
          }).catch((err) => {
            console.error("Thumbnail generation error:", err);
          });
        }
      }
    } catch (err) {
      console.error("Thumbnail pipeline error:", err);
    }

    notifySyncLibraryChanged("scan");
    return entries;
  });

  // ── Open a file with the system default app (e.g. PDF reader) ──
  ipcMain.handle("open-file", async (_event, filePath) => {
    const result = await shell.openPath(filePath);
    // shell.openPath returns "" on success, or an error string
    return { ok: !result, error: result || undefined };
  });

  // ── Delete a series folder from the library ──
  ipcMain.handle("delete-series", async (_event, folderPath) => {
    try {
      if (fs.existsSync(folderPath)) {
        fs.rmSync(folderPath, { recursive: true, force: true });
      }
      notifySyncLibraryChanged("delete");
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // ── Helper: check a single series for new chapters ──
  // Extracted so both check-for-updates and check-all-updates can use it.
  // Spawns Python with --list-chapters to get the current chapter list from
  // the site, compares with .aio_series.json, returns the diff.
  // Per-series chapter-update check. Spawns `aio-dl.py --list-chapters` and
  // diffs the returned chapter set against either meta.chapters_downloaded
  // (default) or filenames on disk (settings.useFileBasedChapterCheck).
  //
  // opts:
  //   collapseSplits — when true, forwards --collapse-splits so the chapter
  //     list mirrors what the actual download path would produce. Without
  //     this, fragment-shaped decimals (52.1 next to 52, etc.) leak into
  //     the diff and stick as "+N new" indefinitely.
  //   signal — AbortSignal from the parallel "Check All" worker pool. When
  //     aborted, the in-flight Python proc is killed and the promise
  //     rejects with an aborted-shape error that the caller normalizes
  //     into { error: "aborted" }.
  //
  // VOLUMES. A series tracks chapters, volumes or both (series-ledger.js
  // trackedKinds, from the ledger plus Vol files on disk). Each kind is listed
  // and diffed against ITS OWN namespace, so a MangaFire chapter series never
  // gets volume noise and a volume-only series never shows "+300 chapters":
  //   chapters only → the old single spawn, byte-identical
  //   volumes only  → one spawn with --download-volumes
  //   both          → one spawn with --with-volumes when both kinds use the
  //                   same language, else one spawn per kind
  // The volume half's "on device" set is ALWAYS ledger ∪ files on disk,
  // whatever useFileBasedChapterCheck says: an older build that rewrote the
  // ledger drops the volume block, and the files are then the only record.
  //
  //   includeVolumes — list volumes even if the series doesn't track them
  //     yet. The Library page's "Get volumes" uses it on a chapter-only
  //     MangaFire series; chapter tracking is unaffected.
  //   peerFolders — OTHER folders holding the SAME series (the forks a site
  //     rename used to create; grep seriesIdentityKey). Everything read off
  //     disk is unioned across the primary + peers, because a series split
  //     over two folders is still one series. Without it the 59-chapter half
  //     reports "5 new" while its 5-chapter fork reports "59 new", and
  //     neither number is true. Empty/absent for the ordinary lone series,
  //     which then costs exactly what it always did.
  // Volume labels on disk for one series folder: "<base> Vol.NN.<ext>" at the
  // root plus images/Volume_N dirs. `title` gives the exact base a volume run
  // writes (series-ledger.js volumeFileBase); the folder name stands in when
  // the ledger has none. Async twin of the sync scan in library.js.
  async function _volumeLabelsOnDisk(folderPath, title) {
    const base =
      seriesLedger.volumeFileBase(title) || seriesLedger.volumeFileBase(path.basename(folderPath));
    const out = [];
    try {
      for (const f of await fs.promises.readdir(folderPath, { withFileTypes: true })) {
        if (!f.isFile()) continue;
        const n = seriesLedger.parseVolumeNumberFromName(f.name, base);
        if (n !== null) out.push(n);
      }
    } catch {}
    try {
      const imagesDir = path.join(folderPath, "images");
      for (const d of await fs.promises.readdir(imagesDir, { withFileTypes: true })) {
        const n = d.isDirectory() ? seriesLedger.parseRawVolumeDir(d.name) : null;
        if (n !== null) out.push(n);
      }
    } catch {}
    return out;
  }

  async function _checkSeriesUpdates(folderPath, opts = {}) {
    const metaPath = path.join(folderPath, ".aio_series.json");
    // fs.promises.access throws on missing — translate to the no-metadata
    // sentinel without surfacing an exception. Async FS keeps the IPC handler
    // thread responsive when "Check All" iterates many series.
    try {
      await fs.promises.access(metaPath);
    } catch {
      return { error: "no_metadata" };
    }

    let meta;
    try {
      meta = JSON.parse(await fs.promises.readFile(metaPath, "utf8"));
    } catch {
      return { error: "invalid_metadata" };
    }

    if (!meta.url) {
      return { error: "no_url" };
    }

    // Primary first, so a lone series takes the identical path it always has.
    // An unreadable peer contributes {} rather than failing the check — the
    // primary's own answer is still worth reporting.
    const peerFolders = Array.isArray(opts.peerFolders) ? opts.peerFolders : [];
    const members = [{ folderPath, meta }];
    for (const peer of peerFolders) {
      let peerMeta = {};
      try {
        const parsed = JSON.parse(
          await fs.promises.readFile(path.join(peer, ".aio_series.json"), "utf8")
        );
        if (parsed && typeof parsed === "object") peerMeta = parsed;
      } catch {}
      members.push({ folderPath: peer, meta: peerMeta });
    }

    const settings = history.getSettings();
    const { pythonCmd, scriptPath, workingDir } = resolveSpawnPaths(settings);

    if (!fs.existsSync(scriptPath)) {
      return { error: "no_script", message: "aio-dl.py not found at " + scriptPath };
    }

    // Which kinds to list (see VOLUMES above), unioned across fork members.
    const kindsTracked = new Set();
    const volumeDiskLabels = new Set();
    for (const member of members) {
      const onDisk = await _volumeLabelsOnDisk(member.folderPath, member.meta?.title || meta.title);
      for (const v of onDisk) volumeDiskLabels.add(v);
      for (const k of seriesLedger.trackedKinds(member.meta, { volumeFilesPresent: onDisk.length > 0 })) {
        kindsTracked.add(k);
      }
    }
    if (opts.includeVolumes) kindsTracked.add("volume");
    const wantChapters = kindsTracked.has("chapter");
    const wantVolumes = kindsTracked.has("volume");
    const chapterLang = meta.language || "en";
    const volumeLang = seriesLedger.kindView(meta, "volume").language || chapterLang;

    try {
      // One `--list-chapters` spawn. `extraFlags` picks the kind
      // (--download-volumes / --with-volumes); `language` is that kind's own.
      const runList = (extraFlags, language) => new Promise((resolve, reject) => {
        const args = [
          "-u", scriptPath,
          "--list-chapters",
          "--verbose",
        ];
        if (language && language !== "en") {
          args.push("--language", language);
        }
        if (meta.site) {
          args.push("--site", meta.site);
        }
        args.push(...extraFlags);
        // Collapse-splits forwarding: when the user has the global
        // collapseSplits setting on, --list-chapters now applies the same
        // group_chapters_for_download filter the download path uses
        // (aio-dl.py lines around `if collapse_splits_enabled:` in the
        // --list-chapters block). Without forwarding, the emitted list
        // would include fragment-shape decimals that would never download
        // under collapse, causing the diff against meta.chapters_downloaded
        // to flag them as new forever. Cross-file: settings.collapseSplits
        // is the source of truth (SettingsTab.jsx default true).
        // Volume numbers have no fragments; the engine forces collapse off in
        // volume mode anyway, so the flag is only sent with a chapter list.
        if (opts.collapseSplits && !extraFlags.includes("--download-volumes")) {
          args.push("--collapse-splits");
        }
        args.push(meta.url);

        // Build env — Playwright path for packaged mode, plus PYTHONPATH
        // on Unix (mirrors initDownloader; needed because the bundled
        // python-src lives under resources/ and won't be on sys.path
        // otherwise on standard CPython). Windows uses ._pth instead.
        // unbuffered:true folds in PYTHONUNBUFFERED since this spawn adds
        // env inline (no separate PYTHONUNBUFFERED at the spawn call).
        const extraEnv = buildPythonEnv({ unbuffered: true });

        const proc = spawn(pythonCmd, args, {
          cwd: workingDir,
          stdio: ["ignore", "pipe", "pipe"],
          windowsHide: true,
          env: { ...process.env, ...extraEnv },
        });

        let stdout = "";
        let stderr = "";
        let aborted = false;

        proc.stdout.on("data", (chunk) => { stdout += chunk.toString("utf8"); });
        proc.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });

        // 60 second timeout — chapter listing should be fast
        const timeout = setTimeout(() => {
          proc.kill();
          reject(new Error("Timed out after 60 seconds"));
        }, 60000);

        // Parallel "Check All" cancel path: when the worker pool's
        // AbortController fires, kill this proc immediately and reject with
        // an aborted sentinel that the outer await normalizes into a
        // { error: "aborted" } result. Idempotent (proc.kill on a dead
        // proc is a no-op). The listener is bound here, AFTER the spawn,
        // so we don't try to kill before the proc handle exists.
        const onAbort = () => {
          aborted = true;
          clearTimeout(timeout);
          try { proc.kill(); } catch {}
          reject(new Error("aborted"));
        };
        if (opts.signal) {
          if (opts.signal.aborted) { onAbort(); return; }
          opts.signal.addEventListener("abort", onAbort, { once: true });
        }

        proc.on("close", (code) => {
          clearTimeout(timeout);
          if (code !== 0) {
            reject(new Error(stderr.trim() || `Python exited with code ${code}`));
            return;
          }

          // stdout may contain log lines before the JSON.
          // The JSON is always the LAST line printed by --list-chapters.
          const lines = stdout.trim().split("\n");
          let jsonData = null;
          for (let i = lines.length - 1; i >= 0; i--) {
            const line = lines[i].trim();
            if (line.startsWith("{")) {
              try {
                jsonData = JSON.parse(line);
                break;
              } catch {
                // Not valid JSON, keep searching
              }
            }
          }

          if (!jsonData) {
            reject(new Error("No JSON output from --list-chapters"));
            return;
          }
          resolve(jsonData);
        });

        proc.on("error", (err) => {
          clearTimeout(timeout);
          reject(err);
        });
      });

      // ── Spawn plan (see VOLUMES in the header) ──
      // `result` is the chapter listing (and the series fields: status, cover…);
      // `volumeList` is the volume labels, or null when listing them failed.
      let result;
      let volumeList = null;
      let volumesError = null;
      if (!wantVolumes) {
        result = await runList([], chapterLang);
      } else if (!wantChapters) {
        result = await runList(["--download-volumes"], volumeLang);
        volumeList = result.volumes || [];
      } else if (volumeLang === chapterLang) {
        result = await runList(["--with-volumes"], chapterLang);
        // --with-volumes reports volumes:null when listing them raised, and
        // still returns the chapter half (aio-dl.py _series_listing_head).
        if (Array.isArray(result.volumes)) volumeList = result.volumes;
        else volumesError = "Couldn't list volumes";
      } else {
        result = await runList([], chapterLang);
        try {
          volumeList = (await runList(["--download-volumes"], volumeLang)).volumes || [];
        } catch (err) {
          if (opts?.signal?.aborted) throw err;
          volumesError = err?.message || "Couldn't list volumes";
        }
      }
      if (!wantChapters) result = { ...result, chapters: [] };

      // ── Determine which chapters are "on device" ──
      // Two modes controlled by settings.useFileBasedChapterCheck:
      //   false (default): trust .aio_series.json's chapters_downloaded list
      //   true:  scan actual files on disk, extract chapter numbers from
      //          filenames (reversing the ~ → . convention for partials)
      const useFileBased = !!settings.useFileBasedChapterCheck;
      let downloadedChapters;
      let checkMode;

      if (useFileBased) {
        // Scan each member folder for actual files and extract chapter numbers.
        // For individual files like "Title Ch 5~5.pdf" → chapter "5.5"
        // For combined range files like "Title Ch 1-50.pdf" → all site
        // chapters in [1, 50] are considered present on device.
        const OUTPUT_EXTS = new Set(["pdf", "epub", "cbz"]);
        const siteChapterList = (result.chapters || []).map(String);
        downloadedChapters = new Set();
        for (const member of members) {
          let diskFiles = [];
          try {
            const contents = await fs.promises.readdir(member.folderPath, {
              withFileTypes: true,
            });
            diskFiles = contents
              .filter((f) => f.isFile() && !f.name.startsWith("."))
              .filter((f) => {
                const ext = path.extname(f.name).toLowerCase().slice(1);
                return OUTPUT_EXTS.has(ext);
              })
              .map((f) => ({ name: f.name }));
          } catch {}

          for (const c of getChaptersOnDevice(diskFiles, siteChapterList)) {
            downloadedChapters.add(c);
          }
          // --format none (image-only) series have no archive files for
          // getChaptersOnDevice to read, so the diff above would mark every
          // chapter "new". Recover the on-device set from the
          // images/Chapter_<n>/ tree and union it in. (JSON mode below is
          // unaffected — it reads chapters_downloaded, which aio-dl.py writes
          // for every format including none.) grep getImageChaptersOnDevice.
          if (diskFiles.length === 0) {
            for (const c of getImageChaptersOnDevice(member.folderPath)) {
              downloadedChapters.add(c);
            }
          }
        }
        checkMode = "files";
      } else {
        // Use the JSON metadata list (what aio-dl.py recorded as downloaded)
        downloadedChapters = new Set();
        for (const member of members) {
          for (const c of member.meta.chapters_downloaded || []) {
            downloadedChapters.add(String(c));
          }
        }
        checkMode = "json";
      }

      // Fragment labels the download path dropped under multi-source consensus
      // (mangafire's duplicate .1-.4 sitting next to the integer floor).
      // --list-chapters runs consensus-free, so it re-lists them; without this
      // subtraction they'd show as a perpetual "+N new" that a "Download Missing"
      // click would refetch as duplicates. Applied in BOTH check modes — a
      // skipped fragment is never on disk, so the file-based path needs it too.
      // Cross-file: aio-dl.py writes chapters_skipped_fragments into
      // .aio_series.json (grep _skipped_fragment_labels).
      // Unioned across members for the same reason the downloaded set is: a
      // fragment the fork's run merged away is merged away for the series.
      const skippedFragments = new Set();
      for (const member of members) {
        for (const c of member.meta.chapters_skipped_fragments || []) {
          skippedFragments.add(String(c));
        }
      }

      // Chapters the user crossed out in the Updates Center. DELIBERATELY NOT
      // subtracted the way skippedFragments is: these stay in the reported set
      // (as `ignoredChapters`) so the panel can render them struck through and
      // the user can undo one. They are only withheld from `newChapters`, which
      // is what every download button queues. grep chapters_ignored.
      const ignoredSet = new Set();
      for (const member of members) {
        for (const c of member.meta.chapters_ignored || []) ignoredSet.add(String(c));
      }

      // Compare: site chapters (minus intentionally-skipped fragments) not yet
      // on device = missing/new.
      const siteChapters = new Set((result.chapters || []).map(String));
      const relevantSiteChapters = [...siteChapters].filter((ch) => !skippedFragments.has(ch));
      const byNumber = (a, b) => parseFloat(a) - parseFloat(b);
      const missingChapters = relevantSiteChapters.filter((ch) => !downloadedChapters.has(ch));
      // Split rather than filter, so the two arrays partition the missing set
      // and `newChapters.length + ignoredChapters.length` is the honest "how
      // many chapters this series is behind by".
      const newChapters = missingChapters.filter((ch) => !ignoredSet.has(ch)).sort(byNumber);
      // Intersected with what is actually missing on the site, so a stale
      // cross-out — chapter pulled from the site, or since downloaded by hand —
      // stops being rendered instead of accumulating forever. The on-disk list
      // is left alone; aio-dl.py prunes it against chapters_downloaded on the
      // next download of this series.
      const ignoredChapters = missingChapters.filter((ch) => ignoredSet.has(ch)).sort(byNumber);

      // ── Volume diff (own namespace; see VOLUMES in the header) ──
      let volumeFields = {};
      if (wantVolumes) {
        const haveVolumes = new Set(volumeDiskLabels);
        const ignoredVolSet = new Set();
        for (const member of members) {
          const view = seriesLedger.kindView(member.meta, "volume");
          for (const v of view.downloaded) haveVolumes.add(v);
          for (const v of view.ignored) ignoredVolSet.add(v);
        }
        const siteVolumes = (volumeList || []).map(seriesLedger.itemLabel);
        const missingVolumes = siteVolumes.filter((v) => !haveVolumes.has(v));
        volumeFields = {
          newVolumes: missingVolumes.filter((v) => !ignoredVolSet.has(v)).sort(byNumber),
          ignoredVolumes: missingVolumes.filter((v) => ignoredVolSet.has(v)).sort(byNumber),
          volumesTotal: siteVolumes.length,
          volumesDownloaded: haveVolumes.size,
          ...(volumesError ? { volumesError } : {}),
        };
      }

      return {
        ok: true,
        trackedKinds: [...kindsTracked],
        newChapters,
        ignoredChapters,
        ...volumeFields,
        total: relevantSiteChapters.length,
        downloaded: downloadedChapters.size,
        checkMode,
        status: result.status || meta.status,
        title: result.title || meta.title,
        updatedMeta: {
          status: result.status,
          authors: result.authors,
          cover: result.cover,
          genres: result.genres,
        },
      };
    } catch (err) {
      // Normalize the abort-shape error from the worker-pool cancel path so
      // the renderer can render an "aborted" pill instead of a generic
      // "check_failed" alarm. Any other failure (timeout, non-zero exit,
      // JSON parse) still surfaces as check_failed with the message.
      const msg = err?.message || String(err);
      if (msg === "aborted" || opts?.signal?.aborted) {
        return { error: "aborted" };
      }
      return { error: "check_failed", message: msg };
    }
  }

  // ── Which sites offer native volumes ──
  // `aio-dl.py --site-capabilities` prints [{name, domains, volumes}] from
  // sites.handler_capabilities(). Spawned ONCE per app session and cached:
  // the answer only changes with a Python update, and the renderer asks on
  // every URL edit. The renderer matches a URL's host against `domains`
  // (src/lib/siteCapabilities.js) instead of probing the URL, because a
  // MangaFire probe would launch its signer browser on every paste.
  // A failed spawn is NOT cached, so the next ask retries.
  let _siteCapabilitiesPromise = null;
  ipcMain.handle("site-capabilities", async () => {
    if (!_siteCapabilitiesPromise) {
      _siteCapabilitiesPromise = new Promise((resolve, reject) => {
        const settings = history.getSettings();
        const { pythonCmd, scriptPath, workingDir } = resolveSpawnPaths(settings);
        const proc = spawn(pythonCmd, ["-u", scriptPath, "--site-capabilities"], {
          cwd: workingDir,
          stdio: ["ignore", "pipe", "pipe"],
          windowsHide: true,
          env: { ...process.env, ...buildPythonEnv({ unbuffered: true }) },
        });
        let out = "";
        proc.stdout.on("data", (c) => { out += c.toString("utf8"); });
        const timer = setTimeout(() => { try { proc.kill(); } catch {} }, 60000);
        proc.on("error", (err) => { clearTimeout(timer); reject(err); });
        proc.on("close", (code) => {
          clearTimeout(timer);
          // The JSON is the last line; handler registration may log before it.
          const line = out.trim().split("\n").reverse().find((l) => l.trim().startsWith("["));
          try {
            if (code !== 0 || !line) throw new Error(`site-capabilities exited ${code}`);
            resolve(JSON.parse(line));
          } catch (err) {
            reject(err);
          }
        });
      }).catch((err) => {
        _siteCapabilitiesPromise = null;
        throw err;
      });
    }
    try {
      return { ok: true, sites: await _siteCapabilitiesPromise };
    } catch (err) {
      return { ok: false, error: err?.message || String(err) };
    }
  });

  // ── Check for new chapters for a single series ──
  // opts.includeVolumes: see _checkSeriesUpdates (Library "Get volumes").
  ipcMain.handle("check-for-updates", async (_event, folderPath, checkOpts = {}) => {
    // Single-series Check uses the global collapseSplits setting so the
    // result matches what a "Download Missing Chapters" click would produce.
    // No abort signal here — single check completes fast and can't be
    // user-cancelled from the DetailView (just close the panel).
    const settings = history.getSettings();
    const collapseSplits = settings.collapseSplits === true;
    return _checkSeriesUpdates(folderPath, {
      collapseSplits,
      includeVolumes: !!checkOpts?.includeVolumes,
    });
  });

  // ── Check for updates on all ongoing series (parallel) ──
  //
  // Worker pool, default 4 slots (settings.checkAllConcurrency, capped at
  // 8). Provider-aware scheduling: each worker picks a job whose `site` is
  // NOT currently held by a peer worker; falls back to FIFO only when every
  // candidate's site is in-flight (the steady state once concurrency ≥
  // unique-site count). This avoids piling 4 mangafire scans onto the same
  // CDN when the user happens to have many mangafire series, without
  // sacrificing throughput in the typical mixed-source library.
  //
  // THE SWEEP OUTLIVES THE LIBRARY TAB, so the renderer is NOT the record —
  // `_updateCheck` (declared near _checkAllAbortCtrl) is. App.jsx renders
  // tab bodies conditionally, so LibraryTab unmounts the moment the user
  // clicks Queue, and every piece of scan state it owned went with it. Three
  // rules follow, and the old handler broke all three:
  //   1. Rows are built HERE and shipped whole on `event.row`; the renderer
  //      only stores them. One reducer means a snapshot handed to a
  //      remounting tab and a live event can never disagree about a row.
  //   2. A re-entrant call while a sweep is running is REFUSED and answered
  //      with the snapshot — it does NOT restart. The old handler aborted
  //      the live controller and started from zero, which is exactly why a
  //      second "Check All" click re-checked 30 series that were already
  //      half done. Only opts.force (the panel's explicit Rescan) preempts.
  //   3. Every emission is stamped with runId and dropped once the run is
  //      superseded. A forced rescan preempts workers still awaiting Python;
  //      their late "completed"/"done" events would otherwise land in the
  //      NEW run's rows and flip it to done at ~0% progress.
  //
  // Cancelable: the renderer can fire cancel-check-all-updates to stop the
  // sweep mid-flight; the in-flight Python procs are killed via the
  // per-call AbortSignal threaded into _checkSeriesUpdates. Tracked via the
  // module-scoped _checkAllAbortCtrl (declared above this handler).
  //
  // Progress events: emits one "queued" per series upfront (renderer pre-
  // renders rows), then "running" / "completed" per worker hop, then a
  // final "done" event carrying total duration. Shapes are documented on
  // preload.js:onUpdateCheckProgress.
  // Sites whose Python handler drives a PERSISTENT Chromium profile, which
  // exactly one process may hold at a time (Chromium locks its user-data-dir).
  // Capped at one in-flight update check each — see claimNext/siteLimit below,
  // and sites/profile_lock.py for the measurement that forced this. Values are
  // the `site` strings stored in .aio_series.json, i.e. the Python handler
  // `name` attributes (sites/mangafire.py, sites/comix.py).
  const _SERIAL_BROWSER_SITES = new Set(["mangafire", "comix"]);

  ipcMain.handle("check-all-updates", async (_event, opts = {}) => {
    const force = !!(opts && opts.force);

    // Rule 2. Answer with the live snapshot so a renderer that lost its copy
    // (tab switch, window reload) resyncs onto the running sweep instead of
    // kicking off a duplicate one.
    if (_updateCheck.isRunning() && !force) {
      return {
        status: "already-running",
        runId: _updateCheck.runId(),
        snapshot: _updateCheck.snapshot(),
      };
    }

    const settings = history.getSettings();
    const { workingDir } = resolveSpawnPaths(settings);
    const mangaDir = getConfiguredOutputRoot(workingDir);
    const thumbCacheDir = path.join(app.getPath("userData"), "thumb-cache");

    // Filter to series with a source URL. When checkAllIncludeCompleted is
    // ON (default), we ignore the status field entirely — mangafire and
    // several other aggregators are deeply unreliable about marking series
    // "Completed" (they tag ongoing series as Completed all the time). The
    // cost of an unnecessary check is one extra Python proc per series,
    // negligible under the parallel pool; the cost of skipping a mislabeled
    // series is missing months of new chapters. Cross-file: setting is
    // declared in SettingsTab.jsx near useFileBasedChapterCheck; UI
    // LibraryTab.jsx mirrors this filter for the toolbar button's
    // ongoingCount badge so the count matches what we'd actually check.
    const entries = scanLibrary(mangaDir, thumbCacheDir);
    const includeCompleted = settings.checkAllIncludeCompleted !== false;
    const checkable = entries.filter((e) => {
      if (!e.seriesMeta?.url) return false;
      if (includeCompleted) return true;
      const status = e.seriesMeta.status;
      return !status || status === "Ongoing" || status === "Releasing";
    });

    // ── One job per SERIES, not per folder ──
    // A site rename used to fork a second folder for one series (grep
    // seriesIdentityKey), and checking the halves independently is what made
    // both of them lie: the 59-chapter folder reported "5 new" forever while
    // its 5-chapter fork reported "59 new". Grouping runs ONE --list-chapters
    // for the pair (their URL is identical, so the second call was pure waste
    // anyway) and diffs it against the union of what both hold. The richest
    // member becomes the row's folderPath, so a queued download targets the
    // folder holding the bulk of the series — and main hands that same folder
    // to Python as --series-dir so the two can't disagree about it.
    //
    // The overwhelming majority of groups have exactly one member, where this
    // is byte-for-byte the old behavior.
    const jobs = groupEntriesBySeries(checkable).map((group) => ({
      entry: group.primary,
      peerFolders: group.members
        .filter((m) => m !== group.primary)
        .map((m) => m.folderPath),
    }));

    // Supersede whatever came before. Past this point the record drops every
    // emission carrying an older runId, so a preempted run can no longer
    // write into the record it no longer owns.
    if (_checkAllAbortCtrl) {
      try { _checkAllAbortCtrl.abort(); } catch {}
      _checkAllAbortCtrl = null;
    }
    const { runId, startedAt } = _updateCheck.begin(jobs.length);

    if (jobs.length === 0) {
      _updateCheck.emit(runId, {
        kind: "done", completed: 0, total: 0, durationMs: 0, aborted: false,
      });
      return { status: "done", runId, results: [], total: 0, checked: 0 };
    }

    const concurrency = Math.max(
      1,
      Math.min(8, Number(settings.checkAllConcurrency) || 4),
    );
    const collapseSplits = settings.collapseSplits === true;

    _checkAllAbortCtrl = new AbortController();
    const ctrl = _checkAllAbortCtrl;

    // Pre-emit a "queued" row for every series so the renderer can render
    // the full row list immediately with placeholders. Total carried on
    // every event so the renderer doesn't need a separate "init" message.
    // enqueuedAt is one shared instant (this loop is synchronous) — FIFO
    // order survives because the record's Map keeps insertion order and the
    // panel's sort is stable.
    for (const job of jobs) {
      const e = job.entry;
      _updateCheck.emit(runId, {
        kind: "queued",
        row: {
          folderPath: e.folderPath,
          title: e.title,
          cover: e.seriesMeta?.cover || null,
          site: e.seriesMeta?.site || null,
          state: "queued",
          enqueuedAt: startedAt,
          ...(job.peerFolders.length ? { memberFolders: job.peerFolders } : {}),
        },
        completed: 0,
        total: jobs.length,
      });
    }

    const remaining = [...jobs];
    const inFlightBySite = new Map(); // site → count of workers currently checking
    const results = [];
    let completed = 0;

    // Provider-aware claim. findIndex+splice runs synchronously between
    // awaits so two workers can never claim the same job — the JS event
    // loop only re-enters on the next await.
    //
    // The FIFO fallback is CAPPED, not unconditional (fixed 2026-08-20). It
    // used to claim idx 0 whenever every candidate's site was already in
    // flight, which in a library dominated by one site meant piling the whole
    // pool onto that site. For sites whose Python side drives a PERSISTENT
    // BROWSER PROFILE that is the one thing that must never happen: Chromium
    // locks a user-data-dir, so the extra processes cannot get the good launch
    // and used to silently degrade to a headless binary that Cloudflare then
    // refuses to clear — which is why mangafire update checks failed while
    // ordinary downloads worked. Cross-file: sites/profile_lock.py's header has
    // the measurement, and sites/mangafire_vrf.py waits on that lock as the
    // Python-side backstop for anyone running the CLI directly.
    function siteLimit(site) {
      return _SERIAL_BROWSER_SITES.has(site) ? 1 : concurrency;
    }
    function claimNext() {
      if (remaining.length === 0) return null;
      // Prefer a site nobody is on, exactly as before.
      let idx = remaining.findIndex(
        (j) => !inFlightBySite.get(j.entry.seriesMeta?.site || "_"),
      );
      if (idx === -1) {
        // Otherwise take the first job whose site is still under its cap.
        idx = remaining.findIndex((j) => {
          const s = j.entry.seriesMeta?.site || "_";
          return (inFlightBySite.get(s) || 0) < siteLimit(s);
        });
      }
      if (idx === -1) return null; // everything left is capped — wait, don't pile on
      return remaining.splice(idx, 1)[0];
    }

    async function worker() {
      while (!ctrl.signal.aborted) {
        const job = claimNext();
        if (!job) {
          // Nothing claimable. Two different situations:
          //   queue empty            -> this worker is done
          //   everything left capped -> a peer is holding that site's only
          //                             slot; wait for it rather than piling
          //                             on (see claimNext) or exiting, which
          //                             would shrink the pool permanently and
          //                             could strand the remaining jobs.
          // Termination is guaranteed: claimNext only returns null with jobs
          // left when some site is at its cap, which means a peer is in flight
          // and will free a slot when it finishes.
          if (remaining.length === 0) return;
          await new Promise((r) => setTimeout(r, 150));
          continue;
        }
        const entry = job.entry;
        const site = entry.seriesMeta?.site || "_";
        inFlightBySite.set(site, (inFlightBySite.get(site) || 0) + 1);

        const queuedRow = _updateCheck.rowFor(runId, entry.folderPath);
        const base = {
          folderPath: entry.folderPath,
          title: entry.title,
          cover: entry.seriesMeta?.cover || null,
          site,
          enqueuedAt: queuedRow?.enqueuedAt || Date.now(),
          // The other folders this row speaks for. Present only on a forked
          // series, so an ordinary row is byte-identical to what it was.
          ...(job.peerFolders.length ? { memberFolders: job.peerFolders } : {}),
        };

        _updateCheck.emit(runId, {
          kind: "running",
          row: { ...base, state: "running" },
          completed,
          total: jobs.length,
        });

        let r;
        try {
          r = await _checkSeriesUpdates(entry.folderPath, {
            collapseSplits,
            signal: ctrl.signal,
            peerFolders: job.peerFolders,
          });
        } catch (err) {
          // _checkSeriesUpdates already wraps its errors into a shape; this
          // catch is a defensive net for anything that escapes (e.g. an
          // out-of-band reject from the inner Promise). Normalize the same
          // way the inner handler does so the renderer sees consistent
          // shapes regardless of failure mode.
          const msg = err?.message || String(err);
          r = msg === "aborted" || ctrl.signal.aborted
            ? { error: "aborted" }
            : { error: "check_failed", message: msg };
        } finally {
          inFlightBySite.set(site, (inFlightBySite.get(site) || 1) - 1);
        }

        completed += 1;
        results.push({ ...base, ...r });
        _updateCheck.emit(runId, {
          kind: "completed",
          row: updateCheckResultRow(base, r),
          // Fresh site metadata (status / authors / cover / genres) for the
          // renderer to splice into its library entries. Not part of the row
          // — it describes the SERIES, not the scan.
          updatedMeta: r.updatedMeta || null,
          completed,
          total: jobs.length,
        });
      }
    }

    try {
      await Promise.all(Array.from({ length: concurrency }, () => worker()));
    } finally {
      // In a finally so a worker that throws its way out still closes the
      // record — a run stuck at state:"running" would refuse every later
      // Check All by rule 2 for the rest of the session.
      const aborted = ctrl.signal.aborted;
      if (_checkAllAbortCtrl === ctrl) _checkAllAbortCtrl = null;
      _updateCheck.emit(runId, {
        kind: "done",
        completed,
        total: jobs.length,
        durationMs: Date.now() - startedAt,
        aborted,
      });
    }

    return { status: "done", runId, results, total: jobs.length, checked: results.length };
  });

  // ── Snapshot of the current / most recent Check All sweep ──
  // Lets a freshly-mounted renderer adopt a sweep it never saw start (the
  // Library tab unmounts on every tab switch; the sweep does not stop).
  // Returns null when no sweep has run this session.
  ipcMain.handle("get-update-check-state", async () => _updateCheck.snapshot());

  // ── Cancel an in-flight Check All sweep ──
  // Aborts the per-scan AbortController which propagates into every
  // _checkSeriesUpdates call via opts.signal. In-flight Python procs are
  // killed; queued series never start. The handler returns { ok: false }
  // when no sweep is running so the renderer can tell whether the cancel
  // was a no-op (e.g. the scan finished between click and IPC).
  ipcMain.handle("cancel-check-all-updates", async () => {
    if (!_checkAllAbortCtrl) return { ok: false };
    try { _checkAllAbortCtrl.abort(); } catch {}
    _checkAllAbortCtrl = null;
    return { ok: true };
  });

  // ── Cross out / restore individual chapters in the update-check ──
  // Writes .aio_series.json:chapters_ignored, the list _checkSeriesUpdates
  // above withholds from `newChapters` (and reports separately so the panel
  // can strike it through). The renderer calls this from the per-chapter "x"
  // and its undo; nothing else writes the key, and aio-dl.py only carries it
  // forward (grep merged_ignored).
  //
  // READ-MODIFY-WRITE LIVES HERE, NOT IN THE RENDERER, and that is the point
  // of having a dedicated IPC rather than reusing save-series-meta: the panel
  // holds a row, not the file, so two quick clicks on different chapters would
  // both send a whole list computed from the same stale copy and the second
  // would erase the first. Each call here re-reads the file.
  //
  // Batched (`chapters` is an array) so "cross out the rest" is one write.
  // Returns the new list for the renderer to mirror into its library entry.
  //
  // `kind` ("chapter" default, or "volume") picks the namespace: volume
  // cross-outs go to volumes.ignored and never touch chapters_ignored
  // (series-ledger.js applyIgnored). Returned as `ignoredItems` too;
  // `chaptersIgnored` keeps its old meaning for chapter calls.
  ipcMain.handle("set-chapters-ignored", async (_event, folderPath, chapters, ignored, kind = "chapter") => {
    try {
      const metaPath = path.join(folderPath, ".aio_series.json");
      let meta;
      try {
        meta = JSON.parse(await fs.promises.readFile(metaPath, "utf8"));
      } catch {
        // No metadata file means no update-check for this series either, so
        // there is nothing a cross-out could apply to. Refuse rather than
        // creating a stub — save-series-meta is the "adopt this folder" path.
        return { ok: false, error: "no_metadata" };
      }
      if (!meta || typeof meta !== "object") return { ok: false, error: "invalid_metadata" };

      if (kind !== "chapter" && kind !== "volume") return { ok: false, error: "bad_kind" };

      // Labels, blank-skipping and the "emptied → key removed" rule live in
      // series-ledger.js applyIgnored (same spelling as the folder merge and
      // aio-dl.py's merged_ignored).
      const { meta: next, ignored: list } = seriesLedger.applyIgnored(meta, kind, chapters, ignored);

      await fs.promises.writeFile(metaPath, JSON.stringify(next, null, 2), "utf8");
      return {
        ok: true,
        kind,
        ignoredItems: list,
        ...(kind === "chapter" ? { chaptersIgnored: list } : {}),
      };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // ── Merge duplicate series folders ──
  // Two-step by contract: the renderer calls with dryRun:true to render the
  // plan, then again with dryRun:false once the user confirms it. All the
  // rules and refusals live in mergeSeriesFolders (see its block header);
  // this wrapper only guarantees a shaped answer instead of a thrown IPC.
  ipcMain.handle("merge-series-folders", async (_event, opts = {}) => {
    try {
      const settings = history.getSettings();
      const { workingDir } = resolveSpawnPaths(settings);
      const r = await mergeSeriesFolders({
        ...(opts || {}),
        // Injected as DATA rather than read inside the module, so the offline
        // test can drive the whole thing without electron. The renderer never
        // supplies these — spreading opts first means it cannot override them
        // either, which is what keeps the containment guard honest.
        libraryRoot: getConfiguredOutputRoot(workingDir),
        runningDownloads: downloader?.getRunning?.() || [],
      });
      // The RESULT's dryRun: the module defaults to a dry run when the
      // caller omits the flag (series-merge.js).
      if (r?.ok && r.dryRun === false) notifySyncLibraryChanged("merge");
      return r;
    } catch (err) {
      return { ok: false, error: "merge_failed", message: err?.message || String(err) };
    }
  });

  // ── Save/update series metadata (manual URL entry for old downloads) ──
  // Used when a series was downloaded before the .aio_series.json feature.
  // The user pastes the URL in the UI, and this saves a minimal metadata
  // file so update-checking becomes possible.
  ipcMain.handle("save-series-meta", async (_event, folderPath, metaData) => {
    try {
      const metaPath = path.join(folderPath, ".aio_series.json");

      // Read existing if any (to preserve fields we don't want to overwrite)
      let existing = {};
      if (fs.existsSync(metaPath)) {
        try {
          existing = JSON.parse(fs.readFileSync(metaPath, "utf8"));
        } catch {}
      }

      // Read hid from canonical/legacy marker if available
      let hid = existing.hid || metaData.hid || null;
      if (!hid) hid = readHidMarker(folderPath);

      const merged = {
        ...existing,
        ...metaData,
        hid: hid || metaData.hid || existing.hid || null,
      };

      fs.writeFileSync(metaPath, JSON.stringify(merged, null, 2), "utf8");
      notifySyncLibraryChanged("series-meta");
      return { ok: true, meta: merged };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  // ── Get system theme ──
  ipcMain.handle("get-theme", async () => {
    return nativeTheme.shouldUseDarkColors ? "dark" : "light";
  });

  // ── Quit the app (used by setup window's close/quit buttons) ──
  ipcMain.handle("quit-app", async () => {
    app.quit();
  });

  // ── Reinstall Python environment ──
  // Deletes the downloaded Python env and restarts the app.
  // On restart, the app sees no .setup-complete marker and
  // automatically shows the setup wizard again.
  ipcMain.handle("reinstall-python", async () => {
    if (!IS_PACKAGED || !pythonEnvDir) {
      return { ok: false, error: "Only available in installed mode" };
    }

    // Device Sync: refuse during a job; stop find-sources' search tree and
    // wait for it (≤5 s), or rmSync would throw EBUSY on a dying python.exe.
    let syncJob = false;
    try {
      syncJob = !!deviceSync?.isJobRunning();
    } catch {}
    if (syncJob) return { ok: false, error: "A device sync job is running. Cancel it first." };
    try {
      await deviceSync?.cancelFindSources();
    } catch {}

    // Delete the entire Python environment
    deleteEnv(pythonEnvDir);

    // app.exit() skips before-quit/will-quit but still emits 'quit' —
    // which is where electron-updater's install-on-quit hook lives. A
    // pending downloaded update would silently install WHILE the
    // relaunched app starts (and the installer kills it). Suppress it;
    // the update applies on the next normal exit instead.
    appUpdater.suppressInstallOnQuit();

    // Restart the app — on next launch, setup will re-run
    app.relaunch();
    app.exit(0);
  });

  // ── App self-update (opt-in; electron/updater.js) ──
  // Colon-namespaced like search:/metadata: to keep visual distance from
  // the kebab-case manga "check-for-updates" family above. get-status
  // returns the full status snapshot (SettingsTab fetches it on mount,
  // then live-updates from the "app-update-status" push channel).
  ipcMain.handle("app-update:get-status", async () => {
    return appUpdater.getStatus();
  });

  ipcMain.handle("app-update:check-now", async () => {
    return appUpdater.checkNow();
  });

  // Same pre-quit cleanup window-all-closed does: kill running Python
  // children first so they can't outlive Electron and hold tmp_<hid>/
  // locks; the cancelled runs surface in the resume bar on relaunch.
  // Guard BEFORE cancelAll — a spurious invoke with no downloaded update
  // must not kill active downloads for nothing.
  ipcMain.handle("app-update:apply-now", async () => {
    if (appUpdater.getStatus().state !== "downloaded") {
      return { ok: false, reason: "No downloaded update to apply." };
    }
    // Set BEFORE cancelAll: that call bounds itself at 5s, so a stuck child
    // could still be in _processes when quitAndInstall closes the window, and
    // the close listener would answer a user-initiated update with a quit
    // prompt. Explicit here rather than relying on runningCount() hitting 0.
    quitConfirmed = true;
    try {
      await Promise.allSettled([downloader?.cancelAll(), deviceSync?.shutdown({ timeoutMs: 5000 })]);
    } catch (e) {
      // only a synchronous throw building the array lands here
      console.error("[apply-now] pre-quit cleanup", e);
    }
    return appUpdater.applyNow();
  });
}

// ============================================================
// APP LIFECYCLE
// ============================================================

// Set once the app is on its way out (window-all-closed or any quit path), so
// a second launch arriving during the ≤5 s shutdown wait can't open a window
// that the pending app.quit() would then tear down.
let appQuitting = false;
app.on("before-quit", () => {
  appQuitting = true;
});

// A second launch focuses this instance instead of starting another (see the
// gotSingleInstanceLock declaration). During first-run setup the only window
// is the setup wizard; once startup is done and no window is left (the user
// closed it but the quit is still pending) it re-creates the main window, so
// a second launch never just vanishes.
app.on("second-instance", () => {
  try {
    const win = (mainWindow && !mainWindow.isDestroyed() ? mainWindow : null) || BrowserWindow.getAllWindows()[0] || null;
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
      return;
    }
    if (app.isReady() && downloader && !appQuitting) createWindow();
  } catch (e) {
    console.error("[second-instance]", e);
  }
});

app.whenReady().then(async () => {
  // An app.quit() before ready only defers shutdown; ready still resolves.
  if (!gotSingleInstanceLock) return;

  if (process.platform === "darwin" && APP_ICON_PATH && app.dock) {
    app.dock.setIcon(APP_ICON_PATH);
  }

  // Compute all paths now that the app is ready
  computePaths();

  // ── Register custom protocol handler ──
  // Serves local files to the renderer via localfile:// URLs.
  // URL format: localfile:///C:/Users/legoc/manga/Title/file.pdf
  protocol.handle("localfile", (request) => {
    const url = new URL(request.url);
    let filePath = decodeURIComponent(url.pathname);
    // On Windows, URL pathname has an extra leading slash: /C:/path
    if (process.platform === "win32" && filePath.startsWith("/")) {
      filePath = filePath.substring(1);
    }
    return net.fetch(pathToFileURL(filePath).href);
  });

  // ── Intercept and inject Referer for Webtoon cover images ──
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: ["*://*.pstatic.net/*", "*://*.webtoons.com/*"] },
    (details, callback) => {
      details.requestHeaders["Referer"] = "https://www.webtoons.com/";
      callback({ cancel: false, requestHeaders: details.requestHeaders });
    }
  );

  // NOTE: Don't create defaultWorkingDir here — it gets created on-demand
  // when the first download starts (aio-dl.py creates manga/ inside it).
  // Creating it at startup would leave an empty "AIO Downloader" folder
  // in Documents even if setup hasn't finished or the user never downloads.

  // Initialize settings/history manager
  history = new HistoryManager(app.getPath("userData"));

  // Set up all IPC handlers (both setup and normal download handlers).
  // We do this before the setup window opens so it can send IPC messages.
  setupIPC();

  // ── FIRST-RUN SETUP ──
  // In packaged mode, check if the Python environment exists.
  // If not, show the setup wizard that downloads everything.
  if (IS_PACKAGED && !isSetupComplete(pythonEnvDir)) {
    await runSetupFlow();
  }

  // ── NORMAL STARTUP ──
  // Ensure the python-src path is in the ._pth file so Python can find
  // the 'sites' module. Runs every startup (not just first-run) because
  // the app install path can change on updates.
  ensurePythonSrcInPth();
  initDownloader();
  // Device Sync. Window-dependent deps read lazily: the window doesn't exist
  // yet. initDeviceSync throws on a missing/mistyped dep (logged; sync stays
  // off). getLibraryRoot is the merge handler's own expression.
  if (initDeviceSync) {
    try {
      deviceSync = initDeviceSync({
        ipcMain,
        send: sendToUI,
        userDataDir: path.join(app.getPath("userData"), "sync"),
        getSettings: () => history.getSettings(),
        getLibraryRoot: () => getConfiguredOutputRoot(resolveSpawnPaths(history.getSettings()).workingDir),
        getRunningDownloads: () => downloader?.getRunning?.() ?? [],
        isCheckAllRunning: () => _updateCheck.isRunning(),
        isSearchRunning: () => searcher?.isRunning?.() ?? false,
        resolveSpawnPaths,
        extraEnv: buildPythonEnv(),
        onFocus: (cb) => app.on("browser-window-focus", cb),
        isFocused: () => !!BrowserWindow.getFocusedWindow(),
        getWindow: () => mainWindow,
        showSaveDialog: (o) => dialog.showSaveDialog(mainWindow, o),
      });
    } catch (e) {
      console.error("[deviceSync] init failed", e);
    }
  }
  createWindow();

  // App self-update — armed unless the user opted OUT (Settings →
  // General → App Updates) AND this install supports it (packaged
  // Windows NSIS / Linux AppImage). First check runs ~30s post-launch;
  // the require("electron-updater") itself is deferred until then, so
  // startup cost is zero either way. See electron/updater.js. The stored
  // `false` that the old opt-in default wrote for every saving user is
  // cleared once by history.js's v1 settings migration — without that,
  // `!== false` would leave the whole existing install base off.
  const updaterSettings = history.getSettings();
  appUpdater.initAppUpdater({
    enabled: updaterSettings.appAutoUpdate !== false,
    delayDays: updaterSettings.appUpdateDelayDays,
    onStatus: (s) => sendToUI("app-update-status", s),
  });
});

app.on("window-all-closed", async () => {
  // Wait for in-flight downloads to actually die before quitting. Without
  // the await, fire-and-forget taskkill on Windows lets Python children
  // outlive Electron — if the user immediately relaunches, the orphan
  // children may still hold tmp_<hid>/ lockfiles (resume detection bug).
  // cancelAll() bounds itself at 5s so a stuck child can't trap quit.
  // Device Sync's shutdown (cancel the job, wait ≤5 s, flush) runs in
  // parallel; allSettled so neither can keep the other from quitting.
  appQuitting = true;
  try {
    await Promise.allSettled([downloader?.cancelAll(), deviceSync?.shutdown({ timeoutMs: 5000 })]);
  } catch (e) {
    // only a synchronous throw building the array lands here
    console.error("[window-all-closed] pre-quit cleanup", e);
  }
  app.quit();
});

// Every quit path ends here, including app.quit() from quit-app / the updater
// and app.exit() from reinstall-python, neither of which emits
// window-all-closed. shutdownNow is synchronous and idempotent.
app.on("quit", () => {
  try {
    deviceSync?.shutdownNow();
  } catch (e) {
    console.error("[deviceSync] shutdownNow", e);
  }
});
