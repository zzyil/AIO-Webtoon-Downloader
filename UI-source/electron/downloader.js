// ============================================================
// DOWNLOADER MODULE
//
// This handles spawning the Python aio-dl.py process,
// reading its output line by line, parsing progress info,
// and sending updates back to the React UI.
// ============================================================

const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

// Drop-list for stdout/stderr noise we can't usefully surface. Playwright's
// BrowserContext teardown races aio-dl.py's chapter loop and produces:
//   - "Error: write EPIPE" / Python "BrokenPipeError" / "ConnectionResetError"
//     on simple bridge closes, AND
//   - a full Node.js crash dump (15-20 lines of JS stack trace + errno
//     object + "Node.js vX.Y.Z" footer) when the PipeTransport hits a
//     write-after-shutdown on Windows (errno -4047 = libuv UV_ESHUTDOWN).
// None of this is actionable — the chapter either succeeded (download
// already finished) or will be retried by the missed-chapter pass.
//
// First-pass filter only matched the literal EPIPE/BrokenPipeError/
// ConnectionResetError substrings; the full Node crash dump contains NONE
// of those, so this extended pattern covers every line of the format:
//   - JavaScript stack frames with `at Func (node:internal/...)`
//   - Playwright driver paths / dispatcher class names (PipeTransport,
//     DispatcherConnection, BrowserContextDispatcher, CRBrowserContext)
//   - Crash markers: `Unhandled '<event>' event`, `Emitted '<event>' event`,
//     the literal `throw er;` line and its `^` caret pointer
//   - The `{ errno: ..., syscall: '...' }` object dump
//   - The trailing `Node.js vX.Y.Z` footer
//
// Duplicated in searcher.js since the search subprocess hits the same
// Playwright bridge.
const NOISY_LINE_RE = new RegExp(
  [
    // Original EPIPE-family literal substrings (still useful for the
    // single-line "Error: write EPIPE" path).
    String.raw`\b(EPIPE|BrokenPipeError|ConnectionResetError)\b`,
    // Node.js internal stack frame paths.
    String.raw`node:events:\d`,
    String.raw`node:internal\/(net|streams|process|timers|destroy)`,
    // Playwright driver internals.
    String.raw`playwright[\\/]driver`,
    String.raw`\bPipeTransport\b`,
    String.raw`\bDispatcherConnection\b`,
    String.raw`\bBrowserContextDispatcher\b`,
    String.raw`\bCRBrowserContext\b`,
    // Node crash format markers.
    String.raw`^\s*throw\s+er\b`,
    String.raw`(Unhandled|Emitted) '\w+' event`,
    String.raw`^\s*errno:\s*-?\d+`,
    String.raw`^\s*syscall:\s*['"]`,
    String.raw`^Node\.js v\d`,
    // Lone caret pointer + lone brace-dump open/close lines.
    String.raw`^\s*\^\s*$`,
    String.raw`^\s*[{}],?\s*$`,
  ].join("|"),
);

