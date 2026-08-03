// ============================================================
// PRELOAD SCRIPT
//
// This file runs in a special "in-between" context. It has
// access to some Node.js features, but it's also connected
// to the browser window where React runs.
//
// We use contextBridge to safely expose specific functions
// to the React code. React can then call them like:
//   window.electronAPI.startDownload({ url, args })
//
// This is much safer than giving React full Node.js access.
// ============================================================

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  // ── Download controls ──
  startDownload: (opts) => ipcRenderer.invoke("start-download", opts),
  cancelDownload: (id) => ipcRenderer.invoke("cancel-download", id),
  resumeDownload: (opts) => ipcRenderer.invoke("resume-download", opts),
  deleteTemp: (dir) => ipcRenderer.invoke("delete-temp", dir),

  // ── Data retrieval ──
  scanResumable: () => ipcRenderer.invoke("scan-resumable"),
  getHistory: () => ipcRenderer.invoke("get-history"),
  getSettings: () => ipcRenderer.invoke("get-settings"),
  saveSettings: (s) => ipcRenderer.invoke("save-settings", s),
  // Display-only read of the currently-resolved python / script / workingDir
  // paths. Renderer uses these for placeholder hints in SettingsTab so the
  // user can see what's auto-resolved without the values being persisted
  // back via saveSettings. See main.js's get-resolved-paths handler for
  // the round-trip-bug rationale.
  getResolvedPaths: () => ipcRenderer.invoke("get-resolved-paths"),
  getTheme: () => ipcRenderer.invoke("get-theme"),

  // ── Download queue persistence ──
  // The queue lives in React state and dies with the renderer, so it's
  // mirrored to download_queue.json (electron/history.js owns the file +
  // shape). useDownloader.js reads this once on mount and writes from a
  // debounced effect — grep queueHydratedRef there.
  getQueueSnapshot: () => ipcRenderer.invoke("queue:get"),
  saveQueueSnapshot: (snap) => ipcRenderer.invoke("queue:save", snap),

  // ── OS dialogs ──
  openFolder: (p) => ipcRenderer.invoke("open-folder", p),
  pickFolder: () => ipcRenderer.invoke("pick-folder"),
  pickFile: (filters) => ipcRenderer.invoke("pick-file", filters),
  readMetadata: (filePath) => ipcRenderer.invoke("metadata:read", filePath),
  updateMetadata: (filePath, data, coverPath) => ipcRenderer.invoke("metadata:update", filePath, data, coverPath),

  // ── Event listeners ──
  // React calls these to subscribe to live updates from the main process.
  // They return an "unsubscribe" function that React calls in useEffect cleanup.
  onDownloadLog: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("download-log", handler);
    // Return a function that removes this listener (for React cleanup)
    return () => ipcRenderer.removeListener("download-log", handler);
  },

  onDownloadProgress: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("download-progress", handler);
    return () => ipcRenderer.removeListener("download-progress", handler);
  },

  onDownloadComplete: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("download-complete", handler);
    return () => ipcRenderer.removeListener("download-complete", handler);
  },

  onThemeChanged: (callback) => {
    const handler = (_event, theme) => callback(theme);
    ipcRenderer.on("theme-changed", handler);
    return () => ipcRenderer.removeListener("theme-changed", handler);
  },

  // ── Quit confirmation ──
  // main.js preventDefaults the window close while a download is actually
  // RUNNING and pushes "confirm-quit" with { running: [{downloadId, title,
  // url, startedAt}] }; ConfirmQuitDialog.jsx answers with exactly one of
  // confirmQuit (close for real) / cancelQuit (stay, and cancel main's 15s
  // safety valve).
  onConfirmQuit: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("confirm-quit", handler);
    return () => ipcRenderer.removeListener("confirm-quit", handler);
  },
  confirmQuit: () => ipcRenderer.invoke("quit:confirm"),
  cancelQuit: () => ipcRenderer.invoke("quit:cancel"),

  // ── Cross-site search ──
  // runSearch resolves with the parsed JSON result (candidate list +
  // optional winner_chapter_map). UI shows live progress via the log
  // feed (onSearchLog) while the search runs (~40-100s typical).
  runSearch: (query, opts) => ipcRenderer.invoke("search:run", { query, opts }),
  cancelSearch: () => ipcRenderer.invoke("search:cancel"),
  onSearchLog: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("search-log", handler);
    return () => ipcRenderer.removeListener("search-log", handler);
  },

  // ── comix.to sign-in ──
  // Runs `aio-dl.py --comix-login`, which opens comix.to in the downloader's own
  // persistent browser profile and waits while the user signs in. Credentials
  // are typed into the real site in a real window — nothing is read, stored or
  // forwarded by us. Resolves {ok, reason}. Cross-file: main.js "comix:login",
  // aio-dl.py --comix-login, sites/comix.py:open_login_window.
  comixLogin: () => ipcRenderer.invoke("comix:login"),

  // ── Library (manga browser) ──
  scanLibrary: () => ipcRenderer.invoke("scan-library"),
  openFile: (path) => ipcRenderer.invoke("open-file", path),
  deleteSeries: (folderPath) => ipcRenderer.invoke("delete-series", folderPath),

  // Listen for thumbnails generated in the background by the main process.
  // After scanLibrary returns, the main process uses mupdf to render
  // missing thumbnails one at a time. This event fires for each completion.
  onThumbnailReady: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("library-thumb-ready", handler);
    return () => ipcRenderer.removeListener("library-thumb-ready", handler);
  },

  // ── Library update checking ──
  // Check a single series for new chapters (spawns Python --list-chapters).
  // Now honors settings.collapseSplits so the diff matches the download path.
  checkForUpdates: (folderPath) => ipcRenderer.invoke("check-for-updates", folderPath),
  // Check ALL ongoing series for new chapters. Runs in a bounded parallel
  // worker pool (default 4 slots, settings.checkAllConcurrency-overridable)
  // with provider-aware scheduling — same-site jobs don't pile onto the
  // same CDN unless every other site is also in-flight. Emits per-series
  // progress via onUpdateCheckProgress (events tagged by `kind`).
  checkAllUpdates: () => ipcRenderer.invoke("check-all-updates"),
  // Abort an in-flight Check All sweep. Kills any running Python procs and
  // prevents queued series from starting. Returns { ok: true } when a scan
  // was active, { ok: false } when no-op (e.g. raced with completion).
  cancelCheckAllUpdates: () => ipcRenderer.invoke("cancel-check-all-updates"),
  // Save or update .aio_series.json (manual URL entry for old downloads)
  saveSeriesMeta: (folderPath, metaData) => ipcRenderer.invoke("save-series-meta", folderPath, metaData),
  // Progress events during check-all-updates. Payload shape varies by
  // `kind`:
  //   { kind: "queued",    folderPath, title, cover, site, total }
  //   { kind: "running",   folderPath, title, site, completed, total }
  //   { kind: "completed", folderPath, title, result, completed, total }
  //   { kind: "done",      completed, total, durationMs, aborted }
  // The renderer dispatches on `kind` — see UpdatesCenter.jsx.
  onUpdateCheckProgress: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("update-check-progress", handler);
    return () => ipcRenderer.removeListener("update-check-progress", handler);
  },

  // ── App self-update (opt-in) ──
  // NOT the manga-chapter checkForUpdates family above — these drive the
  // app's own silent update flow (electron/updater.js): background check,
  // maturity-delay gate, silent download, install on exit. Status object
  // shape lives on updater.js's `status` (state: unsupported|disabled|
  // idle|checking|deferred|downloading|downloaded|up-to-date|no-feed|
  // error + currentVersion/eligibleAt etc.).
  // SettingsTab fetches a snapshot on mount, then subscribes for pushes.
  getAppUpdateStatus: () => ipcRenderer.invoke("app-update:get-status"),
  checkAppUpdateNow: () => ipcRenderer.invoke("app-update:check-now"),
  applyAppUpdateNow: () => ipcRenderer.invoke("app-update:apply-now"),
  onAppUpdateStatus: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("app-update-status", handler);
    return () => ipcRenderer.removeListener("app-update-status", handler);
  },

  // ── Setup (first-run Python environment installation) ──
  // These are used by setup.html during the first-launch wizard,
  // and by the "Reinstall Python" button in SettingsTab.
  retrySetup: () => ipcRenderer.invoke("retry-setup"),
  reinstallPython: () => ipcRenderer.invoke("reinstall-python"),
  quitApp: () => ipcRenderer.invoke("quit-app"),

  // Listen for setup progress events from the main process
  onSetupStep: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("setup-step", handler);
    return () => ipcRenderer.removeListener("setup-step", handler);
  },
  onSetupLog: (callback) => {
    const handler = (_event, line) => callback(line);
    ipcRenderer.on("setup-log", handler);
    return () => ipcRenderer.removeListener("setup-log", handler);
  },
  onSetupProgress: (callback) => {
    const handler = (_event, pct) => callback(pct);
    ipcRenderer.on("setup-progress", handler);
    return () => ipcRenderer.removeListener("setup-progress", handler);
  },
  onSetupComplete: (callback) => {
    const handler = () => callback();
    ipcRenderer.on("setup-complete", handler);
    return () => ipcRenderer.removeListener("setup-complete", handler);
  },
  onSetupError: (callback) => {
    const handler = (_event, msg) => callback(msg);
    ipcRenderer.on("setup-error", handler);
    return () => ipcRenderer.removeListener("setup-error", handler);
  },
});
