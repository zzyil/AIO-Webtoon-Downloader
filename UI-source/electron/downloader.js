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
// Shared line hygiene (ANSI strip + Playwright-teardown noise drop +
// severity classify). NOISY_LINE_RE and stripAnsi were byte-identical
// copies in searcher.js before the dedup sweep; classifyLogLevel's
// success branch is caller-specific, injected below via DOWNLOAD_SUCCESS_RE.
const { NOISY_LINE_RE, stripAnsi, classifyLogLevel } = require("./log-filter");

// Download-stream "this line is a success" markers (green in the LogPanel).
// Passed to the shared classifyLogLevel; searcher.js supplies its own set.
const DOWNLOAD_SUCCESS_RE = /Done\.|saved →|✓|Completed|recovered/i;

// ── Per-chapter ETA estimator ──
// EMA weight for a fresh chapter-duration sample. 0.3 tracks a real slowdown
// (CDN throttle, a chapter with 3x the pages) within a couple of chapters
// without letting one outlier swing the readout.
const ETA_EMA_ALPHA = 0.3;
// Publish nothing until a SECOND sample confirms the first. One sample off a
// cold start (DNS, Cloudflare solve, browser launch) is badly unrepresentative
// and would show a number the very next tick contradicts.
const ETA_MIN_SAMPLES = 2;

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
    // Scanlation-group policy. `mtl` is a choice flag (avoid|allow|exclude,
    // default avoid) and excludeGroup takes the same comma-separated string
    // shape as `group` — aio-dl.py splits both on comma after parse.
    mtl: "--mtl",
    excludeGroup: "--exclude-group",
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
    // CPU-pool budget (Settings → Resource Limits → Max CPU usage). main.js
    // resolves settings.cpuLimit → args.maxCpuPercent and sets it ONLY when
    // < 100, so default (Unlimited) spawns never carry the flag. Python side:
    // aio-dl.py:_cpu_pool_budget scales the modernize/webp/encode/decode pools.
    maxCpuPercent: "--max-cpu-percent",
    // Back-compat: kept alongside imageConcurrency so saved settings dicts
    // that still carry mangafireImageConcurrency (pre-2026-05-13) keep
    // working. aio-dl.py installs a deprecation shim that routes
    // --mangafire-image-concurrency to args.image_concurrency. We never
    // emit BOTH on the same spawn — useDownloader.queueDownload only
    // injects one key from settings.defaults (it migrated to
    // imageConcurrency at the same time).
    mangafireImageConcurrency: "--mangafire-image-concurrency",
    missedRetries: "--missed-retries",
    missedLog: "--missed-log",
    jobStallTimeout: "--job-stall-timeout",
    jobHardTimeout: "--job-hard-timeout",
    jobRetries: "--job-retries",
    jobSpawnGap: "--job-spawn-gap",
    coordDir: "--coord-dir",
    // External metadata enrichment (--metadata-source family).
    // Surfaced as top-level settings in SettingsTab → Metadata Enrichment;
    // useDownloader.queueDownload only injects metadataSource when the user
    // turned the master toggle on (i.e. value != "none"), so the natural
    // "skip empty/null/undefined" filter below already keeps the
    // default-off spawn clean. metadataTagMinRank is similarly only
    // injected when it differs from the Python argparse default (50).
    // Python side: aio-dl.py near --enable-ml-rating + sites/external_metadata.py.
    metadataSource: "--metadata-source",
    metadataTagMinRank: "--metadata-tag-min-rank",
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
    // multiSourceLazy is deliberately NOT here — it's default-ON whenever
    // multi-source is on (absent-means-on), which boolMap's `=== true`
    // test can't express. See the dedicated chokepoint below the loops.
    // LINE Webtoon WebP recompression master toggle (Phase 1, 2026-05-11).
    // Python-side gates the actual encode pass on handler.name match, so
    // emitting this flag for non-webtoons.com downloads is a safe no-op.
    // The valued companion knobs (quality, method) are NOT in flagMap —
    // they're handled below the loops so we can suppress them when the
    // master toggle is off (avoids noisy `--webtoon-recompress-quality 85`
    // on every spawn just because settings.defaults carry the value).
    webtoonRecompress: "--webtoon-recompress",
    // Komikku-compatible per-chapter CBZ output (2026-05-12, Komikku LocalSource format).
    // Python-side force-coerces --format cbz / --keep-chapters /
    // --no-final-file when this is set, so the UI's format selector is
    // effectively ignored for komikku downloads. Output stays at
    // <workingDir>/manga/<Series>/ (user syncs that into their phone's
    // <Komikku-SAF>/local/ themselves). Search/library-initiated downloads
    // pick this up via App.jsx's settings.defaults spread.
    komikku: "--komikku",
    // Content-aware JXL/AVIF transcode (--modernize, CBZ-only). Master toggle
    // only; the valued knobs (--modernize-format / -distance / -quality /
    // -min-saving) are emitted below the loops so they only appear when
    // modernize is on AND the fast-path is satisfiable AND the value differs
    // from the Python default (mirrors the webtoon-recompress knob gating).
    // aio-dl.py hard-errors on --modernize with ANY fast-path-disabling flag,
    // so the modernizeBlocked guard below strips the whole family when the
    // effective args can't ride the CBZ byte-passthrough fast-path. Python
    // side: grep '--modernize compatibility checks' + recompress_chapter_images_modern.
    modernize: "--modernize",
    // Escape hatch for curl_cffi fast download path (2026-05-13). When the
    // user toggles this on in Settings, all handlers fall back to the
    // legacy ThreadPoolExecutor + dl_image cloudscraper path regardless
    // of their per-handler SUPPORTS_FAST_DOWNLOAD flag. Useful for
    // curl_cffi version bugs or CDN-vs-impersonation issues.
    noFastDownload: "--no-fast-download",
    // External metadata force-refresh — companion to the valued
    // metadataSource flag above. Only injected by queueDownload when
    // metadataSource !== "none" AND the user explicitly turned the
    // refresh switch on in Settings → Metadata Enrichment. Python-side
    // bypasses the cached-AniList-ID fast path and re-runs the fuzzy
    // title-match search on every download. See sites/external_metadata.py.
    metadataRefresh: "--metadata-refresh",
  };

  // Add valued arguments
  for (const [key, flag] of Object.entries(flagMap)) {
    const value = args[key];
    // Skip empty/null/undefined values, and skip "all" for chapters (it's the default)
    if (value === null || value === undefined || value === "") continue;
    if (key === "chapters" && value === "all") continue;
    // "avoid" is aio-dl.py's --mtl default; emitting it would add a flag to
    // every single spawn for no behavior change (and churn the saved
    // download_params.json diff on every run).
    if (key === "mtl" && value === "avoid") continue;

    cliArgs.push(flag, String(value));
  }

  // --webtoon-recompress requires an archive output. aio-dl.py hard-errors on
  // --format pdf/none for it; --komikku coerces format→cbz BEFORE that check
  // (~aio-dl.py:5999 then ~6028), so it stays valid then. This buildCliArgs is
  // the single chokepoint for every spawn path (manual, search-, library-
  // initiated, resume), so strip the flag + its valued knobs here whenever the
  // effective format is incompatible — defense-in-depth behind the
  // DownloadTab / SettingsTab UI guards, covering any path that didn't clear it.
  const recompressIncompatible =
    (args.format === "none" || args.format === "pdf") && args.komikku !== true;

  // --modernize rides the CBZ byte-passthrough fast-path and aio-dl.py rejects
  // it with a HARD error on any fast-path-disabling flag (grep '--modernize
  // compatibility checks' — the seven conditions). This buildCliArgs is the
  // single chokepoint for every spawn path, and the search/library paths
  // spread raw settings.defaults (which can carry modernize:true alongside a
  // conflicting quality/scaling/preserve), so strip the whole --modernize*
  // family here whenever the effective args can't satisfy the fast-path —
  // defense-in-depth behind the SettingsTab/DownloadTab UI guards. komikku
  // coerces format→cbz BEFORE the Python check, so it keeps modernize valid
  // regardless of the format value (stricter than webtoon: cbz only, NOT epub).
  const modernizeBlocked =
    (args.format !== "cbz" && args.komikku !== true) ||
    (args.quality != null && Number(args.quality) < 100) ||
    (args.scaling != null && Number(args.scaling) < 100) ||
    args.cbzPreserveOriginals === false ||
    args.noProcessing === true ||
    (args.width != null && args.width !== "") ||
    (args.aspectRatio != null && args.aspectRatio !== "");

  // Add boolean flags
  for (const [key, flag] of Object.entries(boolMap)) {
    if (args[key] === true) {
      if (key === "webtoonRecompress" && recompressIncompatible) continue;
      if (key === "modernize" && modernizeBlocked) continue;
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

  // --multi-source-lazy: opt-out nested inside the multi-source opt-in
  // (2026-07-02). Default-ON for every multi-source download — defer the
  // ~30-80 s cross-site alternatives discovery until a chapter actually
  // fails (aio-dl.py's _ms_lazy_pending hook in _process_chapter_strict).
  // Absent-means-on like cbzPreserveOriginals above: library/search spawn
  // paths spread saved settings.defaults, and dicts saved before the
  // multiSourceLazy field existed must not silently revert to the eager
  // discovery — only an explicit false (user unticked the nested toggle in
  // Settings → Default Multi-source Fallback or the New tab) suppresses
  // the flag. Gated on multiSource so defaults-spread paths without
  // multi-source keep a clean spawn line (the flag would be a Python-side
  // no-op anyway). Prefetched search downloads emit it too — harmless,
  // Python's lazy arming excludes --multi-source-prefetched mode.
  if (args.multiSource === true && args.multiSourceLazy !== false) {
    cliArgs.push("--multi-source-lazy");
  }

  // Global collapse-splits toggle (item 8 in snappy-forging-waffle.md,
  // updated 2026-05-27 for the opt-in flip). aio-dl.py now defaults
  // collapse=False; we emit the positive --collapse-splits flag only
  // when the user has explicitly turned it on. useDownloader.queueDownload
  // injects the field from settings.collapseSplits before calling
  // startDownload. The deprecated --no-collapse-splits is still a hidden
  // alias on the Python side for any script users — we just don't emit it.
  if (args.collapseSplits === true) {
    cliArgs.push("--collapse-splits");
  }

  // User-disabled sites also drop out as MULTI-SOURCE download alternatives —
  // not just from search (the user's "search + downloads" scope decision). Same
  // chokepoint shape as collapseSplits above: comma-joined handler names →
  // --disable-sites. useDownloader.queueDownload injects args.disabledSites from
  // settings.disabledSites on EVERY spawn (manual / search / library / queue),
  // so a disabled site is skipped by find_alternatives_for_direct_url and the
  // guard-filter of _multi_source_alternatives in aio-dl.py (which also scrubs
  // it from any prefetched/disk-cached alt list). Harmless no-op on
  // single-source downloads — Python only consults the exclusion under
  // --multi-source. Array+length guarded, mirroring searcher.js:buildSearchArgs.
  if (Array.isArray(args.disabledSites) && args.disabledSites.length) {
    cliArgs.push("--disable-sites", args.disabledSites.join(","));
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
  if (args.webtoonRecompress === true && !recompressIncompatible) {
    if (args.webtoonRecompressQuality != null && args.webtoonRecompressQuality !== 85) {
      cliArgs.push("--webtoon-recompress-quality", String(args.webtoonRecompressQuality));
    }
    if (args.webtoonRecompressMethod != null && args.webtoonRecompressMethod !== 4) {
      cliArgs.push("--webtoon-recompress-method", String(args.webtoonRecompressMethod));
    }
  }

  // --modernize valued knobs. Same gating shape as the webtoon knobs above:
  // emit only when the master toggle is on, the fast-path is satisfiable
  // (!modernizeBlocked), and the value differs from aio-dl.py's argparse
  // default — keeps the spawn line clean (Python applies the defaults when a
  // flag is absent). Defaults: format=auto, distance=1.0, quality=90,
  // min-saving=0.92 (grep the '--modernize-*' add_argument calls in aio-dl.py).
  if (args.modernize === true && !modernizeBlocked) {
    if (args.modernizeReversible === true) {
      // Fully-reversible archival preset (SettingsTab's modernizeReversible —
      // UI-level, NO dedicated Python flag): force the PAIR format=jxl +
      // distance=0 and ignore the stored routing/distance/AVIF knobs. A PAIR
      // because `auto` + distance 0 is NOT reversible — auto still routes
      // color pages to the AVIF branch, which is lossy at any setting. At
      // distance 0 the Python JXL save runs bit-exact JPEG->JXL
      // reconstruction (djxl recovers the original .jpg byte-for-byte) and
      // pixel-lossless PNG (aio-dl.py: grep is_recon / lossless_jpeg).
      // Resolved here — the single spawn chokepoint — so search/library/
      // UpdatesCenter paths that spread settings.defaults get the same
      // guarantee as the New tab.
      cliArgs.push("--modernize-format", "jxl");
      cliArgs.push("--modernize-distance", "0");
    } else {
      if (args.modernizeFormat != null && args.modernizeFormat !== "auto") {
        cliArgs.push("--modernize-format", String(args.modernizeFormat));
      }
      if (args.modernizeDistance != null && Number(args.modernizeDistance) !== 1.0) {
        cliArgs.push("--modernize-distance", String(args.modernizeDistance));
      }
      if (args.modernizeQuality != null && Number(args.modernizeQuality) !== 90) {
        cliArgs.push("--modernize-quality", String(args.modernizeQuality));
      }
      // AVIF speed only matters when the AVIF branch can run — i.e. not under
      // the reversible preset's forced jxl-only routing. Note speed=0 is a
      // valid non-default (slowest/smallest), so the !== 6 test emits it.
      if (args.modernizeAvifSpeed != null && Number(args.modernizeAvifSpeed) !== 6) {
        cliArgs.push("--modernize-avif-speed", String(args.modernizeAvifSpeed));
      }
    }
    // min-saving + JXL effort apply on both paths: effort is a pure CPU<->size
    // knob (applies to lossless encodes too), and min-saving still guards the
    // PNG-pixel-lossless tier — JPEG reconstructions are guard-EXEMPT on the
    // Python side (adopted whenever smaller at all; grep is_recon).
    if (args.modernizeMinSaving != null && Number(args.modernizeMinSaving) !== 0.92) {
      cliArgs.push("--modernize-min-saving", String(args.modernizeMinSaving));
    }
    if (args.modernizeEffort != null && Number(args.modernizeEffort) !== 7) {
      cliArgs.push("--modernize-effort", String(args.modernizeEffort));
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

  // ── Errors ──
  if (/\[!\]|Error:|Traceback|FAILED/i.test(line)) {
    progress.hasError = true;
  }

  return progress;
}

/**
 * Fold one "Chapter N (…)" tick into the entry's rolling per-chapter EMA and
 * stamp etaMs / chapterMsEma / etaSamples onto the outgoing progress payload.
 *
 * Measured HERE, in the main process, and not in the renderer: useDownloader's
 * 100ms flush coalesces bursts (it keeps only the latest progress per download
 * between ticks), so renderer-side deltas would silently merge two chapters
 * into one sample. This rides the existing download-progress payload — no new
 * IPC channel — and merges through the renderer's shallow progress spread.
 *
 * ONLY intervals whose OPENING chapter was actually downloaded are sampled.
 * On a resume the first N ticks are "already processed, collecting files" and
 * take ~0 ms; averaging those in yields a wildly optimistic ETA that then
 * stalls the moment real work starts. Excluding them OVER-estimates while the
 * cached prefix drains — the honest failure direction — and the UI shows
 * "Resuming cached chapters…" for that window instead of a number.
 *
 * totalChapters comes from the verbose-gated "Selected N chapters." /
 * "Filtered list down to N chapters." lines. queueDownload injects verbose and
 * Downloader.resume hardcodes --verbose, so it is present in practice; when it
 * isn't, etaMs is emitted as null and QueueTab keeps its indeterminate bar.
 *
 * Reads entry.progress.totalChapters (the value from a PREVIOUS line) —
 * _spawn's Object.assign of progressUpdate happens after this call, and a
 * chapter line never carries a total anyway.
 */
function applyChapterEta(entry, progressUpdate) {
  const now = Date.now();
  if (entry._lastTickAt && !entry._lastTickCached) {
    const sample = now - entry._lastTickAt;
    entry._chapterMsEma = entry._etaSamples
      ? entry._chapterMsEma + ETA_EMA_ALPHA * (sample - entry._chapterMsEma)
      : sample;
    entry._etaSamples++;
  }
  entry._lastTickAt = now;
  entry._lastTickCached = progressUpdate.chapterCached === true;

  if (entry._etaSamples < ETA_MIN_SAMPLES) return;

  const total = entry.progress.totalChapters || 0;
  const remaining = Math.max(0, total - entry.processedChapters);
  progressUpdate.chapterMsEma = Math.round(entry._chapterMsEma);
  progressUpdate.etaSamples = entry._etaSamples;
  // Explicit null (not "omit") when the total is unknown: the renderer merges
  // progress shallowly, so omitting the key would leave a stale ETA on screen.
  progressUpdate.etaMs = total > 0 ? Math.round(remaining * entry._chapterMsEma) : null;
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
      // Strip prefetchedAlts once; on success re-add it as the temp-file path.
      const { prefetchedAlts, ...rest } = args;
      downloadArgs = rest;
      try {
        prefetchedTempPath = this._writePrefetchedAlts(downloadId, prefetchedAlts);
        downloadArgs = { ...rest, multiSourcePrefetched: prefetchedTempPath };
      } catch (err) {
        this._onLog(
          downloadId,
          `[!] Failed to write prefetched alts: ${err.message}; falling back to search`,
          "warning",
        );
        // downloadArgs already = rest (prefetchedAlts stripped) — run search path.
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
  resume({ pythonCmd, scriptPath, workingDir, url, tmpDir, format, epubLayout, resourceFlags }) {
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

    // Resource-limit throttle (Settings → Resource Limits). resourceFlags is
    // resolved from the CURRENT settings by main.js (resumeThrottleFlags) — NOT
    // restored from run_params.json — so a resume in a different environment
    // (slower link, busy machine) honors the user's current limit. These are
    // explicit resume-CLI dests, so aio-dl.py's --restore-parameters keeps them
    // over the saved values (grep _user_set_dests) and none are resume-gating.
    if (Array.isArray(resourceFlags) && resourceFlags.length) {
      cliArgs.push(...resourceFlags);
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
      // ETA estimator state — see applyChapterEta above. _lastTickCached is
      // the OPENING chapter of the interval we're about to close, which is
      // what decides whether that interval counts as a sample.
      _lastTickAt: 0,
      _lastTickCached: false,
      _chapterMsEma: 0,
      _etaSamples: 0,
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

    // Per-line pipeline shared by the stdout + stderr STREAM handlers: skip
    // empty, strip ANSI (fast-pathed for the no-escape majority), drop
    // Playwright-teardown noise, classify severity, forward to the LogPanel.
    // Returns the cleaned line when emitted, or null when skipped — the stdout
    // handler runs progress parsing only on survivors. NOTE: the tail-flush in
    // the close handler is deliberately NOT routed through here — it suppresses
    // whitespace/ANSI-only tails via a post-strip `.trim()` guard, whereas the
    // stream keeps the historical `!rawLine`-only skip (a spaces-only line
    // still emits as verbose). Unifying the two would change that edge.
    const emit = (rawLine) => {
      if (!rawLine) return null;
      const line = stripAnsi(rawLine);
      if (NOISY_LINE_RE.test(line)) return null;
      this._onLog(downloadId, line, classifyLogLevel(line, DOWNLOAD_SUCCESS_RE));
      return line;
    };

    // Best-effort delete of the prefetched-alts temp file (Fix B). aio-dl.py
    // reads it once during multi-source setup, so it's safe to remove once
    // the child settles — from BOTH the close handler (normal exit) and the
    // error handler (spawn failed before close would fire, else the file
    // leaks). No-op when this run had no prefetched payload.
    // _writePrefetchedAlts created it under ~/.aio-dl/cache.
    const finalize = () => {
      if (meta.prefetchedTempPath) {
        try {
          fs.unlinkSync(meta.prefetchedTempPath);
        } catch {
          // File may have been removed externally or never created
        }
      }
    };

    // Read stdout line by line
    proc.stdout.on("data", (chunk) => {
      stdoutBuffer += chunk.toString("utf8");
      // Split on newlines and process each complete line
      const lines = stdoutBuffer.split(/\r?\n/);
      // The last element might be an incomplete line, keep it in the buffer
      stdoutBuffer = lines.pop() || "";

      for (const rawLine of lines) {
        const line = emit(rawLine);
        if (line === null) continue;

        // Try to extract progress information from this line
        const progressUpdate = parseProgressLine(line);
        if (Object.keys(progressUpdate).length > 0) {
          // If this line represents a new chapter being processed,
          // increment the counter (used for progress bar)
          if (progressUpdate.chapterTick) {
            entry.processedChapters++;
            progressUpdate.processedChapters = entry.processedChapters;
            // Must run AFTER the counter bump — the ETA is computed off
            // (totalChapters - processedChapters).
            applyChapterEta(entry, progressUpdate);
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
        emit(rawLine);
      }
    });

    // When the process exits
    proc.on("close", (code) => {
      // Flush any remaining data in buffers (NOISY_LINE_RE filter applied
      // here too — a Playwright EPIPE arriving on the trailing line without
      // a terminating newline would otherwise slip past the streaming filter).
      // ANSI-strip first for the same reason the streaming handlers do.
      // Tail bytes are short (single trailing line, if any). This stays inline
      // rather than reusing emit() because the tail suppresses whitespace/
      // ANSI-only content via the post-strip `.trim()` guard (emit only skips
      // a falsy raw line) — see the emit() note above.
      const tailStdout = stripAnsi(stdoutBuffer);
      const tailStderr = stripAnsi(stderrBuffer);
      if (tailStdout.trim() && !NOISY_LINE_RE.test(tailStdout)) {
        this._onLog(downloadId, tailStdout, classifyLogLevel(tailStdout, DOWNLOAD_SUCCESS_RE));
      }
      if (tailStderr.trim() && !NOISY_LINE_RE.test(tailStderr)) {
        // Same classification as the streaming stderr handler above —
        // a trailing line without a final newline shouldn't auto-error.
        this._onLog(downloadId, tailStderr, classifyLogLevel(tailStderr, DOWNLOAD_SUCCESS_RE));
      }

      // Clean up the prefetched-alts temp file (best-effort; see finalize).
      finalize();

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
      finalize();
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
   * How many child processes are alive right now. _processes is private and
   * self-maintaining (the close/error handlers delete their own entry), so
   * this is the authoritative "is anything running" answer.
   *
   * Consumed by main.js's mainWindow "close" listener to decide whether to
   * prompt before quitting — grep confirm-quit.
   */
  runningCount() {
    return this._processes.size;
  }

  /**
   * Identifying details for each live download, for the quit-confirmation
   * dialog's "these are still running" list. `title` is empty until the
   * "Title (hid=…)" line lands, so the renderer falls back to the URL.
   */
  getRunning() {
    const out = [];
    for (const [downloadId, entry] of this._processes) {
      const url = Array.isArray(entry.meta?.url) ? entry.meta.url[0] : entry.meta?.url;
      out.push({
        downloadId,
        title: entry.progress?.title || "",
        url: url || "",
        startedAt: entry.startTime,
      });
    }
    return out;
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