// Strip ANSI color escape sequences before testing against NOISY_LINE_RE
// (Node's Playwright driver emits SGR codes like \x1b[90m...\x1b[39m, so
// `^\s*errno:` won't match a line that's actually `\x1b[90m errno: \x1b[39m`
// without the strip first). Stripping also yields cleaner LogPanel display.
const ANSI_RE = /\x1b\[[0-9;]*m/g;

/**
 * Builds an array of CLI arguments from the UI's args object.
 *
 * For example, if the UI sends:
 *   { format: "pdf", quality: 100, keepChapters: true, verbose: true }
 *
 * This function produces:
 *   ["--format", "pdf", "--quality", "100", "--keep-chapters", "--verbose"]
 *
 * Boolean "true" values become flags (no value after them).
 * Boolean "false" values are skipped entirely.
 * null/undefined/"" values are skipped.
 */
function buildCliArgs(args) {
  const cliArgs = [];

  // ── Map of UI arg names → CLI flag names ──
  // The left side is what React sends, the right side is what aio-dl.py expects
  const flagMap = {
    format: "--format",
    epubLayout: "--epub-layout",
    quality: "--quality",
    scaling: "--scaling",
    width: "--width",
    aspectRatio: "--aspect-ratio",
    chapters: "--chapters",
    language: "--language",
    split: "--split",
    site: "--site",
    cookies: "--cookies",
    group: "--group",
    jobs: "--jobs",
    imageWorkers: "--image-workers",
    httpTimeout: "--http-timeout",
    httpMaxRetries: "--http-max-retries",
    httpBackoffBase: "--http-backoff-base",
    httpBackoffCap: "--http-backoff-cap",
    netMinGap: "--net-min-gap",
    multiSourceQualityMin: "--multi-source-quality-min",
    multiSourcePrefetched: "--multi-source-prefetched",
    prefetchImageWorkers: "--prefetch-image-workers",
    // Fast-download knobs (2026-05-13: generalized from MangaFire-only).
    // Apply to any handler with SUPPORTS_FAST_DOWNLOAD=True
    // (currently mangafire and linewebtoon; see sites/base.py for the
    // implementation and aio-dl.py's argparse for full help text).
    imageConcurrency: "--image-concurrency",
    imagePrefetchDepth: "--image-prefetch-depth",
    imagePrefetchParallel: "--image-prefetch-parallel",
    // MangaFire VRF capture flags (--mangafire-vrf-prefetch-depth and
    // --mangafire-vrf-parallel) intentionally NOT in flagMap. They were
    // removed from the Settings UI on 2026-05-13 — argparse defaults
    // are good for most users; advanced users pass them on the CLI
    // directly. Keeping them out of flagMap means useDownloader.js
    // can't accidentally emit them when settings dicts are spread.
    missedRetries: "--missed-retries",
    missedLog: "--missed-log",
    jobStallTimeout: "--job-stall-timeout",
    jobHardTimeout: "--job-hard-timeout",
    jobRetries: "--job-retries",
    jobSpawnGap: "--job-spawn-gap",
    coordDir: "--coord-dir",
  };

  // ── Boolean flags (no value, just present or absent) ──
  const boolMap = {
    keepChapters: "--keep-chapters",
    noFinalFile: "--no-final-file",
    keepImages: "--keep-images",
    multiSource: "--multi-source",
    noProcessing: "--no-processing",
    noCleanup: "--no-cleanup",
    noPartials: "--no-partials",
    mixByUpvote: "--mix-by-upvote",
    verbose: "--verbose",
    debug: "--debug",
    noRetryMissedChapters: "--no-retry-missed-chapters",
    restoreParameters: "--restore-parameters",
    promptUrls: "--prompt-urls",
    // Curated-sites toggle for the multi-source-direct-URL fan-out.
    // Mirror of searcher.js:70's opts.seededOnly handling — keeping the
    // flag name symmetric (camelCase here, kebab-case CLI) means
    // useDownloader.queueDownload's settings injection works without
    // a separate translation step. aio-dl.py:3589 defines --seeded-only;
    // it's read inside find_alternatives_for_direct_url at line 4432.
    seededOnly: "--seeded-only",
    // LINE Webtoon WebP recompression master toggle (Phase 1, 2026-05-11).
    // Python-side gates the actual encode pass on handler.name match, so
    // emitting this flag for non-webtoons.com downloads is a safe no-op.
    // The valued companion knobs (quality, method) are NOT in flagMap —
    // they're handled below the loops so we can suppress them when the
    // master toggle is off (avoids noisy `--webtoon-recompress-quality 85`
    // on every spawn just because settings.defaults carry the value).
    webtoonRecompress: "--webtoon-recompress",
    // Komikku-compatible per-chapter CBZ output (2026-05-12, komikkuspec.md).
    // Python-side force-coerces --format cbz / --keep-chapters /
    // --no-final-file when this is set, so the UI's format selector is
    // effectively ignored for komikku downloads. Output stays at
    // <workingDir>/mangas/<Series>/ (user syncs that into their phone's
    // <Komikku-SAF>/local/ themselves). Search/library-initiated downloads
    // pick this up via App.jsx's settings.defaults spread.
    komikku: "--komikku",
    // Escape hatch for curl_cffi fast download path (2026-05-13). When the
    // user toggles this on in Settings, all handlers fall back to the
    // legacy ThreadPoolExecutor + dl_image cloudscraper path regardless
    // of their per-handler SUPPORTS_FAST_DOWNLOAD flag. Useful for
    // curl_cffi version bugs or CDN-vs-impersonation issues.
    noFastDownload: "--no-fast-download",
  };

  // Add valued arguments
  for (const [key, flag] of Object.entries(flagMap)) {
    const value = args[key];
    // Skip empty/null/undefined values, and skip "all" for chapters (it's the default)
    if (value === null || value === undefined || value === "") continue;
    if (key === "chapters" && value === "all") continue;

    cliArgs.push(flag, String(value));
  }

  // Add boolean flags
  for (const [key, flag] of Object.entries(boolMap)) {
    if (args[key] === true) {
      cliArgs.push(flag);
    }
  }

  // Phase F (2026-05-07): negative-default flag for the CBZ byte-preserving
  // fast-path. Default-on at the Python side; we only emit the negative
  // form when the user has explicitly turned the Settings switch off. The
  // `=== false` test means undefined / null / true all leave default ON,
  // so older saved settings dicts that don't have the field don't
  // accidentally disable it. Same shape as searcher.js:buildSearchArgs's
  // collapseSplits handling.
  if (args.cbzPreserveOriginals === false) {
    cliArgs.push("--no-cbz-preserve-originals");
  }

  // Global collapse-splits toggle (item 8 in snappy-forging-waffle.md).
  // Same negative-default pattern as cbzPreserveOriginals: aio-dl.py defaults
  // to collapse=True, we emit --no-collapse-splits only when the user has it
  // explicitly off. useDownloader.queueDownload injects the field from
  // settings.collapseSplits before calling startDownload.
  if (args.collapseSplits === false) {
    cliArgs.push("--no-collapse-splits");
  }

  // LINE Webtoon recompression valued knobs (Phase 1, 2026-05-11). Only
  // emit when the master toggle is on AND the value differs from the
  // Python-side argparse default (85 for quality, 4 for method). Without
  // this gate, every spawn carrying settings.defaults would inject both
  // flags regardless of whether the recompress pass actually runs, which
  // is noisy (it'd show up on the `$ python aio-dl.py ...` log line in
  // the LogPanel for non-webtoons.com downloads too). Python ignores the
  // valued flags when --webtoon-recompress is absent — they're argparse
  // metadata for the helper function, gated independently in
  // _process_chapter_impl — so this is purely about spawn-line cleanliness.
  if (args.webtoonRecompress === true) {
    if (args.webtoonRecompressQuality != null && args.webtoonRecompressQuality !== 85) {
      cliArgs.push("--webtoon-recompress-quality", String(args.webtoonRecompressQuality));
    }
    if (args.webtoonRecompressMethod != null && args.webtoonRecompressMethod !== 4) {
      cliArgs.push("--webtoon-recompress-method", String(args.webtoonRecompressMethod));
    }
  }

  return cliArgs;
}

/**
 * Parses a log line from aio-dl.py to extract progress information.
 *
 * ACTUAL output patterns from the Python script:
 *   "Toradora! (hid=6oz)"                          → title + ID
 *   "Chapter 8 (already processed, collecting files)" → cached chapter (resume)
 *   "Chapter 8 (No Group)"                          → downloading new chapter
 *   "Filtered list down to 45 chapters."            → total count (verbose)
 *   "Selected 45 chapters."                         → total count (verbose)
 *   "Fetching 12 media item(s)..."                  → images in current chapter
 *   "Building final file..."                        → building phase
 *   "PDF saved → filename.pdf"                      → output saved
 *   "EPUB saved → filename.epub"                    → output saved
 *   "CBZ saved → filename.cbz"                      → output saved
 *   "Done."                                         → finished
 *   "Parameters match. Resuming download."           → resume mode
 *   "Missed 3 chapter(s). Retrying..."              → missed chapters
 *   "[+] Recovered chapter 5"                       → recovered
 *   "Completed (2/5): url"                          → batch progress
 *   "--- Timing Summary ---"                        → timing (near end)
 */
function parseProgressLine(line) {
  const progress = {};

  // ── Title and ID: "Toradora! (hid=6oz)" ──
  // This is printed once at the start. The hid is at the very end.
  const titleMatch = line.match(/^(.+?)\s+\(hid=([^)]+)\)\s*$/);
  if (titleMatch) {
    progress.title = titleMatch[1].trim();
    progress.hid = titleMatch[2];
  }

  // ── Total chapter count (from verbose filtering output) ──
  // "Filtered list down to 45 chapters."
  // "Selected 45 chapters."
  // "--no-partials: Filtered out 3 partial chapters."
  const totalMatch = line.match(/(?:down to|Selected)\s+(\d+)\s+chapter/i);
  if (totalMatch) {
    progress.totalChapters = parseInt(totalMatch[1], 10);
  }

  // ── Chapter being processed ──
  // "Chapter 8 (already processed, collecting files)" → cached/resumed
  // "Chapter 8 (No Group)" or "Chapter 8 (SomeScan)" → downloading
  // "Chapter 8.5 (Official)" → partial chapter
  // These lines start with \n in the script, so after splitting they start with "Chapter"
  const chapterMatch = line.match(/^Chapter\s+(\d+(?:[.~]\d+)?)\s+\((.+?)\)/);
  if (chapterMatch) {
    progress.currentChapter = parseFloat(chapterMatch[1].replace("~", "."));
    // "already processed" means it's a cached/resumed chapter
    if (/already processed/i.test(chapterMatch[2])) {
      progress.phase = "downloading"; // still in the download phase, just skipping
      progress.chapterCached = true;
    } else {
      progress.phase = "downloading";
      progress.chapterCached = false;
    }
    // Increment the chapter counter (the UI tracks this)
    progress.chapterTick = true;
  }

  // ── Resume detection ──
  if (/Parameters match\.\s*Resuming download/i.test(line)) {
    progress.phase = "resuming";
  }

  // ── Fetching images within a chapter ──
  const fetchMatch = line.match(/Fetching\s+(\d+)\s+media item/i);
  if (fetchMatch) {
    progress.imagesInChapter = parseInt(fetchMatch[1], 10);
  }

  // ── Building final file ──
  if (/Building final file/i.test(line)) {
    progress.phase = "building";
  }

  // ── Building final PDF from chapters ──
  if (/Building final PDF from\s+(\d+)\s+chapter/i.test(line)) {
    progress.phase = "building";
  }

  // ── Output saved ──
  if (/(?:PDF|EPUB|CBZ) saved\s*→/i.test(line)) {
    progress.phase = "saving";
    // Extract the filename after the arrow
    const saveMatch = line.match(/saved\s*→\s*(.+)/);
    if (saveMatch) progress.savedFile = saveMatch[1].trim();
  }

  // ── Chapter PDF saved (per-chapter output) ──
  if (/PDF Chapter saved\s*→/i.test(line)) {
    progress.chapterSaved = true;
  }

  // ── Missed chapters ──
  const missedMatch = line.match(/Missed\s+(\d+)\s+chapter/i);
  if (missedMatch) {
    progress.phase = "retrying";
    progress.missedCount = parseInt(missedMatch[1], 10);
  }

  // ── Recovered chapter ──
  if (/\[.\]\s*Recovered chapter/i.test(line)) {
    progress.recovered = true;
  }

  // ── Still missed (final) ──
  if (/Still missed\s+(\d+)\s+chapter/i.test(line)) {
    const stillMissed = line.match(/Still missed\s+(\d+)/);
    if (stillMissed) progress.stillMissed = parseInt(stillMissed[1], 10);
  }

  // ── Timing summary (means we're almost done) ──
  if (/---\s*Timing Summary\s*---/.test(line)) {
    progress.phase = "finishing";
  }

  // ── Done ──
  if (/^Done\./.test(line.trim())) {
    progress.phase = "done";
  }

  // ── Batch mode progress ──
  // "Completed (2/5): https://..."
  const batchMatch = line.match(/Completed\s+\((\d+)\/(\d+)\)/);
  if (batchMatch) {
    progress.batchCurrent = parseInt(batchMatch[1], 10);
    progress.batchTotal = parseInt(batchMatch[2], 10);
  }

  // ── VRF phase ──
  if (/(?:VRF|vrf).*(?:captur|generat|ensur|browser|playwright)/i.test(line)) {
    progress.phase = "vrf";
  }

  // ── Errors ──
  if (/\[!\]|Error:|Traceback|FAILED/i.test(line)) {
    progress.hasError = true;
  }

  return progress;
}

