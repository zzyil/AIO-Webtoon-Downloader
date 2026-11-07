// ============================================================
// DEVICE SYNC — ADB BINARY LOCATION AND SERVER START
//
// Owns the only two things adb.exe still does for Device Sync (non-UI plan,
// "When adb.exe still runs"):
//   * `adb version`, to list and label candidate binaries;
//   * `adb start-server`, when nothing answers on the server port.
// It never runs `kill-server`: the server is shared with every other adb
// tool on the PC, and no adb CLIENT runs here (adb/client.js speaks the
// protocol), so there is no client/server version fight to settle.
//
// Candidates, in order: the configured path (sync-targets.json adbPath),
// PATH, ANDROID_HOME and ANDROID_SDK_ROOT (each + platform-tools),
// %LOCALAPPDATA%\Android\Sdk\platform-tools on Windows. Deduped
// case-insensitively on Windows.
//
// Stateless: service.js caches the result and decides WHEN a server start is
// allowed (sync enabled, plus an explicit action or an adb target for the
// monitor). Everything that touches the OS is injectable (`execFile`,
// `exists`, `platform`, `env`), so tools/_test_device_sync_adb.js runs the
// Windows cases on Linux.
//
// Read by: service.js (sync:locate-adb, server start before adb work).
// Depends on: adb/client.js (AdbClient.version for ensureServer).
// ============================================================

const path = require("path");
const fs = require("fs");
const childProcess = require("child_process");

const VERSION_TIMEOUT_MS = 5000;
// adb start-server waits for the forked server's "OK" ack; its own internal
// wait is 15 s for the output threads, so give it a little more.
const START_TIMEOUT_MS = 20000;

function _pathMod(platform) {
  return platform === "win32" ? path.win32 : path.posix;
}

function _exeName(platform) {
  return platform === "win32" ? "adb.exe" : "adb";
}

function _key(p, platform) {
  const m = _pathMod(platform);
  const n = m.normalize(p);
  return platform === "win32" ? n.toLowerCase() : n;
}

/**
 * Ordered, deduped candidate paths (they may not exist).
 * @returns {{path:string, source:'configured'|'PATH'|'ANDROID_HOME'|'ANDROID_SDK_ROOT'|'LOCALAPPDATA'}[]}
 */
function candidatePaths({ configured, env = process.env, platform = process.platform } = {}) {
  const m = _pathMod(platform);
  const exe = _exeName(platform);
  const out = [];
  const seen = new Set();
  const add = (p, source) => {
    if (!p) return;
    const k = _key(p, platform);
    if (seen.has(k)) return;
    seen.add(k);
    out.push({ path: m.normalize(p), source });
  };
  if (configured) add(String(configured), "configured");
  const pathVar = env.PATH || env.Path || env.path || "";
  for (let dir of pathVar.split(platform === "win32" ? ";" : ":")) {
    // Windows PATH entries may be quoted ("C:\Program Files\…").
    dir = dir.trim().replace(/^"(.*)"$/, "$1");
    if (dir) add(m.join(dir, exe), "PATH");
  }
  for (const v of ["ANDROID_HOME", "ANDROID_SDK_ROOT"]) {
    if (env[v]) add(m.join(env[v], "platform-tools", exe), v);
  }
  if (platform === "win32" && env.LOCALAPPDATA) {
    add(m.join(env.LOCALAPPDATA, "Android", "Sdk", "platform-tools", exe), "LOCALAPPDATA");
  }
  return out;
}

/**
 * `adb version` stdout → {version, build, installedAs} (adb.cpp adb_version:
 * "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\nInstalled as
 * …"; ddmlib parses the same lines, so the format is stable). null when the
 * first line isn't there.
 */
function parseAdbVersion(stdout) {
  const s = String(stdout || "");
  const v = /Android Debug Bridge version (\d+\.\d+\.\d+)/.exec(s);
  if (!v) return null;
  const b = /^Version (\S+)/m.exec(s);
  const i = /^Installed as (.+?)\r?$/m.exec(s);
  return { version: v[1], build: b ? b[1] : null, installedAs: i ? i[1] : null };
}

function _run(execFile, bin, args, opts) {
  return new Promise((resolve) => {
    execFile(bin, args, opts, (err, stdout, stderr) => {
      resolve({ err, stdout: String(stdout || ""), stderr: String(stderr || "") });
    });
  });
}

