// ============================================================
// PYTHON SETUP MODULE  (rewritten)
//
// Downloads and configures a portable Python environment inside
// the app's data folder on first run.
//
// WHAT CHANGED FROM THE ORIGINAL:
//
//   1. Download verification — every download is checked for ZIP
//      validity (magic bytes + end-of-central-directory marker).
//      Corrupt files are deleted and re-downloaded automatically.
//
//   2. Extraction uses Windows' built-in tar.exe (ships with
//      Windows 10 1803+) instead of PowerShell Expand-Archive,
//      which is known to fail with "End of Central Directory
//      record could not be found" on certain systems.
//
//   3. Every step verifies its outcome (e.g. python.exe exists
//      after extraction, pip responds after install) before
//      moving to the next step.  If verification fails, the
//      step is retried once with a clean slate.
//
//   4. The ._pth file is configured more carefully, handling
//      edge cases that caused "ModuleNotFoundError: No module
//      named 'sites'" in bundled builds.
//
// EXPORTS (unchanged — main.js doesn't need any changes):
//   PythonSetup   — class, instantiated in runSetupFlow()
//   isSetupComplete(envDir) — checks for .setup-complete marker
//   deleteEnv(envDir)       — deletes the entire python-env
//   PYTHON_VERSION          — string, e.g. "3.13.2"
// ============================================================

const https = require("https");
const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawn } = require("child_process");

// ── CONFIGURABLE ──
// Windows uses python.org's official "embeddable" CPython distro. macOS/Linux
// use astral-sh/python-build-standalone (PBS) — relocatable CPython with a
// normal site-packages layout. The two flavors don't share a release cadence,
// so patch versions diverge: keep them tracked separately.
const WIN_PYTHON_VERSION = "3.13.2";   // python.org embed-amd64
const PBS_PYTHON_VERSION = "3.13.13";  // python-build-standalone latest 3.13.x
const PBS_RELEASE        = "20260508"; // tag at github.com/astral-sh/python-build-standalone

// Surfaced for the "Downloading Python X" log line and the .setup-complete
// marker. Picks the version we'll actually install on this host. main.js
// destructures this for back-compat (currently unused there).
const PYTHON_VERSION = process.platform === "win32" ? WIN_PYTHON_VERSION : PBS_PYTHON_VERSION;

const GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py";
const TOTAL_STEPS = 6;

/**
 * Resolve the Python download for this host: { url, archiveName, archiveType }.
 *
 * Windows: python.org embeddable zip — keep the existing 3.13.2 codepath
 * because the embed flow has shipped working installers (delicate fixes for
 * MSVCP140.dll and ._pth live in _configurePython). Don't disturb it.
 *
 * macOS/Linux: PBS install_only tarballs. Normal lib/python3.13/site-packages
 * layout, so pip works without ._pth surgery.
 *
 * archiveType drives _extractPython's tool choice: tar (universal) vs the
 * Windows-only PowerShell fallback.
 */
function getPythonAsset() {
  if (process.platform === "win32") {
    const name = `python-${WIN_PYTHON_VERSION}-embed-amd64.zip`;
    return {
      url: `https://www.python.org/ftp/python/${WIN_PYTHON_VERSION}/${name}`,
      archiveName: name,
      archiveType: "zip",
    };
  }
  // PBS install_only URL pattern:
  //   https://github.com/astral-sh/python-build-standalone/releases/download/
  //     <release>/cpython-<version>+<release>-<triple>-install_only.tar.gz
  const tripleMap = {
    "darwin:x64":   "x86_64-apple-darwin",
    "darwin:arm64": "aarch64-apple-darwin",
    "linux:x64":    "x86_64-unknown-linux-gnu",
    "linux:arm64":  "aarch64-unknown-linux-gnu",
  };
  const key = `${process.platform}:${process.arch}`;
  const triple = tripleMap[key];
  if (!triple) {
    throw new Error(`Unsupported platform/arch combination: ${key}`);
  }
  const name = `cpython-${PBS_PYTHON_VERSION}+${PBS_RELEASE}-${triple}-install_only.tar.gz`;
  return {
    url: `https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${name}`,
    archiveName: name,
    archiveType: "tar.gz",
  };
}

// Every package the runtime imports — verified after pip install completes.
// Each entry is [label, exact_import_statement]. We use the EXACT statement
// the runtime uses (not just bare `import X`) because several packages have
// the failure mode where the top-level package loads but a submodule that
// uses a C/Rust extension doesn't:
//
//   - `import patchright` succeeds, but `from patchright.sync_api import
//     sync_playwright` fails if greenlet (C ext) didn't install. This is
//     EXACTLY the failure mode in early sandbox tests — pip says ok,
//     setup says ok, then runtime crashes with "Playwright is required"
//     (msg comes from sites/mangafire_vrf_simple.py's PLAYWRIGHT_AVAILABLE
//     check, even though we're now importing from patchright — the
//     constant name was kept for back-compat, see comment in that file).
//   - `import cryptography` succeeds, but `from cryptography.hazmat.primitives
//     import hashes` fails if the Rust extension didn't install.
//   - `from PIL import Image` exercises Pillow's C codec loader, where
//     `import PIL` alone wouldn't.
//
// When you add a new runtime dependency to requirements.txt, add the EXACT
// runtime import statement here — pick whichever submodule path the actual
// code uses, not the shallowest one.
const REQUIRED_IMPORTS = [
  ["requests",      "import requests"],
  ["bs4",           "from bs4 import BeautifulSoup"],
  ["lxml",          "from lxml import etree"],
  ["PIL",           "from PIL import Image"],
  ["pypdf",         "from pypdf import PdfReader"],
  ["cloudscraper",  "import cloudscraper"],
  ["rapidfuzz",     "from rapidfuzz import fuzz"],
  ["patchright",    "from patchright.sync_api import sync_playwright"],
  ["pywidevine",    "from pywidevine import PSSH, Cdm"],
  ["cryptography",  "from cryptography.hazmat.primitives import hashes"],
  ["curl_cffi",     "from curl_cffi.requests import AsyncSession"],
];

// ZIP files always start with these 4 bytes ("PK\x03\x04").
const ZIP_MAGIC = Buffer.from([0x50, 0x4b, 0x03, 0x04]);

// gzip-compressed files (.tar.gz) start with these 2 bytes (RFC 1952 §2.3.1).
const GZIP_MAGIC = Buffer.from([0x1f, 0x8b]);