/**
 * Determines the "level" of a log line for color-coding in the UI.
 *   - "error"   → red text
 *   - "warning" → yellow text
 *   - "success" → green text
 *   - "info"    → default text
 *   - "verbose" → dimmed/gray text
 *
 * The error patterns are deliberately STRICT to avoid coloring informational
 * retry-style messages red. The previous rule `/error:|FAILED/i` matched the
 * substring "failed" anywhere (case-insensitive) which painted hundreds of
 * benign lines red — e.g., "First variant failed, trying 9 more" or
 * "[Fallback] X.jpg: first URL failed, trying...". Both are normal retry
 * paths, not errors.
 *
 * The canonical error marker in aio-dl.py is `[!]` prefix (13 occurrences
 * across the codebase). Python tracebacks start with "Traceback ". Python
 * exception chains print as "ExceptionName: message" at line start.
 */
function classifyLogLevel(line) {
  const trimmed = line.trim();
  // [!] is the codebase convention for genuine errors (anchored to the
  // start of the trimmed line so it doesn't fire on "[!?] not really").
  if (/^\[!\]/.test(trimmed)) return "error";
  // Python crash header — always indicates an unhandled exception above.
  if (/^Traceback /.test(trimmed)) return "error";
  // Python exception line: "ImportError: foo", "ValueError: bar", or just
  // "Error: bar". Non-greedy \w*? lets the leading prefix vary while
  // requiring "Error:" as the trailing token.
  if (/^\w*?Error:/.test(trimmed)) return "error";
  // Uppercase FAILED as a word boundary — matches CI-style "X tests FAILED"
  // but NOT "First variant failed" (lowercase). aio-dl.py doesn't emit
  // FAILED uppercase currently; this is forward-compat for tooling that does.
  if (/\bFAILED\b/.test(line)) return "error";

  if (/Warning:|warning:|⚠/i.test(line)) return "warning";
  if (/Done\.|saved →|✓|Completed|recovered/i.test(line)) return "success";
  if (/^\s{2,}/.test(line)) return "verbose"; // Indented lines are usually verbose detail
  return "info";
}

