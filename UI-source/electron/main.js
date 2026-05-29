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
const { HistoryManager } = require("./history");
const { PythonSetup, isSetupComplete, deleteEnv, PYTHON_VERSION } = require("./setup");
const { scanLibrary, generateMissingThumbnails, downloadMissingCovers, cleanupOrphanCovers, getChaptersOnDevice, getImageChaptersOnDevice } = require("./library");

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
  const pythonCmd = settings.pythonCmd || defaultPythonCmd;
  const workingDir = settings.workingDir || defaultWorkingDir;
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

  // Dev mode: load from Vite dev server (hot reload)
  // Prod mode: load built files from disk
  if (process.env.NODE_ENV === "development" || !IS_PACKAGED) {
    mainWindow.loadURL("http://localhost:5173").catch(() => {
      mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
    });
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }

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
  // Build the extra environment variables for the Python process.
  //
  // PLAYWRIGHT_BROWSERS_PATH (all platforms in packaged mode): points
  // patchright at the bundled Chromium that setup.js downloaded, so
  // VRF/Playwright handlers don't try to re-download into a system path.
  //
  // PYTHONPATH (packaged Unix only): puts resources/python-src/ on
  // sys.path so aio-dl.py can `import sites` and `import aio_search_cli`.
  // On Windows the embed Python ignores PYTHONPATH entirely and we use
  // ensurePythonSrcInPth() to write the same path into ._pth instead.
  const extraEnv = {};
  if (IS_PACKAGED && playwrightDir && fs.existsSync(playwrightDir)) {
    extraEnv.PLAYWRIGHT_BROWSERS_PATH = playwrightDir;
  }
  if (IS_PACKAGED && process.platform !== "win32" && pythonSrcDir) {
    extraEnv.PYTHONPATH = pythonSrcDir;
  }

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
    },
  });

  // Cross-site search subprocess invoker — separate from `downloader` because
  // search is a single blocking request/response (not a long-lived stream
  // with progress events) and lives on different IPC channels.
  //
  // Searcher gets the SAME extraEnv as Downloader (PLAYWRIGHT_BROWSERS_PATH
  // in packaged mode). Without this, search would still run but Playwright-
  // using handlers (mangafire, violetscans, rizzfables, mangathemesia with
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
    return {
      ...saved,
      // Genuine user-pref defaults — these have meaningful "missing →
      // assume default" semantics and stay merged here. SettingsTab.jsx
      // owns the DEFAULT_DOWNLOAD_DEFAULTS dict; we just pass through
      // whatever the user has actually saved (or an empty object so
      // SettingsTab can hydrate from its own defaults on first load).
      defaults: saved.defaults || {},
      verboseAlways: saved.verboseAlways !== false,
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
    return { ok: true };
  });

  // ── Start a download ──
  ipcMain.handle("start-download", async (_event, { url, args }) => {
    const settings = history.getSettings();
    const pythonCmd = settings.pythonCmd || defaultPythonCmd;
    const scriptPath = settings.scriptPath || defaultScriptPath;
    const workingDir = settings.workingDir || defaultWorkingDir;

    // Create the working directory on-demand (not at startup, so we don't
    // leave an empty "AIO Downloader" folder if the user never downloads)
    try { fs.mkdirSync(workingDir, { recursive: true }); } catch {}

    const downloadId = downloader.start({
      pythonCmd,
      scriptPath,
      workingDir,
      url,
      args,
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
  ipcMain.handle("search:run", async (_event, { query, opts }) => {
    const settings = history.getSettings();
    const pythonCmd = settings.pythonCmd || defaultPythonCmd;
    const scriptPath = settings.scriptPath || defaultScriptPath;
    const workingDir = settings.workingDir || defaultWorkingDir;
    try { fs.mkdirSync(workingDir, { recursive: true }); } catch {}

    try {
      const result = await searcher.runSearch({
        pythonCmd,
        scriptPath,
        workingDir,
        query,
        opts,
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
    const pythonCmd = settings.pythonCmd || defaultPythonCmd;
    const scriptPath = settings.scriptPath || defaultScriptPath;
    const workingDir = settings.workingDir || defaultWorkingDir;

    const downloadId = downloader.resume({
      pythonCmd,
      scriptPath,
      workingDir,
      url,
      tmpDir,
      format,
      epubLayout,
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
    const workingDir = settings.workingDir || defaultWorkingDir;
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
    return runMetadataCli(
      ["update", filePath, ...(coverPath ? ["--cover-path", coverPath] : [])],
      JSON.stringify(data || {})
    );
  });

  // ── Scan library (read all downloaded manga) ──
  // Returns the list immediately. Missing thumbnails are generated
  // in the background using mupdf (WASM) in the main process.
  // As each thumbnail completes, a 'library-thumb-ready' event is
  // sent to the renderer so it can update that card's cover image.
  ipcMain.handle("scan-library", async () => {
    const settings = history.getSettings();
    const workingDir = settings.workingDir || defaultWorkingDir;
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

    const settings = history.getSettings();
    const pythonCmd = settings.pythonCmd || defaultPythonCmd;
    const workingDir = settings.workingDir || defaultWorkingDir;
    const scriptPath = settings.scriptPath || defaultScriptPath;

    if (!fs.existsSync(scriptPath)) {
      return { error: "no_script", message: "aio-dl.py not found at " + scriptPath };
    }

    try {
      const result = await new Promise((resolve, reject) => {
        const args = [
          "-u", scriptPath,
          "--list-chapters",
          "--verbose",
        ];
        if (meta.language && meta.language !== "en") {
          args.push("--language", meta.language);
        }
        if (meta.site) {
          args.push("--site", meta.site);
        }
        // Collapse-splits forwarding: when the user has the global
        // collapseSplits setting on, --list-chapters now applies the same
        // group_chapters_for_download filter the download path uses
        // (aio-dl.py lines around `if collapse_splits_enabled:` in the
        // --list-chapters block). Without forwarding, the emitted list
        // would include fragment-shape decimals that would never download
        // under collapse, causing the diff against meta.chapters_downloaded
        // to flag them as new forever. Cross-file: settings.collapseSplits
        // is the source of truth (SettingsTab.jsx default true).
        if (opts.collapseSplits) {
          args.push("--collapse-splits");
        }
        args.push(meta.url);

        // Build env — Playwright path for packaged mode, plus PYTHONPATH
        // on Unix (mirrors initDownloader; needed because the bundled
        // python-src lives under resources/ and won't be on sys.path
        // otherwise on standard CPython). Windows uses ._pth instead.
        const extraEnv = { PYTHONUNBUFFERED: "1" };
        if (IS_PACKAGED && playwrightDir) {
          extraEnv.PLAYWRIGHT_BROWSERS_PATH = playwrightDir;
        }
        if (IS_PACKAGED && process.platform !== "win32" && pythonSrcDir) {
          extraEnv.PYTHONPATH = pythonSrcDir;
        }

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

      // ── Determine which chapters are "on device" ──
      // Two modes controlled by settings.useFileBasedChapterCheck:
      //   false (default): trust .aio_series.json's chapters_downloaded list
      //   true:  scan actual files on disk, extract chapter numbers from
      //          filenames (reversing the ~ → . convention for partials)
      const useFileBased = !!settings.useFileBasedChapterCheck;
      let downloadedChapters;
      let checkMode;

      if (useFileBased) {
        // Scan the folder for actual files and extract chapter numbers.
        // For individual files like "Title Ch 5~5.pdf" → chapter "5.5"
        // For combined range files like "Title Ch 1-50.pdf" → all site
        // chapters in [1, 50] are considered present on device.
        const OUTPUT_EXTS = new Set(["pdf", "epub", "cbz"]);
        let diskFiles = [];
        try {
          const contents = await fs.promises.readdir(folderPath, { withFileTypes: true });
          diskFiles = contents
            .filter((f) => f.isFile() && !f.name.startsWith("."))
            .filter((f) => {
              const ext = path.extname(f.name).toLowerCase().slice(1);
              return OUTPUT_EXTS.has(ext);
            })
            .map((f) => ({ name: f.name }));
        } catch {}

        downloadedChapters = getChaptersOnDevice(
          diskFiles,
          (result.chapters || []).map(String)
        );
        // --format none (image-only) series have no archive files for
        // getChaptersOnDevice to read, so the diff above would mark every
        // chapter "new". Recover the on-device set from the
        // images/Chapter_<n>/ tree and union it in. (JSON mode below is
        // unaffected — it reads chapters_downloaded, which aio-dl.py writes
        // for every format including none.) grep getImageChaptersOnDevice.
        if (diskFiles.length === 0) {
          for (const c of getImageChaptersOnDevice(folderPath)) {
            downloadedChapters.add(c);
          }
        }
        checkMode = "files";
      } else {
        // Use the JSON metadata list (what aio-dl.py recorded as downloaded)
        downloadedChapters = new Set((meta.chapters_downloaded || []).map(String));
        checkMode = "json";
      }

      // Compare: site chapters minus on-device chapters = missing/new
      const siteChapters = new Set((result.chapters || []).map(String));
      const newChapters = [...siteChapters]
        .filter((ch) => !downloadedChapters.has(ch))
        .sort((a, b) => parseFloat(a) - parseFloat(b));

      return {
        ok: true,
        newChapters,
        total: result.total || siteChapters.size,
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

  // ── Check for new chapters for a single series ──
  ipcMain.handle("check-for-updates", async (_event, folderPath) => {
    // Single-series Check uses the global collapseSplits setting so the
    // result matches what a "Download Missing Chapters" click would produce.
    // No abort signal here — single check completes fast and can't be
    // user-cancelled from the DetailView (just close the panel).
    const settings = history.getSettings();
    const collapseSplits = settings.collapseSplits === true;
    return _checkSeriesUpdates(folderPath, { collapseSplits });
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
  // Cancelable: the renderer can fire cancel-check-all-updates to stop the
  // sweep mid-flight; the in-flight Python procs are killed via the
  // per-call AbortSignal threaded into _checkSeriesUpdates. Tracked via the
  // module-scoped _checkAllAbortCtrl (declared above this handler).
  //
  // Progress events: emits one "queued" per series upfront (renderer pre-
  // renders rows), then "running" / "completed" per worker hop, then a
  // final "done" event carrying total duration. The old single-counter
  // event shape ({ current, total, title }) is gone — the new richer
  // events are dispatched by `kind` in the renderer.
  ipcMain.handle("check-all-updates", async () => {
    const settings = history.getSettings();
    const workingDir = settings.workingDir || defaultWorkingDir;
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

    if (checkable.length === 0) {
      sendToUI("update-check-progress", {
        kind: "done", completed: 0, total: 0, durationMs: 0, aborted: false,
      });
      return { results: [], total: 0, checked: 0 };
    }

    const concurrency = Math.max(
      1,
      Math.min(8, Number(settings.checkAllConcurrency) || 4),
    );
    const collapseSplits = settings.collapseSplits === true;

    // Fresh AbortController per scan. Replace any stale one (defensive: the
    // previous scan's "done" event should have nulled it, but if cancel
    // fired late or the renderer restarted, we don't want stale refs).
    if (_checkAllAbortCtrl) {
      try { _checkAllAbortCtrl.abort(); } catch {}
    }
    _checkAllAbortCtrl = new AbortController();
    const ctrl = _checkAllAbortCtrl;

    // Pre-emit a "queued" event for every series so the renderer can render
    // the full row list immediately with placeholders. Total carried on
    // every event so the renderer doesn't need a separate "init" message.
    for (const e of checkable) {
      sendToUI("update-check-progress", {
        kind: "queued",
        folderPath: e.folderPath,
        title: e.title,
        cover: e.seriesMeta?.cover || null,
        site: e.seriesMeta?.site || null,
        total: checkable.length,
      });
    }

    const remaining = [...checkable];
    const inFlightBySite = new Map(); // site → count of workers currently checking
    const results = [];
    let completed = 0;
    const startedAt = Date.now();

    // Provider-aware claim. findIndex+splice runs synchronously between
    // awaits so two workers can never claim the same job — the JS event
    // loop only re-enters on the next await. When every site has an
    // in-flight worker (concurrency ≥ unique-sites), the findIndex misses
    // and we fall through to FIFO claim (idx 0) so progress doesn't stall.
    function claimNext() {
      if (remaining.length === 0) return null;
      let idx = remaining.findIndex(
        (e) => !inFlightBySite.get(e.seriesMeta?.site || "_"),
      );
      if (idx === -1) idx = 0;
      return remaining.splice(idx, 1)[0];
    }

    async function worker() {
      while (!ctrl.signal.aborted) {
        const entry = claimNext();
        if (!entry) return;
        const site = entry.seriesMeta?.site || "_";
        inFlightBySite.set(site, (inFlightBySite.get(site) || 0) + 1);

        sendToUI("update-check-progress", {
          kind: "running",
          folderPath: entry.folderPath,
          title: entry.title,
          site,
          completed,
          total: checkable.length,
        });

        let r;
        try {
          r = await _checkSeriesUpdates(entry.folderPath, {
            collapseSplits,
            signal: ctrl.signal,
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
        const merged = {
          folderPath: entry.folderPath,
          title: entry.title,
          cover: entry.seriesMeta?.cover || null,
          site,
          ...r,
        };
        results.push(merged);
        sendToUI("update-check-progress", {
          kind: "completed",
          folderPath: entry.folderPath,
          title: entry.title,
          result: merged,
          completed,
          total: checkable.length,
        });
      }
    }

    await Promise.all(Array.from({ length: concurrency }, () => worker()));

    const aborted = ctrl.signal.aborted;
    if (_checkAllAbortCtrl === ctrl) _checkAllAbortCtrl = null;
    sendToUI("update-check-progress", {
      kind: "done",
      completed,
      total: checkable.length,
      durationMs: Date.now() - startedAt,
      aborted,
    });

    return { results, total: checkable.length, checked: results.length };
  });

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

    // Delete the entire Python environment
    deleteEnv(pythonEnvDir);

    // Restart the app — on next launch, setup will re-run
    app.relaunch();
    app.exit(0);
  });
}

// ============================================================
// APP LIFECYCLE
// ============================================================

app.whenReady().then(async () => {
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
  createWindow();
});

app.on("window-all-closed", async () => {
  // Wait for in-flight downloads to actually die before quitting. Without
  // the await, fire-and-forget taskkill on Windows lets Python children
  // outlive Electron — if the user immediately relaunches, the orphan
  // children may still hold tmp_<hid>/ lockfiles (resume detection bug).
  // cancelAll() bounds itself at 5s so a stuck child can't trap quit.
  if (downloader) await downloader.cancelAll();
  app.quit();
});