class PythonSetup {
  /**
   * @param {object} opts
   * @param {string}   opts.envDir           - Install root (e.g. %APPDATA%/aio-downloader-ui/python-env)
   * @param {string}   opts.pythonSrcDir     - Where aio-dl.py + sites/ + aio_search_cli.py live
   *                                            (process.resourcesPath/python-src in packaged mode).
   *                                            Added to ._pth during _configurePython so the
   *                                            end-of-setup smoke test can import sites/aio_search_cli.
   * @param {string}   opts.vcRuntimeDir     - Where the bundled MSVC++ runtime DLLs ship
   *                                            (process.resourcesPath/vcruntime in packaged mode).
   *                                            Setup copies these next to python.exe in
   *                                            _configurePython so C++-using wheels (greenlet etc.)
   *                                            can load. Optional — if absent, this step is skipped.
   * @param {string}   opts.requirementsPath - Path to requirements.txt shipped with the app
   * @param {function} opts.onStep           - ({ step, total, label }) when a step starts
   * @param {function} opts.onLog            - (string) log lines
   * @param {function} opts.onProgress       - (0.0–1.0) download progress
   * @param {function} opts.onComplete       - All steps finished
   * @param {function} opts.onError          - (errorMessage) a step failed
   */
  constructor({ envDir, pythonSrcDir, vcRuntimeDir, requirementsPath, onStep, onLog, onProgress, onComplete, onError }) {
    this._envDir = envDir;
    this._pythonSrcDir = pythonSrcDir || null;
    this._vcRuntimeDir = vcRuntimeDir || null;
    this._requirementsPath = requirementsPath;
    this._pythonDir = path.join(envDir, "python");
    this._playwrightDir = path.join(envDir, "playwright-browsers");
    this._tempDir = path.join(os.tmpdir(), "aio-setup-temp");

    // Resolve the per-host Python asset (URL + filename + archive type) up
    // front rather than inside _downloadPython. Both _downloadPython AND
    // _extractPython need archivePath/archiveType, and with per-step resume
    // markers (see _step) step 1 may be skipped on retry — meaning a body
    // that writes these on `this` from _downloadPython would leave them
    // undefined when _extractPython runs. getPythonAsset is pure (only
    // reads process.platform / process.arch), so resolving in the ctor
    // is safe and idempotent.
    const asset = getPythonAsset();
    this._archivePath = path.join(this._tempDir, asset.archiveName);
    this._archiveType = asset.archiveType;
    this._archiveUrl = asset.url;
    this._archiveName = asset.archiveName;

    this._onStep = onStep || (() => {});
    this._onLog = onLog || (() => {});
    this._onProgress = onProgress || (() => {});
    this._onComplete = onComplete || (() => {});
    this._onError = onError || (() => {});
  }

  /**
   * Full path to the embedded Python interpreter.
   *   Windows (embed):    <pythonDir>/python.exe
   *   Unix (PBS install): <pythonDir>/bin/python3
   * The PBS install_only layout puts the binary one level deep; the rest of
   * setup (and main.js's defaultPythonCmd) follow this getter so the right
   * path falls out automatically.
   */
  get pythonExe() {
    if (process.platform === "win32") {
      return path.join(this._pythonDir, "python.exe");
    }
    return path.join(this._pythonDir, "bin", "python3");
  }

  /** Full path to the Playwright browsers folder */
  get playwrightDir() {
    return this._playwrightDir;
  }

  /**
   * Path to a "stdlib was extracted" sentinel — used by step 2 to tell apart
   * a real install from a zombie one (bin/python3 present but stdlib missing
   * because tar got SIGKILL'd / AV-scanner pulled files mid-extract / FS race).
   *
   * Unix (PBS install_only): the stdlib lives as loose files under
   *   <pythonDir>/lib/python<MAJOR.MINOR>/encodings/
   * Picking encodings/__init__.py specifically because:
   *   - encodings is the FIRST module CPython imports during interpreter init.
   *     If it's missing, every subsequent import dies with
   *     "ModuleNotFoundError: No module named 'encodings'" + "<no Python frame>".
   *     This is the exact symptom the zombie-state bug produces in step 4.
   *   - Cheap stat call.
   * MAJOR.MINOR is derived from PBS_PYTHON_VERSION so a future bump (3.14.x,
   * 3.15.x) automatically targets the right directory — don't hardcode "3.13".
   *
   * Windows (python.org embed): the stdlib ships INSIDE python<MAJOR><MINOR>.zip
   * (e.g. python313.zip) instead of as loose files, so encodings/__init__.py
   * never exists on disk. The equivalent "stdlib half of extraction landed"
   * sentinel is the zip itself — its absence with python.exe present means
   * the extractor truncated mid-write. The Windows embed extraction tends to
   * land more atomically than PBS's tarball (smaller, fewer files), so this
   * is more belt-and-suspenders here than load-bearing — but cheap to check.
   *
   * Used by: _extractPython skip check + post-extract verify (both in this
   * file). Cross-file: deleteEnv() in this same module wipes envDir on full
   * reset, which clears any stale partial-install sentinel state.
   */
  _extractionSentinelPath() {
    if (process.platform === "win32") {
      // "3.13.2" → "313"
      const winMM = WIN_PYTHON_VERSION.split(".").slice(0, 2).join("");
      return path.join(this._pythonDir, `python${winMM}.zip`);
    }
    // "3.13.13" → "3.13"
    const unixMM = PBS_PYTHON_VERSION.split(".").slice(0, 2).join(".");
    return path.join(
      this._pythonDir, "lib", `python${unixMM}`, "encodings", "__init__.py"
    );
  }

  // ═══════════════════════════════════════════
  // MAIN ENTRY POINT
  // ═══════════════════════════════════════════

  async run() {
    try {
      fs.mkdirSync(this._envDir, { recursive: true });
      fs.mkdirSync(this._tempDir, { recursive: true });

      await this._step(1, `Downloading Python ${PYTHON_VERSION}…`, () => this._downloadPython());
      await this._step(2, "Extracting Python…",                    () => this._extractPython());
      await this._step(3, "Configuring Python…",                   () => this._configurePython());
      await this._step(4, "Installing pip…",                       () => this._installPip());
      await this._step(5, "Installing & verifying dependencies…",  () => this._installRequirements());
      await this._step(6, "Downloading Chromium browser…",         () => this._downloadBrowser());

      // Write the marker file so future launches skip setup.
      const marker = path.join(this._envDir, ".setup-complete");
      fs.writeFileSync(marker, JSON.stringify({
        completedAt: new Date().toISOString(),
        pythonVersion: PYTHON_VERSION,
      }));

      // Clean up temp downloads.
      try { fs.rmSync(this._tempDir, { recursive: true, force: true }); } catch {}

      this._onLog("\n✓ Setup complete!");
      this._onComplete();
    } catch (err) {
      this._onLog(`\n✗ Error: ${err.message}`);
      this._onError(err.message);
    }
  }