class Downloader {
  constructor({ onLog, onProgress, onComplete, extraEnv }) {
    // These callbacks send data back to the Electron main process,
    // which forwards them to the React UI
    this._onLog = onLog;
    this._onProgress = onProgress;
    this._onComplete = onComplete;

    // Extra environment variables to pass to every Python process.
    // Used in packaged mode to set PLAYWRIGHT_BROWSERS_PATH so the
    // bundled Playwright can find its Chromium installation.
    this._extraEnv = extraEnv || {};

    // Track all running processes by their download ID
    this._processes = new Map();

    // Simple incrementing counter for download IDs
    this._nextId = 1;
  }

  /**
   * Start a new download by spawning the Python process.
   * Returns a downloadId that the UI uses to track this specific download.
   */
  start({ pythonCmd, scriptPath, workingDir, url, args }) {
    const downloadId = `dl_${this._nextId++}_${Date.now()}`;

    // Fix B (2026-05-07): when SearchSourceCard.handleDownload bundles
    // prefetched alternatives into args.prefetchedAlts, persist the JSON to
    // a known cache path BEFORE buildCliArgs runs. We then inject the path
    // back as args.multiSourcePrefetched so the existing flagMap entry picks
    // it up and emits --multi-source-prefetched <path>. Failure to write
    // is non-fatal — we drop the prefetched data and aio-dl.py runs the
    // search path as before. The temp file is unlinked in _spawn's close
    // handler so we don't accumulate stale session files in ~/.aio-dl/cache.
    let prefetchedTempPath = null;
    let downloadArgs = args;
    if (args && args.prefetchedAlts) {
      try {
        prefetchedTempPath = this._writePrefetchedAlts(downloadId, args.prefetchedAlts);
        const { prefetchedAlts, ...rest } = args;
        downloadArgs = { ...rest, multiSourcePrefetched: prefetchedTempPath };
      } catch (err) {
        this._onLog(
          downloadId,
          `[!] Failed to write prefetched alts: ${err.message}; falling back to search`,
          "warning",
        );
        const { prefetchedAlts, ...rest } = args;
        downloadArgs = rest;
      }
    }

    // Build the full argument list for aio-dl.py
    // The -u flag tells Python to run with unbuffered stdout/stderr.
    // Without it, Python buffers output in ~4KB chunks when running
    // as a child process, so log lines arrive in delayed bursts.
    const cliArgs = ["-u", scriptPath, ...buildCliArgs(downloadArgs)];

    // The URL(s) go at the end of the argument list
    if (Array.isArray(url)) {
      cliArgs.push(...url);
    } else {
      cliArgs.push(url);
    }

    // Log the command we're about to run (helpful for debugging)
    const cmdString = `${pythonCmd} ${cliArgs.join(" ")}`;
    this._onLog(downloadId, `$ ${cmdString}`, "info");

    this._spawn(downloadId, pythonCmd, cliArgs, workingDir, {
      url,
      args: downloadArgs,
      prefetchedTempPath,
    });

    return downloadId;
  }

