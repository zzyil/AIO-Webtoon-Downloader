// ============================================================
// SEARCHER MODULE
//
// Spawns `aio-dl.py --search "<query>" --search-json` as a child
// process, accumulates stdout (the JSON candidate list), streams
// stderr lines to the React UI as live "search-log" events, and
// resolves with the parsed JSON when the process exits.
//
// Mirrors the spawn/stream pattern of downloader.js but for the
// search backend's blocking-with-stderr-progress contract:
//   - stdout = clean JSON, parsed on close
//   - stderr = `[*] Searching N sites...` / `[*] Probing image
//     quality...` / `--seeded-only: skipped K handler(s)` / etc.
//
// Search is blocking (typical 40-100s) — UI shows progress feed.
// ============================================================

const { spawn } = require("child_process");
// Shared line hygiene — same ANSI strip + Playwright-teardown noise drop the
// download subprocess uses (both crash via the same PipeTransport bridge on
// close). NOISY_LINE_RE + stripAnsi were byte-identical copies here before
// the dedup sweep; classifyLogLevel's success branch is search-specific,
// injected below via SEARCH_SUCCESS_RE.
const { NOISY_LINE_RE, stripAnsi, classifyLogLevel } = require("./log-filter");
// Tree kill (deviceSync P4): Python search spawns Playwright → Chromium, and a
// plain proc.kill() leaves those grandchildren running. grep proc-kill.
const { killTree, killTreeNow, trackChild } = require("./proc-kill");

// Search-stream "this line is a success" markers (green in the LogPanel):
// the auto-pick winner + the chapter-alignment summary. downloader.js
// supplies its own (Done./saved/etc.) set.
const SEARCH_SUCCESS_RE = /--auto-pick selected|alignment/i;

/**
 * Build CLI args from the UI's options object.
 * Mirrors downloader.js:buildCliArgs structure but for search flags.
 *
 * Booleans → flag-only when true, omitted when false.
 * Numbers/strings → flag + value, omitted when null/undefined/"".
 */
function buildSearchArgs(query, opts = {}) {
  const args = ["--search", query, "--search-json"];

  // Valued flags
  if (opts.searchLanguage) args.push("--search-language", String(opts.searchLanguage));
  if (opts.searchTimeout != null) args.push("--search-timeout", String(opts.searchTimeout));
  if (opts.searchMinMatch != null) args.push("--search-min-match", String(opts.searchMinMatch));
  if (opts.searchParallelism != null) args.push("--search-parallelism", String(opts.searchParallelism));
  if (opts.multiSourceQualityMin != null)
    args.push("--multi-source-quality-min", String(opts.multiSourceQualityMin));

  // Boolean toggles
  if (opts.seededOnly) args.push("--seeded-only");
  if (opts.multiSource) args.push("--multi-source");
  // collapseSplits defaults FALSE as of 2026-05-27 (opt-in flip); we emit
  // the positive --collapse-splits flag only when the user has explicitly
  // turned it on in Settings or the inline toggle. `=== true` is
  // intentional: undefined / null / false all mean "leave default OFF"
  // so older saved searchOpts dicts without the field stay off (the new
  // safer default). aio-dl.py's argparse keeps --no-collapse-splits as a
  // hidden alias for script users; we don't emit it.
  if (opts.collapseSplits === true) args.push("--collapse-splits");
  // No ML-rating flag: the opt-in ML image rating was removed with the
  // image-ranker rework (2026-10-21) and aio-dl.py no longer accepts
  // --enable-ml-rating. An old saved searchOpts.enableMlRating is simply
  // never read (this builder maps keys explicitly; nothing iterates opts).

  // User-disabled search sites (Settings → Search → "Search Sources", or the
  // SlowSitesCallout's "Disable & re-search"). Comma-joined handler names →
  // --disable-sites, which drops them from the fan-out AND the image-quality
  // probe (Python: aio_search_cli.parse_disable_sites → search_all
  // exclude_sites). useDownloader.runSearch injects opts.disabledSites from
  // settings.disabledSites on every search; the callout ALSO passes an explicit
  // list in opts to dodge the settings-ref update race on its immediate
  // re-search (opts wins in the hook's finalOpts merge). Array+length guarded,
  // mirroring the download-path emitter in downloader.js:buildCliArgs.
  if (Array.isArray(opts.disabledSites) && opts.disabledSites.length) {
    args.push("--disable-sites", opts.disabledSites.join(","));
  }

  return args;
}