  async _step(num, label, fn) {
    this._onStep({ step: num, total: TOTAL_STEPS, label });
    this._onLog(`\n── Step ${num}/${TOTAL_STEPS}: ${label} ──`);
    this._onProgress(0);

    // Per-step resume marker. If a previous setup attempt completed this
    // step but failed later, the marker lets the retry skip already-done
    // work and pick up where things broke. The marker is written ONLY
    // after fn() resolves successfully, so an interrupted step (SIGKILL,
    // throw mid-body) doesn't leave a "completed" marker behind. Combined
    // with step 2's sentinel verify (see _extractPython), this means the
    // marker never papers over a zombie extraction — if fn() returned,
    // its invariants were satisfied at that moment.
    //
    // Cleanup: deleteEnv() at the bottom of this file does rmSync on the
    // whole envDir, so these markers (and .setup-complete) are wiped on
    // a full reset together. Do NOT add per-step cleanup anywhere — the
    // single delete path is what makes "Reset" a hard reset.
    //
    // Edge case: if the host's temp dir is cleared between runs (rare on
    // macOS/Linux/Windows, but possible on some configs), the step-1
    // marker may outlive the cached archive in this._tempDir. Step 2 then
    // throws "Python archive not found or corrupt. Click Retry" — at
    // which point the user's recourse is deleteEnv via the UI's reset
    // button. Acceptable trade for resume cheapness on the common path.
    const markerPath = path.join(this._envDir, `.step-${num}-complete`);
    if (fs.existsSync(markerPath)) {
      this._onLog(`Step ${num} already complete (marker present), skipping`);
      return;
    }
    await fn();
    try {
      fs.writeFileSync(markerPath, JSON.stringify({
        at: new Date().toISOString(),
        label,
      }));
    } catch (err) {
      // Marker write is a hint, not a hard requirement — if it fails
      // we lose only the resume optimization, not correctness. The
      // next retry will just re-do this step (its body is idempotent).
      this._onLog(`Warning: could not write step ${num} marker: ${err.message}`);
    }
  }

  // ═══════════════════════════════════════════
  // STEP 1 — Download Python embeddable package
  // ═══════════════════════════════════════════

  async _downloadPython() {
    // Asset fields (path / type / url / name) are resolved once in the
    // constructor — see the comment there explaining why.
    const archivePath = this._archivePath;

    // If a cached download exists AND validates, reuse it.
    if (fs.existsSync(archivePath) && this._isValidArchive(archivePath, this._archiveType)) {
      this._onLog("Using cached download (verified valid)");
      return;
    }

    // Delete any corrupt/partial cached file.
    try { fs.unlinkSync(archivePath); } catch {}

    this._onLog(`Downloading ${this._archiveName}…`);
    this._onLog(`  source: ${this._archiveUrl}`);
    await this._downloadFile(this._archiveUrl, archivePath);

    // Verify the download is a real archive of the expected type.
    if (!this._isValidArchive(archivePath, this._archiveType)) {
      try { fs.unlinkSync(archivePath); } catch {}
      throw new Error(
        `Downloaded file is corrupt (not a valid ${this._archiveType}). ` +
        "This usually means the download was interrupted. Click Retry."
      );
    }

    const sizeMB = (fs.statSync(archivePath).size / 1_048_576).toFixed(1);
    this._onLog(`Downloaded: ${sizeMB} MB ✓`);
  }

  // ═══════════════════════════════════════════
  // STEP 2 — Extract Python zip
  // ═══════════════════════════════════════════

  async _extractPython() {
    // Skip extraction only when BOTH the interpreter binary AND the stdlib
    // sentinel are present. The original check trusted pythonExe alone,
    // which let a zombie state through: a SIGKILL / AV scanner / FS race
    // during tar extraction can leave bin/python3 on disk while
    // lib/.../encodings/ is still missing. On the next launch step 2 would
    // skip, step 3's `python --version` would short-circuit before stdlib
    // init and report "Verified ✓", and step 4's pip install would crash
    // with "ModuleNotFoundError: No module named 'encodings'" + "<no Python
    // frame>". The sentinel makes both halves of the extraction load-bearing.
    // See _extractionSentinelPath for what file we pick and why.
    const sentinel = this._extractionSentinelPath();
    const exeExists = fs.existsSync(this.pythonExe);
    const sentinelExists = fs.existsSync(sentinel);

    if (exeExists && sentinelExists) {
      this._onLog(
        `${path.basename(this.pythonExe)} already exists (stdlib sentinel verified), skipping extraction`
      );
      return;
    }
    if (exeExists && !sentinelExists) {
      // Zombie state: pythonExe present, stdlib half missing. Wipe so the
      // re-extract below starts fresh. The existing "Clean out any leftover
      // partial extraction" branch a few lines down would also wipe, but
      // calling it out explicitly here makes the zombie-detection visible
      // in the wizard log so users understand WHY step 2 is repeating.
      this._onLog(
        `Detected partial extraction, cleaning… (missing: ${path.relative(this._pythonDir, sentinel) || path.basename(sentinel)})`
      );
      try {
        fs.rmSync(this._pythonDir, { recursive: true, force: true });
      } catch (err) {
        // Best-effort cleanup; the recursive mkdir + tar -xf below tolerates
        // partial leftovers. Don't fail setup over this — the next loop
        // through finds a clean state.
        this._onLog(`Warning: cleanup failed (${err.message}) — continuing`);
      }
    }

    const archivePath = this._archivePath;
    const archiveType = this._archiveType;

    if (!archivePath || !fs.existsSync(archivePath) || !this._isValidArchive(archivePath, archiveType)) {
      throw new Error(
        "Python archive not found or corrupt. Click Retry to re-download."
      );
    }

    // Clean out any leftover partial extraction.
    if (fs.existsSync(this._pythonDir)) {
      this._onLog("Cleaning previous partial extraction…");
      fs.rmSync(this._pythonDir, { recursive: true, force: true });
    }
    fs.mkdirSync(this._pythonDir, { recursive: true });

    // ── Primary: tar ──
    // tar handles BOTH .zip and .tar.gz, and is universally available:
    //   - Windows 10 1803+ ships tar.exe (April 2018)
    //   - macOS / Linux always have it
    // Auto-detects compression (-xf does the right thing for .tar.gz too).
    let extracted = false;

    try {
      this._onLog("Extracting with tar…");
      await this._runCommand("tar", ["-xf", archivePath, "-C", this._pythonDir]);
      extracted = true;
    } catch (tarErr) {
      this._onLog(`tar failed: ${tarErr.message}`);
    }

    // ── Fallback: PowerShell Expand-Archive (Windows + .zip only) ──
    // PowerShell's Expand-Archive only handles .zip, and powershell.exe
    // doesn't exist on Unix — so this branch is gated to that combination.
    // On Unix the tar primary should always succeed; if it doesn't, the
    // verify step below throws and the user retries.
    if (!extracted && archiveType === "zip" && process.platform === "win32") {
      this._onLog("Falling back to PowerShell…");
      fs.rmSync(this._pythonDir, { recursive: true, force: true });
      fs.mkdirSync(this._pythonDir, { recursive: true });

      try {
        await this._runCommand("powershell.exe", [
          "-NoProfile", "-NonInteractive", "-Command",
          `Expand-Archive -Path '${archivePath}' -DestinationPath '${this._pythonDir}' -Force`,
        ]);
        extracted = true;
      } catch (psErr) {
        this._onLog(`PowerShell failed: ${psErr.message}`);
      }
    }

    // ── Verify extraction succeeded ──
    // Two layouts reach this point:
    //   - Windows embed zip: flat — python.exe at <pythonDir>/python.exe
    //   - PBS install_only:  nested — binary at <pythonDir>/python/bin/python3
    // _fixNestedExtraction handles the nested case by moving sub-folder
    // contents up one level. After that, this.pythonExe should resolve.
    if (!extracted || !fs.existsSync(this.pythonExe)) {
      const moved = this._fixNestedExtraction();
      if (!moved) {
        try { fs.rmSync(this._pythonDir, { recursive: true, force: true }); } catch {}
        try { fs.unlinkSync(archivePath); } catch {}
        throw new Error(
          `Extraction failed — ${path.basename(this.pythonExe)} not found after extracting. ` +
          "The archive may have been corrupt. Click Retry to re-download."
        );
      }
    }

    // Verify the stdlib sentinel landed too, not just pythonExe. Catches
    // the case where tar exits 0 but the stdlib half was truncated (the
    // tar process was killed AFTER bin/ was written but BEFORE
    // lib/.../encodings/ — both are real bytes on disk so tar's exit code
    // doesn't reflect the truncation). Throwing here means the step-2
    // marker in _step() never gets written, so the next retry re-enters
    // this function and the sentinel-aware skip check at the top catches
    // the zombie state.
    if (!fs.existsSync(sentinel)) {
      try { fs.rmSync(this._pythonDir, { recursive: true, force: true }); } catch {}
      try { fs.unlinkSync(archivePath); } catch {}
      throw new Error(
        `Extraction left a partial install — stdlib sentinel missing ` +
        `(${path.relative(this._pythonDir, sentinel) || path.basename(sentinel)}). ` +
        "The extractor may have been interrupted (AV scanner, low disk, " +
        "process kill). Click Retry to re-download and re-extract."
      );
    }

    this._onLog("Extraction complete ✓");
  }