  /**
   * Persist the search-tab's prefetched-alts payload to a JSON file.
   * Lives under ~/.aio-dl/cache/ms_prefetched_<downloadId>.json (same dir
   * the Python side uses for img_quality.json + probe_failures.json, so
   * the project keeps one cache root). Returns the absolute path.
   */
  _writePrefetchedAlts(downloadId, payload) {
    const os = require("os");
    const cacheDir = path.join(os.homedir(), ".aio-dl", "cache");
    fs.mkdirSync(cacheDir, { recursive: true });
    const filePath = path.join(cacheDir, `ms_prefetched_${downloadId}.json`);
    fs.writeFileSync(filePath, JSON.stringify(payload, null, 2), "utf8");
    return filePath;
  }

  /**
   * Resume a download using --restore-parameters.
   * This tells aio-dl.py to read settings from the existing tmp folder.
   *
   * The --format flag is kept SEPARATE from restored params by the Python script,
   * so you can change the output format when resuming (e.g. switch from PDF to EPUB).
   *
   * @param {string} format - Output format to use (pdf/epub/cbz/none). If provided,
   *   overrides whatever format was used in the original download.
   * @param {string} epubLayout - EPUB layout (vertical/page), only used when format is epub.
   */
  resume({ pythonCmd, scriptPath, workingDir, url, tmpDir, format, epubLayout }) {
    const downloadId = `dl_${this._nextId++}_${Date.now()}`;

    // If no format specified, read the original from disk. Try run_meta.json
    // FIRST (aio-dl.py writes format there on every run, intentionally) and
    // only fall back to run_params.json for legacy tmp folders. See
    // aio-dl.py:get_behavior_params — format is deliberately omitted from
    // run_params so the user can pick a new format on resume; the canonical
    // record of the original format lives in run_meta.json.
    if (!format) {
      const tryRead = (filename) => {
        try {
          const p = path.join(tmpDir, filename);
          if (!fs.existsSync(p)) return null;
          const data = JSON.parse(fs.readFileSync(p, "utf8"));
          return data.format || null;
        } catch {
          return null;
        }
      };
      format =
        tryRead("run_meta.json") ||
        tryRead("ui_meta.json") ||  // Electron-side metadata (downloader.js writes this when run_meta isn't yet present)
        tryRead("run_params.json") ||
        "pdf";
    }

    // -u flag: unbuffered Python output (same as start())
    const cliArgs = ["-u", scriptPath, "--restore-parameters", "--format", format];

    // Add EPUB layout if format is epub
    if (format === "epub" && epubLayout) {
      cliArgs.push("--epub-layout", epubLayout);
    }

    cliArgs.push("--verbose", url);

    const cmdString = `${pythonCmd} ${cliArgs.join(" ")}`;
    this._onLog(downloadId, `$ ${cmdString}`, "info");

    this._spawn(downloadId, pythonCmd, cliArgs, workingDir, { url, resumed: true });

    return downloadId;
  }