class Searcher {
  constructor({ onLog, extraEnv }) {
    // Single callback for streaming stderr lines into the UI's log feed.
    // Result/error are returned by the runSearch() promise rather than via
    // separate events because search is blocking — there's no incremental
    // result to push.
    this._onLog = onLog;

    // Extra environment variables to pass to every search subprocess. In
    // packaged mode this carries PLAYWRIGHT_BROWSERS_PATH so the
    // Playwright-using handlers (comix, violetscans, rizzfables,
    // mangathemesia w/ use_playwright=True) can find the bundled
    // Chromium. Without this, those handlers throw at search()
    // and silently drop out of the candidate list. Same shape as
    // Downloader._extraEnv — see main.js:initDownloader.
    this._extraEnv = extraEnv || {};

    // The currently-running child process: one search at a time per
    // Searcher. The UI does NOT prevent a second runSearch while one runs
    // (a new query, a re-search, React StrictMode's double effect), so
    // runSearch cancels the previous process; the renderer ignores the
    // replaced call's answer (hooks/useSearch.js). Held so cancel() can kill it.
    this._proc = null;

    // Counter for log-correlation IDs (same shape as downloader's
    // downloadId). The UI uses this to scope stderr lines to the
    // current search invocation in the LogPanel.
    this._nextId = 1;

    // Processes this Searcher cancelled (deviceSync P4). Tracked per process,
    // not as one flag: runSearch's cancel-previous path starts a new process
    // while the old one is still dying, and the old one's late close must
    // still read as cancelled. Needed because a Windows taskkill /f ends the
    // child with code 1 and signal null, which the close handler would
    // otherwise report as "Search exited with code 1".
    this._cancelled = new WeakSet();
  }