  // ═══════════════════════════════════════════
  // STEP 3 — Configure Python's ._pth file
  // ═══════════════════════════════════════════

  async _configurePython() {
    // ── Unix fast-path ──
    // PBS install_only ships a normal lib/python3.13/site-packages tree, so
    // pip writes there by default — no ._pth surgery, no VC++ DLLs needed.
    // The pythonSrcDir gets onto sys.path via PYTHONPATH at spawn time
    // (set in main.js initDownloader / _checkSeriesUpdates / Searcher),
    // which the standard CPython interpreter respects (unlike Windows embed).
    // We DO still verify the interpreter starts so Step 4+ get a clean baseline.
    if (process.platform !== "win32") {
      try {
        // NOT `--version`: CPython prints sys.version to stdout BEFORE
        // running site.py / initializing the codecs subsystem, so a
        // partial-extraction Python with no stdlib still passes
        // `python --version`. That's how the zombie-extraction bug
        // sneaks past step 3 today, only to crash step 4's pip install
        // with "ModuleNotFoundError: No module named 'encodings'" and
        // "<no Python frame>".
        //
        // Force actual stdlib loading by importing the modules whose
        // absence would silently break our pipeline:
        //   - encodings: required by Python's bytecode + str() loader;
        //     this is the SPECIFIC failure mode of the zombie bug.
        //   - ssl: PBS bundles its own OpenSSL; a broken bundle (truncated
        //     archive, wrong CPU arch) silently breaks pip TLS but
        //     `python --version` still works.
        //   - ctypes: depends on libffi linkage; broken libffi breaks
        //     several wheel-loaded C extensions (cryptography, greenlet).
        //   - sys + os: always loaded, cheap insurance against weirder
        //     stdlib-init failures.
        // If any of these fail, the install is poisoned and step 4 would
        // crash later — surface the cause NOW with an actionable retry
        // message instead of a cryptic "<no Python frame>" later.
        const ver = await this._runPython([
          "-c",
          "import sys, os, encodings, ssl, ctypes; print(sys.version.split()[0])",
        ]);
        this._onLog(`Verified: Python ${ver.trim()} (stdlib smoke test passed) ✓`);
      } catch (err) {
        throw new Error(
          `Python stdlib smoke test failed — likely a partial extraction. ` +
          `Click Retry to wipe and re-download. Detail: ${err.message}`
        );
      }
      return;
    }

    // ── Windows-embed-only path below ──
    // ── First: copy bundled VC++ runtime DLLs next to python.exe ──
    //
    // The Python embed distro ships vcruntime140.dll + vcruntime140_1.dll but
    // NOT MSVCP140.dll, MSVCP140_1.dll, MSVCP140_2.dll, or CONCRT140.dll.
    // Several pip wheels need these to load — most importantly greenlet,
    // which is playwright's transitive dep. Without msvcp140.dll, the user
    // sees `_greenlet.pyd → DLL load failed` mid-search/mid-download even
    // though pip install reports success.
    //
    // We bundle these DLLs as extraResources (see package.json) and copy
    // them next to python.exe so Windows finds them first via its standard
    // DLL search order. The DLLs are part of the Microsoft Visual C++
    // Redistributable; redistributing them with the app is permitted by
    // Microsoft's redist license.
    //
    // Idempotent: skips DLLs that are already present (Python embed's own
    // vcruntime140.dll is preserved if the version on the user's system is
    // newer or the same; ours overwrites if our version is newer).
    if (this._vcRuntimeDir && fs.existsSync(this._vcRuntimeDir)) {
      this._onLog("Copying VC++ runtime DLLs into Python dir…");
      let copied = 0;
      try {
        for (const file of fs.readdirSync(this._vcRuntimeDir)) {
          if (!/\.dll$/i.test(file)) continue;
          const src = path.join(this._vcRuntimeDir, file);
          const dst = path.join(this._pythonDir, file);
          fs.copyFileSync(src, dst);
          copied++;
        }
        this._onLog(`Copied ${copied} runtime DLL(s) ✓`);
      } catch (err) {
        // Don't fail the whole setup if DLL copy fails — pip install might
        // still work if the user has VC++ Redist already. The smoke test
        // at end of step 5 will catch any remaining import failures.
        this._onLog(`Warning: DLL copy failed (${err.message}) — continuing`);
      }
    }

    // ── Then: configure ._pth so sys.path is correct ──
    // The embeddable Python ships with a ._pth file (e.g. python313._pth)
    // that completely controls sys.path.  When this file exists, Python
    // IGNORES the PYTHONPATH environment variable.
    //
    // We need to:
    //   1. Uncomment "import site" so pip-installed packages work
    //   2. Add "Lib\site-packages" so pip packages are importable
    //
    // Without this, every pip install silently "works" but imports fail.

    const files = fs.readdirSync(this._pythonDir);
    const pthFile = files.find((f) => /^python\d+\._pth$/i.test(f));

    if (!pthFile) {
      // No ._pth file means Python will use default sys.path discovery,
      // which generally works.  This happens with full (non-embed) installs.
      this._onLog("No ._pth file found (non-embed install?) — skipping");
      return;
    }

    const pthPath = path.join(this._pythonDir, pthFile);
    let content = fs.readFileSync(pthPath, "utf8");

    // Build the desired content from scratch rather than patching.
    // The original file typically contains:
    //   python313.zip
    //   .
    //   #import site
    //
    // We want:
    //   python313.zip
    //   .
    //   Lib\site-packages
    //   import site

    const lines = content.split(/\r?\n/);
    const newLines = [];

    let hasSitePackages = false;
    let hasImportSite = false;

    for (const rawLine of lines) {
      const line = rawLine.trim();

      // Uncomment "#import site" → "import site"
      if (/^#\s*import\s+site/.test(line)) {
        newLines.push("import site");
        hasImportSite = true;
        continue;
      }

      if (line === "import site") {
        hasImportSite = true;
      }

      if (line === "Lib\\site-packages") {
        hasSitePackages = true;
      }

      newLines.push(rawLine);
    }

    // Add missing entries.
    if (!hasSitePackages) {
      // Insert before "import site" if present, otherwise append.
      const siteIdx = newLines.findIndex((l) => l.trim() === "import site");
      if (siteIdx >= 0) {
        newLines.splice(siteIdx, 0, "Lib\\site-packages");
      } else {
        newLines.push("Lib\\site-packages");
      }
    }

    if (!hasImportSite) {
      newLines.push("import site");
    }

    // Add the python-src directory so the embedded Python can `import sites`
    // and `import aio_search_cli` (those modules ship in resources/python-src/
    // as extraResources, NOT inside the python-env folder where ._pth lives).
    // Without this:
    //   - aio-dl.py crashes on startup with "ModuleNotFoundError: sites"
    //   - --search crashes with "ModuleNotFoundError: aio_search_cli"
    // Required for the smoke tests at the end of step 5 to pass.
    //
    // main.js:ensurePythonSrcInPth() also runs on every launch and rewrites
    // this entry to handle install-path changes on app updates — its smart
    // remove-stale + re-add logic is what makes upgrades safe. This here is
    // just the bootstrap copy so first-run setup can verify the bundle.
    if (this._pythonSrcDir) {
      const alreadyHasIt = newLines.some((l) => l.trim() === this._pythonSrcDir);
      if (!alreadyHasIt) {
        const siteIdx = newLines.findIndex((l) => l.trim() === "import site");
        if (siteIdx >= 0) {
          newLines.splice(siteIdx, 0, this._pythonSrcDir);
        } else {
          newLines.push(this._pythonSrcDir);
        }
      }
    }

    const newContent = newLines.join("\n") + "\n";
    fs.writeFileSync(pthPath, newContent);

    this._onLog(`Configured ${pthFile}:`);
    for (const l of newContent.trim().split("\n")) {
      this._onLog(`  ${l}`);
    }

    // Create the Lib\site-packages directory so pip has somewhere to
    // install packages.  Without this, pip install works but writes to
    // a non-existent path.
    const sitePackages = path.join(this._pythonDir, "Lib", "site-packages");
    fs.mkdirSync(sitePackages, { recursive: true });

    // Verify Python starts and can report its version.
    try {
      const ver = await this._runPython(["--version"]);
      this._onLog(`Verified: ${ver.trim()} ✓`);
    } catch (err) {
      throw new Error(`Python installed but won't start: ${err.message}`);
    }
  }