  /**
   * Internal: actually spawn the child process and wire up stdout/stderr.
   */
  _spawn(downloadId, pythonCmd, cliArgs, workingDir, meta) {
    // spawn() creates a new process. We pipe stdout and stderr so we
    // can read them line by line.
    const proc = spawn(pythonCmd, cliArgs, {
      cwd: workingDir,
      // "pipe" means we get proc.stdout and proc.stderr as readable streams
      stdio: ["ignore", "pipe", "pipe"],
      // On Windows, this makes the process run in its own group so we can
      // kill it cleanly without also killing Electron
      windowsHide: true,
      // CRITICAL: Force Python to use unbuffered output.
      // Without this, Python pipes use 4-8KB buffering, which means
      // log lines pile up and arrive in big delayed chunks instead
      // of appearing instantly line-by-line.
      // Also merge any extra env vars (e.g. PLAYWRIGHT_BROWSERS_PATH
      // for bundled Playwright in packaged mode).
      env: { ...process.env, ...this._extraEnv, PYTHONUNBUFFERED: "1" },
    });

    // Store the process so we can cancel it later
    const entry = {
      process: proc,
      meta,
      startTime: Date.now(),
      // Accumulated progress data (gets updated as we parse log lines)
      progress: { phase: "starting", title: "", totalChapters: 0, currentChapter: 0 },
      // Running counter: how many "Chapter N" lines we've seen so far
      processedChapters: 0,
    };
    // Promise that resolves when this child process finally exits — both
    // the close and error handlers below resolve it. Used by cancelAll() so
    // the main process can wait for orphaned children to actually die before
    // app.quit() rather than fire-and-forget taskkill (which is async on
    // Windows; Python often outlives Electron by 1-3 seconds without a wait).
    let _resolveClose;
    entry.closePromise = new Promise((resolve) => { _resolveClose = resolve; });
    entry._resolveClose = _resolveClose;
    this._processes.set(downloadId, entry);

    // Buffer for incomplete lines (stdout comes in chunks, not always full lines)
    let stdoutBuffer = "";
    let stderrBuffer = "";

    // Read stdout line by line
    proc.stdout.on("data", (chunk) => {
      stdoutBuffer += chunk.toString("utf8");
      // Split on newlines and process each complete line
      const lines = stdoutBuffer.split(/\r?\n/);
      // The last element might be an incomplete line, keep it in the buffer
      stdoutBuffer = lines.pop() || "";

      for (const rawLine of lines) {
        if (!rawLine) continue;
        // Strip ANSI escapes once and reuse for both the noise filter and
        // the LogPanel render — keeps user-visible logs free of `[90m...`
        // garbage from the Node-side Playwright driver. Fast-path the
        // common case (most lines have no ANSI) by skipping the regex
        // scan entirely when the literal escape byte isn't present.
        const line = rawLine.includes("\x1b") ? rawLine.replace(ANSI_RE, "") : rawLine;
        if (NOISY_LINE_RE.test(line)) continue;
        const level = classifyLogLevel(line);
        this._onLog(downloadId, line, level);

        // Try to extract progress information from this line
        const progressUpdate = parseProgressLine(line);
        if (Object.keys(progressUpdate).length > 0) {
          // If this line represents a new chapter being processed,
          // increment the counter (used for progress bar)
          if (progressUpdate.chapterTick) {
            entry.processedChapters++;
            progressUpdate.processedChapters = entry.processedChapters;
          }

          // When we detect the hid (from "Title (hid=xxx)"), save the URL
          // and other metadata to ui_meta.json in the tmp folder.
          // This means resume always has the URL available, even if
          // download history is cleared.
          if (progressUpdate.hid && !entry._metaSaved) {
            entry._metaSaved = true;
            const tmpDir = path.join(workingDir, `tmp_${progressUpdate.hid}`);
            const metaPath = path.join(tmpDir, "ui_meta.json");
            try {
              // Only write if the tmp folder exists (created by aio-dl.py)
              if (fs.existsSync(tmpDir)) {
                const metaData = {
                  url: Array.isArray(meta.url) ? meta.url[0] : meta.url,
                  title: progressUpdate.title || "",
                  format: meta.args?.format || "pdf",
                  startedAt: entry.startTime,
                };
                fs.writeFileSync(metaPath, JSON.stringify(metaData, null, 2));
              }
            } catch {
              // Non-critical — resume will still work via history lookup
            }
          }

          Object.assign(entry.progress, progressUpdate);
          this._onProgress(downloadId, { ...entry.progress });
        }
      }
    });

    // Read stderr — historically auto-classified as "error", but with the
    // mangafire stderr-print shim (sites/mangafire.py module-level print
    // override) most stderr lines are actually informational VRF / progress
    // output, NOT errors. Run through classifyLogLevel so genuine errors
    // (Traceback, [!], Python ExceptionName:) still color red while
    // informational stderr stays at info/verbose. Bug surfaced 2026-05-07
    // when the user reported the Logs panel was a wall of red.
    proc.stderr.on("data", (chunk) => {
      stderrBuffer += chunk.toString("utf8");
      const lines = stderrBuffer.split(/\r?\n/);
      stderrBuffer = lines.pop() || "";

      for (const rawLine of lines) {
        if (!rawLine) continue;
        const line = rawLine.includes("\x1b") ? rawLine.replace(ANSI_RE, "") : rawLine;
        if (NOISY_LINE_RE.test(line)) continue;
        const level = classifyLogLevel(line);
        this._onLog(downloadId, line, level);
      }
    });

    // When the process exits
    proc.on("close", (code) => {
      // Flush any remaining data in buffers (NOISY_LINE_RE filter applied
      // here too — a Playwright EPIPE arriving on the trailing line without
      // a terminating newline would otherwise slip past the streaming filter).
      // ANSI-strip first for the same reason the streaming handlers do.
      // Tail bytes are short (single trailing line, if any), so the includes
      // fast-path is mostly cosmetic here but stays consistent with above.
      const tailStdout = stdoutBuffer.includes("\x1b") ? stdoutBuffer.replace(ANSI_RE, "") : stdoutBuffer;
      const tailStderr = stderrBuffer.includes("\x1b") ? stderrBuffer.replace(ANSI_RE, "") : stderrBuffer;
      if (tailStdout.trim() && !NOISY_LINE_RE.test(tailStdout)) {
        this._onLog(downloadId, tailStdout, classifyLogLevel(tailStdout));
      }
      if (tailStderr.trim() && !NOISY_LINE_RE.test(tailStderr)) {
        // Same classification as the streaming stderr handler above —
        // a trailing line without a final newline shouldn't auto-error.
        this._onLog(downloadId, tailStderr, classifyLogLevel(tailStderr));
      }

      // Fix B: clean up the prefetched-alts temp file. aio-dl.py reads it
      // once during multi-source setup, so it's safe to delete on close.
      // Best-effort — losing one stale file isn't worth crashing the spawn
      // tracker over. _writePrefetchedAlts created it under ~/.aio-dl/cache.
      if (meta.prefetchedTempPath) {
        try {
          fs.unlinkSync(meta.prefetchedTempPath);
        } catch {
          // File may have been removed externally or never created
        }
      }

      this._processes.delete(downloadId);

      const result = {
        exitCode: code,
        status: code === 0 ? "completed" : "failed",
        duration: Date.now() - entry.startTime,
        ...entry.progress,
      };
      this._onComplete(downloadId, result);
      // Unblock cancelAll() awaiters now that the child is really gone.
      entry._resolveClose();
    });

    // Handle spawn errors (e.g. python not found)
    proc.on("error", (err) => {
      this._onLog(downloadId, `Process error: ${err.message}`, "error");
      // Same cleanup as the close handler — spawn errored before close
      // would fire, so the temp file would otherwise leak.
      if (meta.prefetchedTempPath) {
        try {
          fs.unlinkSync(meta.prefetchedTempPath);
        } catch {}
      }
      this._processes.delete(downloadId);
      this._onComplete(downloadId, {
        exitCode: -1,
        status: "error",
        error: err.message,
        duration: Date.now() - entry.startTime,
      });
      // Spawn errored before any close event will fire — resolve here too,
      // otherwise cancelAll() would wait the full 5s timeout for a child
      // that never actually started.
      entry._resolveClose();
    });
  }

