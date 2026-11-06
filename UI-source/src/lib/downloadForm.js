// ============================================================
// New-tab download form: defaults, sparse edits, and the args builder.
//
// Owns the pure half of components/DownloadTab.jsx so the SAME code runs in
// two places:
//   - DownloadTab, to render the form;
//   - lib/queueRecipe.js (kind "newTab"), which rebuilds a waiting item's CLI
//     args from the settings saved when that item STARTS, not when it was queued.
//
// THE EDITS MODEL. The form's state is a SPARSE object holding only the fields
// the user changed (`edits`). What the form shows is
// `{...formDefaults(settings), ...edits}`, so any field the user didn't change
// shows, and downloads with, the current Settings value. Keeping only the edits
// is also what defines "explicitly changed" for the queue: a waiting New-tab
// item pins exactly its edits and follows Settings for everything else
// (user rule 2026-10-17). applyEdits drops a key whose new value equals the
// current default, so toggling a field back counts as "not changed".
//
// Relative imports only: tools/_test_queue_recipe.js bundles this file with
// esbuild outside Vite.
// ============================================================

import { KOMIKKU_VOLUMES_MESSAGE } from "./downloadArgs.js";

// ── DEFAULT VALUES ──
// Match aio-dl.py's defaults so you can start a basic download
// without changing anything.
//
// NOTE on quality=100 vs aio-dl.py's argparse default of 85:
// Phase G4 in aio-dl.py reads `_user_set_quality` from sys.argv; any explicit
// --quality < 100 disables the CBZ byte-preserving fast-path. buildNewTabArgs
// always emits --quality from form state, so a default of 85 would force every
// CBZ download into the slow decode/re-encode path. Keep this at 100 unless you
// also revisit Phase G4. Direct CLI users still get aio-dl.py's argparse
// default of 85 — the divergence is intentional.
export const DEFAULT_FORM = {
  urls: "",
  format: "pdf",
  epubLayout: "vertical",
  quality: 100,
  scaling: 100,
  width: "",
  aspectRatio: "",
  chapters: "all",
  language: "en",
  noPartials: false,
  keepChapters: false,
  noFinalFile: false,
  keepImages: false,
  noProcessing: false,
  noCleanup: false,
  splitMode: "none",
  splitValue: "",
  group: "",
  excludeGroup: "",
  // "avoid" mirrors aio-dl.py's --mtl default: machine-translated versions
  // rank below every human one but are still used when they're the only
  // version of a chapter.
  mtl: "avoid",
  mixByUpvote: false,
  cookies: "",
  jobs: 1,
  imageWorkers: 3,
  httpTimeout: 30,
  httpMaxRetries: 6,
  httpBackoffBase: 1.0,
  httpBackoffCap: 45,
  netMinGap: 0.25,
  missedRetries: 2,
  noRetryMissedChapters: false,
  // Multi-source fallback (also exposed in Search tab; applies here for
  // direct-URL downloads). When enabled, aio-dl.py pre-fetches chapter
  // lists from cross-site alternatives so per-chapter download failures
  // can fall over to another source instead of aborting. Quality floor
  // gates which alternatives qualify (mirror of --multi-source-quality-min).
  multiSource: false,
  multiSourceQualityMin: 0.65,
  // Lazy-discovery modifier (--multi-source-lazy, 2026-07-02): defer the
  // cross-site discovery until a chapter fails. Opt-out nested in the
  // multi-source opt-in — DownloadTab's multiSource switch resets it to true
  // on enable, and consumers treat absent as on (`!== false`) so settings
  // dicts saved before the field existed stay lazy. buildNewTabArgs forwards
  // only an explicit false; downloader.js's chokepoint emits the flag
  // whenever multiSource is on and this isn't false.
  multiSourceLazy: true,
  // CBZ byte-preservation (Phase F, 2026-05-07). True = use original wire
  // bytes when --format cbz with --scaling 100 and no --width/--quality
  // override. False emits --no-cbz-preserve-originals and forces the
  // legacy decode/recombine/re-encode path. Settings tab provides the
  // global default; the New tab has no per-job control for it.
  cbzPreserveOriginals: true,
  // LINE Webtoon WebP recompression (Phase 1, 2026-05-11). Re-encodes
  // each downloaded page from PNG/JPEG to lossy WebP at the chosen quality
  // before packaging. Only fires on webtoons.com (Python checks
  // handler.name === "linewebtoon"); silently no-ops elsewhere.
  webtoonRecompress: false,
  webtoonRecompressQuality: 85,
  webtoonRecompressMethod: 4,
  // Content-aware JXL/AVIF transcode (--modernize, CBZ-only). No New-tab
  // control: these come from settings.defaults via formDefaults().
  // buildNewTabArgs emits them when modernizeAllowed; downloader.js
  // :buildCliArgs maps modernize* → --modernize* and strips the family if the
  // CBZ fast-path is disabled. aio-dl.py: grep '--modernize compatibility checks'.
  modernize: false,
  // Fully-reversible archival preset — resolved at downloader.js:buildCliArgs
  // into the forced PAIR --modernize-format jxl + --modernize-distance 0
  // (auto + distance 0 is NOT reversible: color pages would still take the
  // always-lossy AVIF branch). SettingsTab owns the toggle.
  modernizeReversible: false,
  modernizeFormat: "auto",
  modernizeQuality: 90,
  modernizeDistance: 1.0,
  modernizeMinSaving: 0.92,
  // CPU<->size encoder knobs (no pixel change). INVERSE axes: higher JXL effort
  // = slower/smaller; higher AVIF speed = faster/larger. Defaults = sweet spot.
  modernizeEffort: 7,           // JXL effort 1-9 → --modernize-effort
  modernizeAvifSpeed: 6,        // AVIF speed 0-10 → --modernize-avif-speed
  // Komikku-compatible per-chapter CBZ output (2026-05-12, Komikku LocalSource format).
  // When on, Python force-coerces format=cbz / keep-chapters / no-final-file,
  // writes per-chapter ComicInfo.xml inside each CBZ, plus cover.jpg +
  // details.json at the series-folder root.
  komikku: false,
  // Chapters | Volumes switch (DownloadTab). JOB DATA, not a setting: it is
  // only valid for a URL on a site with native volumes, so it never belongs
  // in settings.defaults (SettingsTab has no control for it, and
  // formDefaults below forces it back to false whatever a saved dict holds).
  downloadVolumes: false,
};