/** Run `adb version`. {ok:true, version, build, installedAs} or {ok:false, error}. */
async function probeVersion(bin, { execFile = childProcess.execFile, timeoutMs = VERSION_TIMEOUT_MS } = {}) {
  const r = await _run(execFile, bin, ["version"], { windowsHide: true, timeout: timeoutMs });
  const parsed = parseAdbVersion(r.stdout);
  if (r.err || !parsed) {
    const why = r.err ? r.err.code || r.err.message : "unrecognized `adb version` output";
    return { ok: false, error: String(why) };
  }
  return { ok: true, ...parsed };
}

/**
 * Every candidate that exists, each with its `adb version`. A configured path
 * is always reported (ok:false, 'not found' when missing) so the UI can say
 * why it isn't used. `resolved` is the configured binary when it works, else
 * the first working candidate, else null.
 * @returns {Promise<{candidates:{path, source, ok, version?, build?, error?}[], resolved:string|null}>}
 */
async function locateAdb({ configured, env = process.env, platform = process.platform, execFile = childProcess.execFile, exists = _isFile } = {}) {
  const candidates = [];
  for (const c of candidatePaths({ configured, env, platform })) {
    if (!(await exists(c.path))) {
      if (c.source === "configured") candidates.push({ ...c, ok: false, error: "not found" });
      continue;
    }
    const v = await probeVersion(c.path, { execFile });
    candidates.push(v.ok ? { ...c, ok: true, version: v.version, build: v.build } : { ...c, ok: false, error: v.error });
  }
  const working = candidates.filter((c) => c.ok);
  const conf = working.find((c) => c.source === "configured");
  return { candidates, resolved: (conf || working[0] || {}).path || null };
}

async function _isFile(p) {
  try {
    return (await fs.promises.stat(p)).isFile();
  } catch (_) {
    return false;
  }
}

/**
 * `adb start-server`. cwd is the binary's own folder, NOT the app's: the
 * server it forks inherits the starting directory (adb.cpp launch_server,
 * CreateProcessW "use parent's starting directory") and outlives the app, so
 * starting it from the install folder would keep that folder open against
 * an update or uninstall. execFile doesn't hang on the long-lived server:
 * launch_server makes the caller's std handles non-inheritable first.
 * @returns {Promise<{ok:boolean, code:number|null, stdout:string, stderr:string, error?:string}>}
 */
async function startServer(bin, { execFile = childProcess.execFile, timeoutMs = START_TIMEOUT_MS, platform = process.platform } = {}) {
  const cwd = _pathMod(platform).dirname(bin);
  const r = await _run(execFile, bin, ["start-server"], { windowsHide: true, timeout: timeoutMs, cwd });
  if (r.err) {
    return { ok: false, code: typeof r.err.code === "number" ? r.err.code : null, stdout: r.stdout, stderr: r.stderr, error: String(r.err.code || r.err.message) };
  }
  return { ok: true, code: 0, stdout: r.stdout, stderr: r.stderr };
}

/**
 * Make sure a server answers. Asks `host:version` first and starts the
 * server ONLY when that fails with server-unavailable; any other error is
 * returned as is. Never kills a server, even one of another version.
 * @param {import('./client').AdbClient} client
 * @param {string|null} bin  resolved adb binary (locateAdb().resolved)
 * @returns {Promise<{ok:true, version:number, started:boolean} | {ok:false, error, started:boolean}>}
 */
async function ensureServer(client, bin, { execFile = childProcess.execFile, platform = process.platform, retries = 10, retryMs = 300, timeoutMs } = {}) {
  try {
    return { ok: true, version: await client.version(), started: false };
  } catch (e) {
    if (!e || e.kind !== "server-unavailable") return { ok: false, error: e, started: false };
  }
  if (!bin) return { ok: false, error: new Error("no adb binary to start the server with"), started: false };
  const st = await startServer(bin, { execFile, platform, timeoutMs });
  if (!st.ok) return { ok: false, error: new Error(`adb start-server failed: ${st.error}${st.stderr ? ` (${st.stderr.trim()})` : ""}`), started: false };
  let last = null;
  for (let i = 0; i <= retries; i += 1) {
    try {
      return { ok: true, version: await client.version(), started: true };
    } catch (e) {
      last = e;
      await new Promise((r) => setTimeout(r, retryMs));
    }
  }
  return { ok: false, error: last, started: true };
}

module.exports = {
  candidatePaths,
  parseAdbVersion,
  probeVersion,
  locateAdb,
  startServer,
  ensureServer,
};