  /**
   * Kill a running download process.
   *
   * Returns a Promise that resolves when the underlying child has actually
   * exited (close event fired). Callers that don't care can fire-and-forget
   * — the IPC handler in main.js does this. Callers that DO care, like
   * cancelAll() during app shutdown, await it so the process tree is gone
   * before the next step (app.quit) runs.
   */
  cancel(downloadId) {
    const entry = this._processes.get(downloadId);
    if (!entry) return Promise.resolve();

    this._onLog(downloadId, "Cancelling download...", "warning");

    try {
      // On Windows, we need to kill the entire process tree
      // because Python might have spawned child processes (like Playwright)
      if (process.platform === "win32") {
        spawn("taskkill", ["/pid", String(entry.process.pid), "/f", "/t"], {
          windowsHide: true,
        });
      } else {
        entry.process.kill("SIGTERM");
      }
    } catch (err) {
      // Process might have already exited
    }
    return entry.closePromise;
  }

  /**
   * Kill all running downloads (called when the app quits).
   *
   * Awaits the process trees actually dying, with a 5s upper bound so a
   * stuck taskkill (e.g. an AV scanner blocking) doesn't trap quit forever.
   * Without the wait, the previous fire-and-forget loop let Python children
   * outlive Electron, which kept tmp_<hid>/ lockfiles held briefly across
   * an immediate relaunch.
   */
  cancelAll() {
    const pending = [];
    for (const id of this._processes.keys()) {
      pending.push(this.cancel(id));
    }
    if (pending.length === 0) return Promise.resolve();
    const QUIT_TIMEOUT_MS = 5000;
    return Promise.race([
      Promise.all(pending),
      new Promise((resolve) => setTimeout(resolve, QUIT_TIMEOUT_MS)),
    ]);
  }