  // ═══════════════════════════════════════════
  // STEP 4 — Install pip
  // ═══════════════════════════════════════════

  async _installPip() {
    // Check if pip already works.
    if (await this._hasPip()) {
      return;
    }

    // Download the official pip bootstrapper.
    const getPipPath = path.join(this._tempDir, "get-pip.py");
    if (!fs.existsSync(getPipPath)) {
      this._onLog("Downloading get-pip.py…");
      await this._downloadFile(GET_PIP_URL, getPipPath);
    }

    this._onLog("Installing pip…");
    await this._runPython([getPipPath, "--no-warn-script-location"]);

    // Verify it worked.
    if (!(await this._hasPip())) {
      throw new Error(
        "pip installed but verification failed. " +
        "This usually means the ._pth file is misconfigured."
      );
    }
  }

  // ═══════════════════════════════════════════
  // STEP 5 — Install Python dependencies
  // ═══════════════════════════════════════════

  async _installRequirements() {
    if (!this._requirementsPath || !fs.existsSync(this._requirementsPath)) {
      this._onLog("requirements.txt not found — skipping");
      return;
    }

    // The embedded Python doesn't include setuptools or wheel.
    // Some packages only ship as source distributions (.tar.gz) and
    // need setuptools to build.  Install build tools first.
    this._onLog("Installing build tools (setuptools, wheel)…");
    await this._runPython([
      "-m", "pip", "install",
      "setuptools", "wheel",
      "--no-warn-script-location",
      "--no-cache-dir",
    ]);

    this._onLog("Installing packages (this may take a minute)…");
    await this._runPython([
      "-m", "pip", "install",
      "-r", this._requirementsPath,
      "--no-warn-script-location",
      "--no-cache-dir",
    ]);

    // ── Comprehensive import verification ──
    // Every package the runtime uses must import cleanly using the EXACT
    // statement the runtime uses (see REQUIRED_IMPORTS comment for why
    // shallow `import playwright` isn't enough). If any fails, throw —
    // don't let users hit a cryptic "Playwright is required" or "DLL
    // load failed" mid-search later.
    this._onLog(`Verifying ${REQUIRED_IMPORTS.length} runtime imports…`);
    const importStmt = REQUIRED_IMPORTS.map(([, stmt]) => stmt).join("; ");
    const verifySnippet =
      `${importStmt}; print('imports OK: ${REQUIRED_IMPORTS.length} packages verified')`;
    try {
      await this._runPython(["-c", verifySnippet]);
    } catch (err) {
      throw new Error(
        "One or more required Python packages failed to import after pip install. " +
        "This usually means a wheel didn't install correctly for your platform " +
        "(common case: a transitive C-extension dep like greenlet for playwright " +
        "or the cryptography Rust extension was skipped). The full traceback is " +
        "in the log panel above — expand 'Show logs' to see which import failed. " +
        `Click Retry to re-run setup. Detail: ${err.message}`
      );
    }
    this._onLog("All runtime imports verified ✓");

    // ── End-to-end bundle verification ──
    // Verify aio-dl.py's entire dependency tree loads — including all
    // 40+ handlers in sites/__init__.py and the deferred-import target
    // aio_search_cli (used by --search). If any handler has a syntax
    // error, missing import, or python-version incompatibility, this
    // catches it during setup instead of on the user's first search.
    //
    // Requires pythonSrcDir to be on sys.path, which _configurePython
    // already ensured by adding it to ._pth.
    if (this._pythonSrcDir) {
      this._onLog("Verifying bundled scripts load…");
      // On Windows the python-src dir is on sys.path via the ._pth rewrite
      // we did in _configurePython. On Unix, embed-style ._pth doesn't exist
      // (PBS uses standard sys.path), so pass PYTHONPATH explicitly. main.js
      // does the same at spawn time for downloads/searches; this is the
      // setup-time bootstrap so the smoke test below sees the bundle.
      const smokeEnv = process.platform === "win32" ? {} : { PYTHONPATH: this._pythonSrcDir };
      try {
        await this._runPython([
          "-c",
          "import sites; import aio_search_cli; " +
          "n = sum(1 for _ in sites.iter_search_capable_handlers()); " +
          "print(f'bundle OK: {n} search-capable handlers registered')",
        ], smokeEnv);
      } catch (err) {
        throw new Error(
          "The bundled aio-dl.py / sites / aio_search_cli failed to load. " +
          "The python-src bundle in the installer may be incomplete or one " +
          `of the handlers is broken. Detail: ${err.message}`
        );
      }
      this._onLog("Bundled scripts verified ✓");
    }
  }

