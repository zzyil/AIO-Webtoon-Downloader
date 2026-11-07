// ============================================================
// PROC-KILL — kill a child process and everything it spawned
//
// Owns: the one tree-kill spelling shared by searcher.js and
//   sync/find-sources.js (through Searcher). downloader.js keeps its inline
//   copy (downloader.js cancel(), grep taskkill) because that file is
//   in-flight; switch it here at ship time (plan open decision 3).
// Read by: searcher.js (cancel / cancelAndWait), sync/service.js
//   (shutdownNow → killTreeNow through Searcher.cancelNow).
// Depends on: child_process only. No electron.
//
// Why a tree kill: Python search/download children start Playwright, which
// starts Chromium. ChildProcess.kill() on Windows is TerminateProcess on the
// python.exe alone, so the grandchildren survive, keep the browser profile
// locked and hold files open (setup.js deleteEnv's rmSync then fails EBUSY).
//   win32:  `taskkill /pid N /t /f` (exit code 1, signal null on the child).
//   others: the child must be spawned `detached: true` so it leads its own
//           process group; process.kill(-pid) then reaches the whole group.
//           A child that isn't a group leader falls back to child.kill().
//
// killTreeNow (shutdownNow, app.on("quit")) is SYNCHRONOUS on win32: it
// waits for taskkill (≤ NOW_TIMEOUT_MS). libuv puts every non-detached child
// in a kill-on-close job object with SILENT_BREAKAWAY_OK, so when the app
// exits the job kills the python at once while its own children (Playwright,
// Chromium) are outside the job and live on. A detached taskkill fired just
// before the exit was tried and killed: it lost that race 3/3 on Windows 11
// (the python is gone before taskkill lists its children), orphaning the
// grandchild exactly as no kill at all would (P5, deviation 15).
// ============================================================

const { spawn, spawnSync } = require("child_process");

// killTreeNow's cap on the synchronous taskkill (measured ~150-350 ms).
const NOW_TIMEOUT_MS = 2000;

// child → Promise that resolves on its "close" event. Filled by trackChild()
// at spawn time, so killTree can tell "already closed" from "exited, close
// not delivered yet" without reading ChildProcess internals.
const _closed = new WeakMap();

/**
 * Register a freshly spawned child so killTree can await its close even if
 * the close event fires before killTree is called. Idempotent.
 * @returns {Promise<void>} resolves on the child's "close" (or "error" before spawn).
 */
function trackChild(child) {
  if (!child) return Promise.resolve();
  let p = _closed.get(child);
  if (!p) {
    p = new Promise((resolve) => {
      child.once("close", () => resolve());
      // A spawn failure emits "error" and may never emit "close".
      child.once("error", () => {
        if (child.pid == null) resolve();
      });
    });
    _closed.set(child, p);
  }
  return p;
}

function _exited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

// Fire the platform kill (async on win32: killTree awaits the child's close).
function _signalTree(child, signal) {
  if (child.pid == null) return;
  if (process.platform === "win32") {
    try {
      const tk = spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], {
        windowsHide: true,
        stdio: "ignore",
      });
      tk.on("error", () => {});
    } catch {
      /* taskkill missing: nothing better to try */
    }
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch {
    try {
      child.kill(signal);
    } catch {
      /* already gone */
    }
  }
}

/**
 * Kill `child` and its descendants.
 *
 * Contract: resolves (never rejects) with {closed:true} once the child's
 * "close" fired, or {closed:false} when it hadn't after `timeoutMs` plus a
 * SIGKILL grace second. A child that already exited is not signalled again
 * (its PID may be recycled), only awaited.
 *
 * @param {import('child_process').ChildProcess} child
 * @param {{timeoutMs?: number}} [opts]
 */
function killTree(child, { timeoutMs = 5000 } = {}) {
  if (!child) return Promise.resolve({ closed: true });
  const tracked = _closed.has(child);
  const closeP = trackChild(child);
  // Untracked and already exited: we can't know whether close already fired,
  // so don't wait on it.
  if (!tracked && _exited(child)) return Promise.resolve({ closed: true });
  if (!_exited(child)) _signalTree(child, "SIGTERM");

  return new Promise((resolve) => {
    let done = false;
    const finish = (closed) => {
      if (done) return;
      done = true;
      clearTimeout(t1);
      clearTimeout(t2);
      resolve({ closed });
    };
    let t2 = null;
    const t1 = setTimeout(() => {
      if (process.platform !== "win32" && !_exited(child)) _signalTree(child, "SIGKILL");
      t2 = setTimeout(() => finish(false), 1000);
    }, timeoutMs);
    closeP.then(() => finish(true));
  });
}

/**
 * Synchronous kill for shutdownNow(): the tree is dead (or taskkill timed
 * out) when this returns, so an app exit right after can't orphan it (see
 * the header). win32 waits for `taskkill /t /f` up to NOW_TIMEOUT_MS;
 * elsewhere SIGKILLs the group (already synchronous).
 * Contract: never throws; returns true when a kill was sent.
 */
function killTreeNow(child) {
  try {
    if (!child || child.pid == null || _exited(child)) return false;
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true, stdio: "ignore", timeout: NOW_TIMEOUT_MS });
      return true;
    }
    _signalTree(child, "SIGKILL");
    return true;
  } catch {
    return false;
  }
}

module.exports = { killTree, killTreeNow, trackChild, NOW_TIMEOUT_MS };