// What an untouched New tab shows: the hardcoded form defaults under the
// user's saved download defaults (SettingsTab owns those).
export function formDefaults(settings) {
  // downloadVolumes is job data (see DEFAULT_FORM); never inherited.
  return { ...DEFAULT_FORM, ...(settings?.defaults || {}), downloadVolumes: false };
}

// Value equality for form fields (strings, numbers, booleans; arrays/objects
// compare by content). Used to decide whether a write is an edit at all.
export function sameFormValue(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// Apply `patch` to a sparse edits object against `baseline` (the values the
// form shows for unedited fields). A key whose new value equals the baseline is
// REMOVED, so it follows later Settings changes; any other value is kept as an
// edit. Returns a new object; `edits` is not mutated. Shared by the New tab
// (baseline = formDefaults(settings)) and SearchTab's download dialog
// (baseline = searchDialogInitial(...)).
export function applyEdits(edits, patch, baseline) {
  const next = { ...(edits || {}) };
  const base = baseline || {};
  for (const [k, v] of Object.entries(patch || {})) {
    if (sameFormValue(v, base[k])) delete next[k];
    else next[k] = v;
  }
  return next;
}

// --webtoon-recompress needs an archive to write the converted pages into,
// so aio-dl.py rejects it for --format pdf/none (hard error at startup,
// see the --webtoon-recompress validation block in aio-dl.py). --komikku
// coerces format→cbz BEFORE that check, so recompress stays valid when
// Komikku is on regardless of the selected format.
export function recompressAllowed(form) {
  return !!(form.komikku || form.format === "cbz" || form.format === "epub");
}

// --modernize is CBZ-only (stricter than webtoon's cbz/epub): its .jxl/.avif
// pages only survive the CBZ byte-passthrough fast-path. komikku coerces
// format→cbz so it qualifies.
//
// These two gates are DERIVED, never written into the form: the format buttons
// used to write modernize/webtoonRecompress = false, which under the edits
// model would have pinned "off" against Settings after a PDF→CBZ round trip.
export function modernizeAllowed(form) {
  return !!(form.komikku || form.format === "cbz");
}

// Build the CLI args dict for a New-tab download. Pure: the same `form` and
// `settings` always give the same args.
//   form     — the DISPLAYED form ({...formDefaults(settings), ...edits}).
//   settings — the app settings in force (verboseAlways is read here).
//   opts.edits    — the sparse edits; mtl / excludeGroup present here are
//                   emitted even at "avoid" / "" so the Settings value that
//                   queueRecipe.globalArgs injects can't override an explicit
//                   choice (downloader.js:buildCliArgs drops both values, so
//                   the CLI still gets Python's default).
//   opts.urlCount — number of URLs; >1 sets --jobs as before.
// The result carries NO settings.defaults spread — the form already folded the
// defaults in, which is how this path has always worked.
export function buildNewTabArgs(form, settings, { edits = {}, urlCount = 1 } = {}) {
  const args = {
    format: form.format,
    quality: form.quality,
    language: form.language,
    verbose: settings?.verboseAlways !== false,
  };

  // Only include changed-from-default values
  if (form.scaling !== 100) args.scaling = form.scaling;
  if (form.format === "epub") args.epubLayout = form.epubLayout;
  if (form.width) args.width = parseInt(form.width, 10);
  if (form.aspectRatio) args.aspectRatio = form.aspectRatio;
  if (form.chapters !== "all") args.chapters = form.chapters;
  if (form.noPartials) args.noPartials = true;
  if (form.keepChapters) args.keepChapters = true;
  if (form.noFinalFile) args.noFinalFile = true;
  if (form.keepImages) args.keepImages = true;
  if (form.noProcessing) args.noProcessing = true;
  if (form.noCleanup) args.noCleanup = true;
  if (form.noRetryMissedChapters) args.noRetryMissedChapters = true;
  if (form.missedRetries !== 2) args.missedRetries = form.missedRetries;
  if (form.multiSource) {
    args.multiSource = true;
    // Only emit --multi-source-quality-min when it differs from the CLI
    // default (0.65). Keeps the spawn args cleaner.
    if (form.multiSourceQualityMin !== 0.65) {
      args.multiSourceQualityMin = form.multiSourceQualityMin;
    }
    // Lazy discovery is absent-means-on: downloader.js's chokepoint emits
    // --multi-source-lazy whenever multiSource is on UNLESS the key is an
    // explicit false, so only the opt-out needs forwarding.
    if (form.multiSourceLazy === false) {
      args.multiSourceLazy = false;
    }
  }
  // Phase F: only emit when the user has it OFF — buildCliArgs turns this
  // into --no-cbz-preserve-originals. The default (true) is Python's own.
  if (form.cbzPreserveOriginals === false) {
    args.cbzPreserveOriginals = false;
  }
  // Webtoon WebP recompression. buildCliArgs forwards quality/method only
  // when the master toggle is on AND they differ from Python's defaults.
  // Gated on recompressAllowed so an incompatible combo (pdf/none without
  // komikku) never reaches the CLI — aio-dl.py would hard-error on it.
  if (form.webtoonRecompress && recompressAllowed(form)) {
    args.webtoonRecompress = true;
    args.webtoonRecompressQuality = form.webtoonRecompressQuality;
    args.webtoonRecompressMethod = form.webtoonRecompressMethod;
  }
  // Komikku output. One boolean — Python does all the coercion.
  if (form.komikku) {
    args.komikku = true;
  }
  // Native volumes. Refused under Komikku rather than sent: aio-dl.py's
  // --komikku turns --download-volumes off, so the job would quietly download
  // CHAPTERS with the volume numbers. DownloadTab disables the switch while
  // Komikku is on; this catches Komikku being switched on in Settings while
  // the item waits in the queue (the recipe is resolved at start).
  if (form.downloadVolumes) {
    if (form.komikku) throw new Error(KOMIKKU_VOLUMES_MESSAGE);
    args.downloadVolumes = true;
  }
  // Content-aware JXL/AVIF transcode. downloader.js:buildCliArgs strips the
  // whole family if the effective args can't ride the CBZ fast-path, so only
  // the format gate is checked here.
  if (form.modernize && modernizeAllowed(form)) {
    args.modernize = true;
    args.modernizeReversible = form.modernizeReversible === true;
    args.modernizeFormat = form.modernizeFormat;
    args.modernizeDistance = form.modernizeDistance;
    args.modernizeQuality = form.modernizeQuality;
    args.modernizeMinSaving = form.modernizeMinSaving;
    args.modernizeEffort = form.modernizeEffort;
    args.modernizeAvifSpeed = form.modernizeAvifSpeed;
  }
  if (form.splitMode === "size" && form.splitValue) args.split = form.splitValue;
  if (form.splitMode === "chapters" && form.splitValue) args.split = `${form.splitValue}ch`;
  if (form.group.trim()) args.group = form.group.trim();
  if (form.excludeGroup.trim() || "excludeGroup" in edits) args.excludeGroup = form.excludeGroup.trim();
  if ((form.mtl && form.mtl !== "avoid") || "mtl" in edits) args.mtl = form.mtl;
  if (form.mixByUpvote) args.mixByUpvote = true;
  if (form.cookies.trim()) args.cookies = form.cookies.trim();
  if (form.jobs > 1) args.jobs = form.jobs;
  if (form.imageWorkers !== 3) args.imageWorkers = form.imageWorkers;
  if (form.httpTimeout !== 30) args.httpTimeout = form.httpTimeout;
  if (form.httpMaxRetries !== 6) args.httpMaxRetries = form.httpMaxRetries;
  if (form.httpBackoffBase !== 1.0) args.httpBackoffBase = form.httpBackoffBase;
  if (form.httpBackoffCap !== 45) args.httpBackoffCap = form.httpBackoffCap;
  if (form.netMinGap !== 0.25) args.netMinGap = form.netMinGap;

  // Multiple URLs go to ONE aio-dl.py process with --jobs N; --jobs defaults
  // to the URL count unless the user set it.
  if (urlCount > 1) {
    args.jobs = form.jobs > 1 ? form.jobs : urlCount;
  }
  return args;
}