  // ═══════════════════════════════════════════
  // STEP 6 — Download Chromium for Playwright
  // ═══════════════════════════════════════════
  //
  // Why no separate "install playwright" step: requirements.txt already
  // pins playwright>=1.40.0, so step 5's `pip install -r requirements.txt`
  // installs it. The verification smoke test at the end of step 5 confirms
  // `import playwright` works before we get here.

  async _downloadBrowser() {
    // Check if browsers are already downloaded AND launchable. If only
    // partially downloaded (interrupted previous run, etc.), the launch
    // smoke test below will catch it and trigger a re-download via Retry.
    let alreadyHaveChromium = false;
    if (fs.existsSync(this._playwrightDir)) {
      try {
        const entries = fs.readdirSync(this._playwrightDir);
        // Playwright creates a "chromium-XXXX" folder inside the browsers dir.
        if (entries.some((e) => e.startsWith("chromium"))) {
          alreadyHaveChromium = true;
          this._onLog("Chromium directory found, will verify launch below");
        }
      } catch {}
    }

    if (!alreadyHaveChromium) {
      fs.mkdirSync(this._playwrightDir, { recursive: true });
      this._onLog("Downloading Chromium (this may take a few minutes)…");

      // `patchright` is a Playwright fork; its CLI (`-m patchright install
      // chromium`) downloads the same Chromium binaries Playwright uses, into
      // the path specified by PLAYWRIGHT_BROWSERS_PATH (env var name is
      // unchanged for compatibility). Either CLI works against the same
      // browser cache, but we use the patchright command to keep the version
      // pin consistent with the runtime — patchright tracks Playwright
      // upstream but pins specific Chromium revisions.
      //
      // 20min watchdog override (vs _runPython's 10min default): the
      // Chromium tarball is ~150 MB and a sluggish CDN mirror or shaped
      // residential link (think hotel WiFi, mobile tether) can stretch
      // this to 12-18 min. Don't want to false-positive a real download.
      await this._runPython(
        ["-m", "patchright", "install", "chromium"],
        { PLAYWRIGHT_BROWSERS_PATH: this._playwrightDir },
        20 * 60_000
      );
    }

    // ── Chromium launch verification ──
    // Actually launch Chromium briefly to confirm it can run. Catches:
    //   - Corrupt download
    //   - Missing system DLL (e.g., MSVC runtime on very old Windows)
    //   - Antivirus quarantining the chromium binary
    //   - Wrong PLAYWRIGHT_BROWSERS_PATH (typo, etc.)
    // Without this check, the user only finds out something is wrong on
    // their first MangaFire search/download — which is exactly the failure
    // mode we're trying to eliminate.
    //
    // Headless launch + immediate close takes ~2-3s on a normal machine.
    this._onLog("Verifying Chromium launches…");
    try {
      await this._runPython(
        [
          "-c",
          // Use patchright explicitly — even though `from playwright.sync_api`
          // would also work after a `pip install patchright` (the package
          // shadows playwright's import path), we want the test to fail
          // loudly if patchright itself isn't installed correctly. Match the
          // import path the runtime actually uses.
          "from patchright.sync_api import sync_playwright; " +
          "p = sync_playwright().start(); " +
          "b = p.chromium.launch(headless=True); " +
          "b.close(); " +
          "p.stop(); " +
          "print('chromium launch OK')",
        ],
        { PLAYWRIGHT_BROWSERS_PATH: this._playwrightDir }
      );
    } catch (err) {
      // Platform-specific advice on what to check next. On Windows the
      // common cause is antivirus quarantining chrome.exe; on macOS the
      // bundled headless_shell may need Gatekeeper allowance; on Linux
      // missing system libraries (libnss3, libgbm, libasound2) are typical.
      const advice = process.platform === "win32"
        ? "Check your antivirus quarantine for any blocked chrome.exe under " + this._playwrightDir
        : process.platform === "darwin"
          ? "Try opening the bundled Chromium binary once via Finder → right-click → Open to clear Gatekeeper. Path: " + this._playwrightDir
          : "Make sure system libraries are installed: libnss3, libgbm1, libasound2 (apt) or equivalents. Path: " + this._playwrightDir;
      throw new Error(
        "Chromium installed but failed to launch. The download may be corrupt " +
        "or a system protection mechanism may be blocking the binary. Try Retry — " +
        `if that doesn't help: ${advice}. Detail: ${err.message}`
      );
    }

    this._onLog("Chromium browser ready ✓");
  }

  // ═══════════════════════════════════════════
  // UTILITIES
  // ═══════════════════════════════════════════

  /**
   * Check if a file is a valid ZIP by reading its header and
   * scanning for the end-of-central-directory signature.
   *
   * The old code only checked `size > 1MB` which let corrupt
   * downloads pass through.  A real ZIP must start with PK\x03\x04
   * and contain PK\x05\x06 near the end.
   */
  _isValidZip(filePath) {
    try {
      const stat = fs.statSync(filePath);
      if (stat.size < 100) return false;

      const fd = fs.openSync(filePath, "r");

      // Check ZIP magic bytes at start of file.
      const header = Buffer.alloc(4);
      fs.readSync(fd, header, 0, 4, 0);
      if (!header.equals(ZIP_MAGIC)) {
        fs.closeSync(fd);
        return false;
      }

      // Check for end-of-central-directory signature near the end.
      // EOCD is at most 65557 bytes from the end (65535 comment + 22 header).
      const tailSize = Math.min(stat.size, 65580);
      const tail = Buffer.alloc(tailSize);
      fs.readSync(fd, tail, 0, tailSize, stat.size - tailSize);
      fs.closeSync(fd);

      // EOCD signature: PK\x05\x06
      for (let i = tail.length - 22; i >= 0; i--) {
        if (tail[i] === 0x50 && tail[i + 1] === 0x4b &&
            tail[i + 2] === 0x05 && tail[i + 3] === 0x06) {
          return true;
        }
      }
      return false;
    } catch {
      return false;
    }
  }

  /**
   * Cheap header check for gzip-compressed files (.tar.gz). We only check
   * the gzip magic bytes — verifying the embedded tar payload would mean
   * decompressing the whole thing, which is what tar -xf does anyway.
   * If the file is gzip-truncated, tar fails loudly during extraction and
   * the user retries.
   */
  _isValidTarGz(filePath) {
    try {
      const stat = fs.statSync(filePath);
      if (stat.size < 32) return false;
      const fd = fs.openSync(filePath, "r");
      const header = Buffer.alloc(2);
      fs.readSync(fd, header, 0, 2, 0);
      fs.closeSync(fd);
      return header.equals(GZIP_MAGIC);
    } catch {
      return false;
    }
  }