  /**
   * Spawn the search subprocess. Returns a Promise that resolves with the
   * parsed JSON on clean exit (code=0 + valid JSON), or rejects with an
   * Error on spawn failure / non-zero exit / JSON parse failure.
   *
   * @param {object} cfg - { pythonCmd, scriptPath, workingDir, query, opts, searchId? }
   *   searchId: the id put on this search's log lines; minted here when absent.
   */
  runSearch({ pythonCmd, scriptPath, workingDir, query, opts, searchId: givenId }) {
    if (this._proc) {
      // A second search while one runs is normal (new query, re-search,
      // StrictMode): the newest wins, the prior process is cancelled and its
      // promise rejects with err.cancelled.
      this.cancel();
    }

    const mintedId = `srch_${this._nextId++}_${Date.now()}`;
    const searchId = typeof givenId === "string" && givenId ? givenId : mintedId;
    const cliArgs = ["-u", scriptPath, ...buildSearchArgs(query, opts)];
    const cmdString = `${pythonCmd} ${cliArgs.join(" ")}`;

    this._onLog?.(searchId, `$ ${cmdString}`, "info");

    return new Promise((resolve, reject) => {
      let proc;
      try {
        proc = spawn(pythonCmd, cliArgs, {
          cwd: workingDir,
          stdio: ["ignore", "pipe", "pipe"],
          windowsHide: true,
          // PYTHONUNBUFFERED so stderr progress lines appear live, not in
          // 4KB-buffered bursts (same fix as downloader.js). _extraEnv
          // carries PLAYWRIGHT_BROWSERS_PATH in packaged mode so handlers
          // that use Playwright (comix, violetscans, rizzfables, etc.)
          // can find the bundled Chromium. Same merge order as downloader.js.
          env: { ...process.env, ...this._extraEnv, PYTHONUNBUFFERED: "1" },
          // Own process group off Windows, so killTree's process.kill(-pid)
          // reaches Playwright's children too (deviceSync P4).
          detached: process.platform !== "win32",
        });
      } catch (err) {
        reject(new Error(`Failed to spawn search: ${err.message}`));
        return;
      }

      this._proc = proc;
      trackChild(proc);

      // Accumulate stdout — it's the JSON candidate list, parsed on close.
      // We don't try to parse mid-stream because aio-dl.py emits the JSON
      // as one final block via json.dumps() at the end of run_search_mode.
      const stdoutChunks = [];
      proc.stdout.on("data", (chunk) => {
        stdoutChunks.push(chunk);
      });

      // Stream stderr line-by-line into the log feed for live progress.
      // Buffer incomplete lines across data events the same way downloader.js
      // does — the search emits things like
      //   "[*] Searching 22 sites for 'Frieren'..."
      //   "[*] Probing image quality across 35 sources..."
      // mixed with browser-init noise from the Playwright-driven handlers.
      let stderrBuffer = "";
      proc.stderr.on("data", (chunk) => {
        stderrBuffer += chunk.toString("utf8");
        const lines = stderrBuffer.split(/\r?\n/);
        stderrBuffer = lines.pop() || "";
        for (const rawLine of lines) {
          if (!rawLine) continue;
          // Strip ANSI escapes once — Node-side Playwright driver wraps
          // values in SGR codes (\x1b[33m...\x1b[39m) and the regex
          // anchors (^\s*errno:, ^\s*syscall:) need the bare text.
          // stripAnsi fast-paths lines without escape bytes (the majority).
          const line = stripAnsi(rawLine);
          if (NOISY_LINE_RE.test(line)) continue;
          this._onLog?.(searchId, line, classifyLogLevel(line, SEARCH_SUCCESS_RE));
        }
      });

      proc.on("error", (err) => {
        if (this._proc === proc) this._proc = null;
        reject(new Error(`Search process error: ${err.message}`));
      });

      proc.on("close", (code, signal) => {
        if (this._proc === proc) this._proc = null;

        // Drain any final stderr line (no trailing newline edge case).
        // ANSI-strip + NOISY_LINE_RE applied here too so a Playwright
        // teardown line arriving without a terminating newline doesn't
        // slip past. Kept inline (not the stream loop's shape) — the tail
        // gate is `tail` truthiness, not a per-line skip.
        const tail = stripAnsi(stderrBuffer);
        if (tail && !NOISY_LINE_RE.test(tail)) {
          this._onLog?.(searchId, tail, classifyLogLevel(tail, SEARCH_SUCCESS_RE));
        }

        if (this._cancelled.has(proc) || signal === "SIGTERM" || signal === "SIGKILL") {
          // User-initiated cancel — communicate as a benign rejection so
          // the UI can distinguish from genuine errors.
          const err = new Error("Search cancelled");
          err.cancelled = true;
          reject(err);
          return;
        }

        if (code !== 0) {
          reject(new Error(`Search exited with code ${code}`));
          return;
        }

        // Parse stdout as JSON. The Python --search-json contract emits
        // a {"candidates": [...]} dict (empty list = legitimate "no
        // results") on EVERY successful exit. Empty stdout therefore
        // means the process exited cleanly without writing the contract,
        // i.e. crashed in cleanup or output went somewhere else. Surface
        // that as an error instead of silently presenting an empty
        // results page that hides the actual failure mode.
        const stdoutText = Buffer.concat(stdoutChunks).toString("utf8").trim();
        if (!stdoutText) {
          reject(
            new Error(
              "Search produced no JSON output. The Python process may have " +
                "exited cleanly without writing the --search-json contract " +
                "(common when a teardown error swallowed the stdout buffer). " +
                "Check the search log for any [!] lines.",
            ),
          );
          return;
        }
        try {
          const json = JSON.parse(stdoutText);
          resolve(json);
        } catch (err) {
          reject(
            new Error(
              `Search produced non-JSON stdout (${stdoutText.length}b). ` +
                `First 200 chars: ${stdoutText.slice(0, 200)}`,
            ),
          );
        }
      });
    });
  }

  /**
   * Best-effort cancel of the in-flight search. Tree-kills the child; the
   * `close` handler rejects with err.cancelled=true so the UI can
   * distinguish from real failures.
   *
   * Returns false when nothing is running, or when the child already exited
   * and only its close is pending: its PID may already be recycled, so no
   * taskkill is sent, and the close reports the natural result.
   */
  cancel() {
    const proc = this._proc;
    if (!proc || proc.exitCode !== null || proc.signalCode !== null) return false;
    this._cancelled.add(proc);
    killTree(proc).catch(() => {});
    return true;
  }

  /**
   * cancel(), then wait until the child's close event (≤ ~6 s). For callers
   * that must not proceed while the tree is alive (reinstall-python's
   * deleteEnv, app quit). Resolves {wasRunning}; never rejects.
   */
  async cancelAndWait({ timeoutMs = 5000 } = {}) {
    const proc = this._proc;
    if (!proc) return { wasRunning: false };
    if (proc.exitCode !== null || proc.signalCode !== null) {
      await trackChild(proc);
      return { wasRunning: false };
    }
    this._cancelled.add(proc);
    await killTree(proc, { timeoutMs });
    return { wasRunning: true };
  }

  /** Synchronous fire-and-forget tree kill for app.on("quit") (shutdownNow). */
  cancelNow() {
    const proc = this._proc;
    if (!proc) return false;
    this._cancelled.add(proc);
    return killTreeNow(proc);
  }

  /** Whether a search is currently running. */
  isRunning() {
    return this._proc !== null;
  }
}

module.exports = { Searcher };