  /**
   * Scan the working directory for tmp_* folders that contain
   * run_params.json — these are resumable downloads.
   *
   * Also reads run_meta.json (written by aio-dl.py) and ui_meta.json
   * (written by Electron) for URL, title, and format info.
   *
   * Returns an array of objects with info about each one.
   */
  scanResumable(workingDir) {
    const results = [];

    try {
      const entries = fs.readdirSync(workingDir, { withFileTypes: true });

      for (const entry of entries) {
        // Only look at folders named tmp_<something>
        if (!entry.isDirectory() || !entry.name.startsWith("tmp_")) continue;

        const tmpPath = path.join(workingDir, entry.name);
        const paramsPath = path.join(tmpPath, "run_params.json");

        // Must have run_params.json to be resumable
        if (!fs.existsSync(paramsPath)) continue;

        try {
          const params = JSON.parse(fs.readFileSync(paramsPath, "utf8"));

          // Read metadata files for URL, title, format
          // Priority: run_meta.json (from Python) > ui_meta.json (from Electron)
          let meta = {};
          const runMetaPath = path.join(tmpPath, "run_meta.json");
          const uiMetaPath = path.join(tmpPath, "ui_meta.json");
          try {
            if (fs.existsSync(runMetaPath)) {
              meta = JSON.parse(fs.readFileSync(runMetaPath, "utf8"));
            } else if (fs.existsSync(uiMetaPath)) {
              meta = JSON.parse(fs.readFileSync(uiMetaPath, "utf8"));
            }
          } catch {
            // Non-critical — URL/title will fall back to history lookup
          }

          // Count how many chapters are already done.
          // The Python script creates marker files inside each ch_* subdirectory:
          //   .processed_complete — when processing (resize/quality) was done
          //   .download_complete  — when --no-processing is used (raw images)
          let cachedChapters = 0;
          const tmpEntries = fs.readdirSync(tmpPath, { withFileTypes: true });
          for (const sub of tmpEntries) {
            if (sub.isDirectory() && sub.name.startsWith("ch_")) {
              const chPath = path.join(tmpPath, sub.name);
              if (
                fs.existsSync(path.join(chPath, ".processed_complete")) ||
                fs.existsSync(path.join(chPath, ".download_complete"))
              ) {
                cachedChapters++;
              }
            }
          }

          // Extract the hid from the folder name: tmp_<hid> → <hid>
          const hid = entry.name.replace(/^tmp_/, "");

          results.push({
            hid,
            tmpDir: tmpPath,
            params,
            cachedChapters,
            folderName: entry.name,
            // From metadata files (may be empty if neither exists)
            url: meta.url || null,
            title: meta.title || null,
            format: meta.format || params.format || null,
          });
        } catch {
          // Skip folders with unreadable params
        }
      }
    } catch (err) {
      console.error("Error scanning for resumable downloads:", err);
    }

    return results;
  }
}

module.exports = { Downloader };