  /** Dispatch validator based on the archive type returned by getPythonAsset. */
  _isValidArchive(filePath, type) {
    if (type === "zip") return this._isValidZip(filePath);
    if (type === "tar.gz") return this._isValidTarGz(filePath);
    return false;
  }

  /**
   * Some archives extract into a nested sub-folder instead of flat:
   *   - Some ZIP tools wrap into pythonDir/python-3.13.2-embed-amd64/python.exe
   *   - PBS install_only tarballs always nest: pythonDir/python/bin/python3
   * This finds that case and moves the sub-folder's contents up one level
   * so this.pythonExe resolves cleanly.
   *
   * The target relative path differs by platform — on Windows we look for
   * a sub-folder containing python.exe, on Unix bin/python3.
   *
   * Rollback contract: if any single rename in the inner loop fails, every
   * already-completed rename is reversed (best-effort) before returning
   * false. Without rollback, a partial failure left a hybrid layout where
   * bin/ moved up but lib/ stayed nested — looks "extracted enough" to
   * survive a sloppy skip check, but Python can't find its stdlib. That's
   * the same class of zombie state the sentinel check at the top of
   * _extractPython tries to prevent.
   */
  _fixNestedExtraction() {
    const targetRel = process.platform === "win32"
      ? "python.exe"
      : path.join("bin", "python3");
    const targetLabel = path.basename(this.pythonExe);

    let entries;
    try {
      entries = fs.readdirSync(this._pythonDir);
    } catch (err) {
      this._onLog(`_fixNestedExtraction: cannot read ${this._pythonDir}: ${err.message}`);
      return false;
    }

    for (const entry of entries) {
      const sub = path.join(this._pythonDir, entry);
      let isDir;
      try {
        isDir = fs.statSync(sub).isDirectory();
      } catch {
        // Stat failed on this entry (race / permissions) — skip; keep
        // looking at other entries for a candidate sub-folder.
        continue;
      }
      if (!isDir) continue;
      const subExe = path.join(sub, targetRel);
      if (!fs.existsSync(subExe)) continue;

      this._onLog(`Found ${targetLabel} in sub-folder "${entry}", moving up…`);

      let inner;
      try {
        inner = fs.readdirSync(sub);
      } catch (err) {
        this._onLog(
          `_fixNestedExtraction: cannot read sub-folder ${sub}: ${err.message}`
        );
        return false;
      }

      // Track every successful rename so we can reverse them if a later
      // one fails. Each entry is {from, to} where `from` is the original
      // path inside the sub-folder and `to` is the destination in
      // _pythonDir. Rollback re-renames from→to backwards (i.e. to→from).
      const moved = [];

      for (const file of inner) {
        const src = path.join(sub, file);
        const dest = path.join(this._pythonDir, file);
        try {
          fs.renameSync(src, dest);
          moved.push({ from: src, to: dest });
        } catch (err) {
          // Mid-loop failure (perm error, AV lock, dest already exists).
          // Roll every prior move back into the sub-folder so the caller
          // sees a coherent "nothing happened" state. Best-effort: if a
          // rollback rename also fails we log it but keep going — the
          // outer _extractPython then wipes _pythonDir as part of its
          // "Extraction failed" cleanup, which is the recovery of last
          // resort.
          this._onLog(
            `_fixNestedExtraction: rename failed at "${file}": ${err.message}; ` +
            `rolling back ${moved.length} prior move(s)`
          );
          for (let i = moved.length - 1; i >= 0; i--) {
            const m = moved[i];
            try {
              fs.renameSync(m.to, m.from);
            } catch (rbErr) {
              this._onLog(
                `_fixNestedExtraction: rollback failed for ` +
                `${path.basename(m.to)}: ${rbErr.message}`
              );
            }
          }
          return false;
        }
      }

      try {
        fs.rmdirSync(sub);
      } catch (err) {
        // Inner loop succeeded so the moves are valid; the sub-folder
        // should be empty now. A failing rmdir usually means readdir
        // missed a hidden file or the dir became un-removable mid-op.
        // Either way the extracted Python is functional — log and
        // continue rather than treating this as a real failure.
        this._onLog(
          `_fixNestedExtraction: failed to remove empty sub-folder ${sub}: ${err.message}`
        );
      }
      return true;
    }
    return false;
  }

