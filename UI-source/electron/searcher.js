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

// Stderr noise drop-list (mirrors downloader.js NOISY_LINE_RE — same
// Playwright teardown produces these on search subprocess close). Match
// before the line reaches the LogPanel so the user never sees them.
//
// Extended 2026-05-08 to cover the full Node.js crash dump (15-20 lines
// of JS stack trace + errno object + "Node.js vX.Y.Z" footer) that the
// original EPIPE-substring match missed entirely. See downloader.js for
// the per-pattern rationale; we keep these in sync because both processes
// crash via the same PipeTransport bridge.
const NOISY_LINE_RE = new RegExp(
  [
    String.raw`\b(EPIPE|BrokenPipeError|ConnectionResetError)\b`,
    String.raw`node:events:\d`,
    String.raw`node:internal\/(net|streams|process|timers|destroy)`,
    String.raw`playwright[\\/]driver`,
    String.raw`\bPipeTransport\b`,
    String.raw`\bDispatcherConnection\b`,
    String.raw`\bBrowserContextDispatcher\b`,
    String.raw`\bCRBrowserContext\b`,
    String.raw`^\s*throw\s+er\b`,
    String.raw`(Unhandled|Emitted) '\w+' event`,
    String.raw`^\s*errno:\s*-?\d+`,
    String.raw`^\s*syscall:\s*['"]`,
    String.raw`^Node\.js v\d`,
    String.raw`^\s*\^\s*$`,
    String.raw`^\s*[{}],?\s*$`,
  ].join("|"),
);

const ANSI_RE = /\x1b\[[0-9;]*m/g;

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
  // collapseSplits defaults TRUE; we only emit the negative flag when the
  // user has explicitly turned it off in Settings or the inline toggle.
  // `=== false` is intentional: undefined / null / true all mean "leave
  // default ON" so older saved searchOpts dicts without the field don't
  // accidentally disable collapse.
  if (opts.collapseSplits === false) args.push("--no-collapse-splits");
  // enableMlRating defaults FALSE (production default; matches argparse).
  // Only emit the flag when explicitly enabled. Wiring this through so
  // users can opt into torch-backed T2/T3 image-quality scoring (CLIP-IQA,
  // NIQE, paired DISTS). Off by default because torch's Windows import
  // can stall on WMI degraded states — see search_orchestrator.py's
  // _ML_RATING_ENABLED docstring for the full rationale.
  if (opts.enableMlRating) args.push("--enable-ml-rating");

  return args;
}

/**
 * Classify a stderr line for log-level coloring (same convention as
 * downloader.js — keeps the LogPanel rendering consistent). Patterns
 * stay STRICT (anchored, word-bound) so retry-style messages like
 * "First variant failed, trying 9 more" or "[Fallback] X failed" don't
 * paint red. Same fix as downloader.js (2026-05-07 log-noise bug).
 */
function classifyLogLevel(line) {
  const trimmed = line.trim();
  if (/^\[!\]/.test(trimmed)) return "error";
  if (/^Traceback /.test(trimmed)) return "error";
  if (/^\w*?Error:/.test(trimmed)) return "error";
  if (/\bFAILED\b/.test(line)) return "error";

  if (/Warning:|warning:|⚠/i.test(line)) return "warning";
  if (/--auto-pick selected|alignment/i.test(line)) return "success";
  if (/^\s{2,}/.test(line)) return "verbose";
  return "info";
}

class Searcher {
  constructor({ onLog, extraEnv }) {
    // Single callback for streaming stderr lines into the UI's log feed.
    // Result/error are returned by the runSearch() promise rather than via
    // separate events because search is blocking — there's no incremental
    // result to push.
    this._onLog = onLog;

    // Extra environment variables to pass to every search subprocess. In
    // packaged mode this carries PLAYWRIGHT_BROWSERS_PATH so MangaFire's
    // VRF bridge (and the other Playwright-using handlers — violetscans,
    // rizzfables, mangathemesia w/ use_playwright=True) can find the
    // bundled Chromium. Without this, those handlers throw at search()
    // and silently drop out of the candidate list. Same shape as
    // Downloader._extraEnv — see main.js:initDownloader.
    this._extraEnv = extraEnv || {};

    // The currently-running child process. Only one search at a time
    // (the UI prevents concurrent invocations via searchState.status).
    // Hold a reference so cancel() can SIGTERM it.
    this._proc = null;

    // Counter for log-correlation IDs (same shape as downloader's
    // downloadId). The UI uses this to scope stderr lines to the
    // current search invocation in the LogPanel.
    this._nextId = 1;
  }

  /**
   * Spawn the search subprocess. Returns a Promise that resolves with the
   * parsed JSON on clean exit (code=0 + valid JSON), or rejects with an
   * Error on spawn failure / non-zero exit / JSON parse failure.
   *
   * @param {object} cfg - { pythonCmd, scriptPath, workingDir, query, opts }
   */
  runSearch({ pythonCmd, scriptPath, workingDir, query, opts }) {
    if (this._proc) {
      // Defense-in-depth: UI shouldn't allow a second search while one is
      // running, but if it happens we cancel the prior before starting.
      this.cancel();
    }

    const searchId = `srch_${this._nextId++}_${Date.now()}`;
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
          // that use Playwright (mangafire, violetscans, rizzfables, etc.)
          // can find the bundled Chromium. Same merge order as downloader.js.
          env: { ...process.env, ...this._extraEnv, PYTHONUNBUFFERED: "1" },
        });
      } catch (err) {
        reject(new Error(`Failed to spawn search: ${err.message}`));
        return;
      }

      this._proc = proc;

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
      // mixed with VRF init noise from MangaFire's Playwright bridge.
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
          // Fast-path lines without escape bytes (the majority) so we
          // skip the regex scan and string allocation on plain text.
          const line = rawLine.includes("\x1b") ? rawLine.replace(ANSI_RE, "") : rawLine;
          if (NOISY_LINE_RE.test(line)) continue;
          this._onLog?.(searchId, line, classifyLogLevel(line));
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
        // slip past.
        const tail = stderrBuffer.includes("\x1b") ? stderrBuffer.replace(ANSI_RE, "") : stderrBuffer;
        if (tail && !NOISY_LINE_RE.test(tail)) {
          this._onLog?.(searchId, tail, classifyLogLevel(tail));
        }

        if (signal === "SIGTERM" || signal === "SIGKILL") {
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
   * Best-effort cancel of the in-flight search. SIGTERM the child; the
   * `close` handler resolves with err.cancelled=true so the UI can
   * distinguish from real failures.
   */
  cancel() {
    if (!this._proc) return false;
    try {
      this._proc.kill("SIGTERM");
    } catch {
      /* swallow — process may already be dead */
    }
    return true;
  }

  /** Whether a search is currently running. */
  isRunning() {
    return this._proc !== null;
  }
}

module.exports = { Searcher };