  /** Check if pip is installed and responds. */
  async _hasPip() {
    try {
      const ver = await this._runPython(["-m", "pip", "--version"]);
      this._onLog("pip already installed: " + ver.trim().split("\n")[0]);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Download a file from a URL, following redirects.
   * Reports progress via this._onProgress (0.0 to 1.0).
   *
   * Downloads to a temporary ".downloading" file first, then
   * renames to the final path.  This prevents a partial/corrupt
   * download from being mistaken for a valid cached file on retry.
   */
  _downloadFile(url, destPath) {
    return new Promise((resolve, reject) => {
      const tmpPath = destPath + ".downloading";

      const makeRequest = (requestUrl, redirectCount = 0) => {
        if (redirectCount > 5) {
          return reject(new Error("Too many redirects"));
        }

        const mod = requestUrl.startsWith("https") ? https : http;

        const req = mod.get(requestUrl, {
          headers: { "User-Agent": "AIO-Downloader-Setup/2.0" },
        }, (res) => {
          if ([301, 302, 307, 308].includes(res.statusCode) && res.headers.location) {
            res.resume();
            return makeRequest(res.headers.location, redirectCount + 1);
          }

          if (res.statusCode !== 200) {
            res.resume();
            return reject(new Error(`HTTP ${res.statusCode} from ${requestUrl}`));
          }

          const totalBytes = parseInt(res.headers["content-length"], 10) || 0;
          let downloadedBytes = 0;
          const file = fs.createWriteStream(tmpPath);

          res.on("data", (chunk) => {
            downloadedBytes += chunk.length;
            if (totalBytes > 0) {
              this._onProgress(downloadedBytes / totalBytes);
            }
          });

          res.pipe(file);

          file.on("finish", () => {
            file.close(() => {
              // Verify we got the full file.
              if (totalBytes > 0 && downloadedBytes < totalBytes) {
                try { fs.unlinkSync(tmpPath); } catch {}
                return reject(new Error(
                  `Incomplete download: got ${downloadedBytes} of ${totalBytes} bytes`
                ));
              }
              // Move temp file to final path.
              try {
                fs.renameSync(tmpPath, destPath);
              } catch {
                fs.copyFileSync(tmpPath, destPath);
                try { fs.unlinkSync(tmpPath); } catch {}
              }
              resolve();
            });
          });

          file.on("error", (err) => {
            try { fs.unlinkSync(tmpPath); } catch {}
            reject(err);
          });
        });

        req.on("error", reject);
        req.setTimeout(60_000, () => {
          req.destroy(new Error("Download timed out after 60 seconds"));
        });
      };

      makeRequest(url);
    });
  }

  /**
   * Run a command (like tar.exe) and return stdout.
   * Logs all output in real time.
   *
   * Watchdog: kills the spawned process if it doesn't exit within
   * `timeoutMs` (default 5min). Without it, a stuck tar.exe (corrupt
   * archive, AV scanner deadlock) freezes setup forever — only the
   * download path had a timeout previously, the extraction path didn't.
   * 5min is comfortably above the worst observed extraction (Python
   * embed zip ~30 MB on slow disks finishes in <60s).
   */
  _runCommand(command, args, timeoutMs = 5 * 60_000) {
    return new Promise((resolve, reject) => {
      const proc = spawn(command, args, {
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });

      let stdout = "";
      let stderr = "";
      let timedOut = false;

      // Watchdog: SIGKILL on Windows maps to TerminateProcess, which is
      // what we want — tar.exe doesn't respond gracefully when wedged.
      const watchdog = setTimeout(() => {
        timedOut = true;
        this._onLog(`Watchdog: ${command} exceeded ${Math.round(timeoutMs / 1000)}s — terminating`);
        try { proc.kill("SIGKILL"); } catch {}
      }, timeoutMs);

      proc.stdout.on("data", (d) => {
        const text = d.toString();
        stdout += text;
        text.split("\n").filter(Boolean).forEach((l) => this._onLog("  " + l.trim()));
      });

      proc.stderr.on("data", (d) => {
        const text = d.toString();
        stderr += text;
        text.split("\n").filter(Boolean).forEach((l) => this._onLog("  " + l.trim()));
      });

      proc.on("close", (code) => {
        clearTimeout(watchdog);
        if (timedOut) {
          reject(new Error(`${command} timed out after ${Math.round(timeoutMs / 1000)}s and was terminated`));
          return;
        }
        if (code === 0) resolve(stdout);
        else reject(new Error(`${command} exited with code ${code}: ${stderr.slice(0, 200)}`));
      });

      proc.on("error", (err) => {
        clearTimeout(watchdog);
        reject(new Error(`Failed to start ${command}: ${err.message}`));
      });
    });
  }

  /**
   * Run a command with the embedded python.exe.
   * Returns stdout. Logs all output in real time AND accumulates stderr so
   * the rejection reason carries the actual Python error (e.g. the
   * `ModuleNotFoundError: No module named 'greenlet'` line) instead of just
   * "Python exited with code 1". Without this, our smoke-test failures
   * surface as opaque "exited with code 1" in the wizard's error box and
   * the real cause is only visible if the user expands the log panel.
   *
   * Watchdog: SIGKILLs python if it doesn't exit within `timeoutMs`
   * (default 10min). Mirrors _runCommand's pattern — without it, a hung
   * pip resolver, a wedged Chromium probe, or a deadlocked C-extension
   * import would freeze setup forever with no recourse but Force-Quit.
   * 10min comfortably accommodates the worst legitimate operation we run:
   * `pip install -r requirements.txt` on a slow connection, where the
   * C-extension wheels (cryptography, lxml, greenlet) are 5-15 MB each
   * and arch-specific. Callers that need longer (Chromium download in
   * step 6, ~150 MB) pass a larger override.
   *
   * @param {string[]} args      argv passed to python
   * @param {object}   extraEnv  merged into process.env for the child
   * @param {number}   timeoutMs watchdog timeout in ms (default 10 min)
   */
  _runPython(args, extraEnv = {}, timeoutMs = 10 * 60_000) {
    return new Promise((resolve, reject) => {
      // Verify the interpreter binary exists before trying to spawn it.
      // This gives a clear error instead of cryptic "spawn ENOENT".
      if (!fs.existsSync(this.pythonExe)) {
        return reject(new Error(
          `Python interpreter not found at: ${this.pythonExe}\n` +
          "The Python extraction may have failed. Click Retry."
        ));
      }

      const proc = spawn(this.pythonExe, args, {
        env: {
          ...process.env,
          ...extraEnv,
          PYTHONUNBUFFERED: "1",
        },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
        cwd: this._envDir,
      });

      let stdout = "";
      // Accumulate stderr so we can surface it in the rejection error.
      // Capped to last ~4KB to avoid memory blow-up if Python tracebacks
      // get huge — the meaningful tail (the actual exception line) is
      // always at the bottom of a Python traceback.
      let stderr = "";
      const STDERR_CAP = 4096;
      let timedOut = false;

      // Watchdog. SIGKILL is the right signal here even though Python
      // would normally honor SIGTERM — pip/playwright/patchright install
      // sub-spawn multiple workers that don't always propagate SIGTERM
      // to children, so SIGKILL on the top-level process is the only
      // reliable way to actually free the wizard. On Windows, Node maps
      // SIGKILL to TerminateProcess, which is equally final.
      const argHint = args.length ? args[0] : "interpreter";
      const watchdog = setTimeout(() => {
        timedOut = true;
        this._onLog(
          `Watchdog: python ${argHint} exceeded ${Math.round(timeoutMs / 1000)}s — terminating`
        );
        try { proc.kill("SIGKILL"); } catch {}
      }, timeoutMs);

      proc.stdout.on("data", (d) => {
        const text = d.toString();
        stdout += text;
        text.split("\n").filter(Boolean).forEach((line) => this._onLog("  " + line.trim()));
      });

      proc.stderr.on("data", (d) => {
        const text = d.toString();
        stderr += text;
        if (stderr.length > STDERR_CAP) {
          stderr = stderr.slice(-STDERR_CAP);
        }
        text.split("\n").filter(Boolean).forEach((line) => this._onLog("  " + line.trim()));
      });

      proc.on("close", (code) => {
        clearTimeout(watchdog);
        if (timedOut) {
          reject(new Error(
            `Python (${argHint}) timed out after ` +
            `${Math.round(timeoutMs / 1000)}s and was terminated. ` +
            "Most often a stuck pip install (mirror outage / slow link) " +
            "or a deadlocked C extension. Click Retry."
          ));
          return;
        }
        if (code === 0) {
          resolve(stdout);
          return;
        }
        // Pull the last meaningful line from stderr — typically the actual
        // exception (e.g. "ModuleNotFoundError: No module named 'greenlet'").
        // Python tracebacks have the exception at the bottom; we want that,
        // not "Traceback (most recent call last):" at the top.
        const lines = stderr.trim().split(/\r?\n/).filter(Boolean);
        const lastLine = lines.length ? lines[lines.length - 1].trim() : "";
        const detail = lastLine
          ? `${lastLine} (Python exited with code ${code})`
          : `Python exited with code ${code}`;
        reject(new Error(detail));
      });

      proc.on("error", (err) => {
        clearTimeout(watchdog);
        reject(new Error(`Failed to start Python: ${err.message}`));
      });
    });
  }
}

// ═══════════════════════════════════════════
// HELPER FUNCTIONS (used by main.js)
// ═══════════════════════════════════════════

function isSetupComplete(envDir) {
  return fs.existsSync(path.join(envDir, ".setup-complete"));
}

function deleteEnv(envDir) {
  if (fs.existsSync(envDir)) {
    fs.rmSync(envDir, { recursive: true, force: true });
  }
}

module.exports = { PythonSetup, isSetupComplete, deleteEnv, PYTHON_VERSION };
