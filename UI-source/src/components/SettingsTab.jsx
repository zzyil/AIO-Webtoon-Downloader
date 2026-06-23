import React, { useState, useEffect, useMemo, useRef } from "react";
import {
  Button, Input, Label, Select, Checkbox, Card, SectionHeader, Slider, Badge, Switch,
} from "@/components/ui/primitives";
import {
  Save, RotateCcw, FolderOpen, FileText, Package, Terminal, RefreshCw,
  Check, AlertTriangle,
} from "lucide-react";
import { cn } from "@/lib/utils";

// Same language list as DownloadTab/SearchTab. Duplicated rather than
// importing to avoid pulling unrelated module deps; one row to add when
// a new language ships, lives in three places.
const LANGUAGES = [
  { value: "en", label: "English" },
  { value: "ja", label: "Japanese" },
  { value: "ko", label: "Korean" },
  { value: "zh", label: "Chinese" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "pt-br", label: "Portuguese (BR)" },
  { value: "de", label: "German" },
  { value: "it", label: "Italian" },
  { value: "ru", label: "Russian" },
  { value: "ar", label: "Arabic" },
  { value: "tr", label: "Turkish" },
];

// Default values for settings.searchOpts. Mirrors DEFAULT_OPTS in
// SearchTab.jsx. Both surfaces (Settings + Search) read/write this
// namespace so changing in one is reflected in the other.
//
// `collapseSplits` was here historically (Phase 3) but moved to the top-level
// settings.collapseSplits in 2026-05-08 because the same toggle now affects
// download behavior, not just search-display diagnostics. See section 8 of
// snappy-forging-waffle.md.
const DEFAULT_SEARCH_OPTS = {
  searchLanguage: "en",
  seededOnly: true,
  multiSource: false,
  multiSourceQualityMin: 0.65,
  searchTimeout: 20,
  searchMinMatch: 0.55,
  searchParallelism: 6,
  // Mirror of SearchTab.jsx DEFAULT_OPTS.enableMlRating. The UI control is
  // in SearchTab's "Advanced search options" Collapsible (not surfaced here)
  // so the shared settings.searchOpts namespace stays schema-aligned across
  // both surfaces. See SearchTab.jsx for the WMI-hang rationale.
  enableMlRating: false,
};

// ── DEFAULT VALUES ──
// Empty placeholders shown only during the brief window before main.js's
// get-settings IPC resolves. The real per-machine defaults come from
// main.js (DEV_SCRIPT_PATH / DEV_WORKING_DIR derived from __dirname in dev,
// or app.getPath("documents")/AIO Downloader in packaged). Used to bake an
// absolute path to the original developer's OneDrive folder here, which
// mkdirSync would silently re-create on any other machine — better to show
// blank for ~50ms than mislead the user with a stranger's home path.
//
// On Reset (handleReset below), dev-mode reverts to these blanks so the
// user can re-pick their workingDir; packaged-mode preserves the existing
// value via the prev.isPackaged guard.
// All three path fields START EMPTY. The empty string is the user's
// "no override — use the runtime-resolved default" sentinel. Every
// consumer (main.js spawn sites) does `settings.X || defaultX`, so
// empty falls through to the runtime default cleanly.
//
// This is part of the 2026-05-13 round-trip fix: AppImage / macOS
// Gatekeeper App Translocation / DMG-direct runs all produce volatile
// auto-computed paths that change between launches. If we initialized
// these to non-empty values (or hydrated them from settings.json with
// the old computed defaults baked in), Save would persist the volatile
// path back to disk and the next launch would ENOENT. Keeping initial
// state empty + filtering the hydration spread + reading resolved
// paths separately via getResolvedPaths() ensures the saved settings
// dict only carries user-typed overrides.
const DEV_DEFAULTS = {
  pythonCmd: "",
  scriptPath: "",
  workingDir: "",
};

// ── Dirty diff for the Save Settings button ──
// Compares the local in-memory settings draft against the most recently
// hydrated `settings` prop (mirrors what's on disk via get-settings IPC).
// Counts every key that differs across the top level + the two known
// nested namespaces (defaults, searchOpts). `isPackaged` is excluded —
// it's read-only from main.js and never persisted (see handleSave below).
// Returns 0 when `settings` is nullish so the button doesn't blink an
// inflated count during the ~50ms before the first get-settings IPC
// resolves on mount.
//
// IMPORTANT: walks ONLY local's keys, not the union of local + settings.
// history.js:saveSettings does a defensive merge (`{...this._settings,
// ...filtered}`) so legacy or obsolete keys on disk (e.g. the
// pre-2026-05-13 `mangafireImageConcurrency` left over after the rename
// migration) survive every save indefinitely. Counting those in the
// dirty diff would produce a phantom count the user can't act on —
// the UI doesn't surface those fields, so there's no control to flip,
// and clicking Save wouldn't bring the count down. By walking only
// local's keys we count exactly the keys the user CAN influence via the
// UI, which is the contract "Save Settings · N changed" implies.
//
// New-feature defaults backfilling missing keys (a new field added in
// the UI's initial useState that older settings.json files don't have
// yet) still surface as dirty — local has the key, settings doesn't,
// `local[k] !== settings[k]` → counted. Saving flushes them to disk
// and the count drops to 0 on the next render. That's correct: those
// one-time inflations represent genuine on-disk work.
// Cross-file: matches the shape of the initial useState below + the
// migration logic in the settings-hydration useEffect + the merge in
// electron/history.js:saveSettings.
function countDirtySettings(local, settings) {
  if (!settings) return 0;
  const SKIP_TOP = new Set(["isPackaged"]);
  const NESTED = ["defaults", "searchOpts"];
  let count = 0;

  for (const k of Object.keys(local)) {
    if (SKIP_TOP.has(k) || NESTED.includes(k)) continue;
    if (local[k] !== settings[k]) count++;
  }

  for (const ns of NESTED) {
    const a = local[ns] || {};
    const b = settings[ns] || {};
    for (const k of Object.keys(a)) {
      if (a[k] !== b[k]) count++;
    }
  }

  return count;
}

// ── Save Settings button with dirty-state + confirmation sweep ──
// Replaces the bare <Button> the footer used to render. Visual states:
//   - idle/clean:   muted primary (bg-primary/60), Check icon, "Up to date"
//   - idle/dirty:   full primary + amber ring + amber dot, Save icon,
//                   "Save Settings · N changed"
//   - in:           emerald-500 fill grows L→R from a pseudo-overlay span
//                   over 280ms; label flips to "Saved" + Check icon
//   - hold:         emerald fill held at scaleX(1) for ~900ms (driven
//                   by the timer gap, not a separate phase)
//   - out:          emerald fill shrinks scaleX(1)→scaleX(0) with the
//                   origin flipped to right; takes 320ms then → idle
//   - error:        bg-destructive, AlertTriangle icon, "Save failed";
//                   auto-dismisses after 2500ms back to idle (which then
//                   renders dirty because the hydration round-trip
//                   never happened — dirty count stayed > 0)
//
// The L→R origin flip when entering 'out' is invisible: transform-origin
// is NOT in the transition-property list, so it snaps instantly while
// scaleX is still 1 (visually identical regardless of origin at scale 1),
// then the scaleX(1)→scaleX(0) shrink runs against the new right origin.
//
// Cross-file coupling: `cn` from @/lib/utils (clsx + tailwind-merge).
// Async onSave threads through SettingsTab.handleSave → useDownloader.
// saveSettings (await window.electronAPI.saveSettings + setSettings) →
// preload.js → electron/main.js's "save-settings" IPC handler →
// history.js:saveSettings (volatile-path filter + atomic file write).
// IPC currently resolves in <50ms locally, so the sweep is the dominant
// duration the user actually perceives — any "loading" spinner state
// would flash by too fast to register and isn't worth wiring up.
function SaveSettingsButton({ dirty, onSave }) {
  const [phase, setPhase] = useState("idle"); // 'idle' | 'in' | 'out' | 'error'
  const timers = useRef([]);

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const cancelTimers = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  const handleClick = async () => {
    if (phase !== "idle") return;

    try {
      await onSave();
    } catch {
      cancelTimers();
      setPhase("error");
      timers.current.push(setTimeout(() => setPhase("idle"), 2500));
      return;
    }

    cancelTimers();
    setPhase("in");
    // 280ms sweep-in + 900ms hold + 320ms sweep-out ≈ 1500ms total. Two
    // timers fire at the in→out boundary (1180ms) and the final reset
    // to idle (1500ms); the sweep visual itself is driven by CSS
    // transitions off the phase-derived className below.
    timers.current.push(setTimeout(() => setPhase("out"), 1180));
    timers.current.push(setTimeout(() => setPhase("idle"), 1500));
  };

  const isAnimating = phase === "in" || phase === "out";
  const isErrored = phase === "error";
  const isClean = dirty === 0;

  const sweepScale = phase === "in" ? "scale-x-100" : "scale-x-0";
  const sweepOrigin = phase === "out" ? "origin-right" : "origin-left";
  const sweepDuration =
    phase === "in" ? "duration-[280ms]" :
    phase === "out" ? "duration-[320ms]" :
    "duration-0";

  let icon, label, baseColor, showAmberDot, ring;
  if (isErrored) {
    icon = <AlertTriangle className="w-4 h-4" />;
    label = "Save failed";
    baseColor = "bg-destructive text-destructive-foreground";
    showAmberDot = false;
    ring = "";
  } else if (isAnimating) {
    icon = <Check className="w-4 h-4" />;
    label = "Saved";
    // text-white reads cleanly over both bg-primary (under-sweep) and
    // bg-emerald-500 (over-sweep) in both light + dark themes.
    baseColor = "bg-primary text-white";
    showAmberDot = false;
    ring = "";
  } else if (isClean) {
    icon = <Check className="w-4 h-4" />;
    label = "Up to date";
    baseColor = "bg-primary/60 text-primary-foreground/90 hover:bg-primary/70";
    showAmberDot = false;
    ring = "";
  } else {
    icon = <Save className="w-4 h-4" />;
    label = `Save Settings · ${dirty} changed`;
    baseColor = "bg-primary text-primary-foreground hover:bg-primary/90";
    showAmberDot = true;
    ring = "ring-2 ring-amber-500/30";
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={isAnimating || isErrored}
      aria-busy={isAnimating}
      aria-live="polite"
      className={cn(
        "relative overflow-hidden inline-flex flex-1 items-center justify-center gap-2",
        "h-9 px-4 py-2 text-sm rounded-md font-medium shadow-sm",
        "transition-colors duration-200",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        baseColor,
        ring,
      )}
    >
      {/* Sweep overlay — emerald fill driven by phase-derived classes.
          Sits absolutely inside overflow-hidden so the rounded corners
          clip the sweep; z-0 (implicit) keeps it behind the label span. */}
      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-0 bg-emerald-500 transition-transform ease-out",
          sweepScale,
          sweepOrigin,
          sweepDuration,
        )}
      />
      <span className="relative z-10 inline-flex items-center gap-2">
        {showAmberDot && (
          <span
            aria-hidden="true"
            className="w-1.5 h-1.5 rounded-full bg-amber-400 shadow-[0_0_4px_rgba(245,158,11,0.7)]"
          />
        )}
        {icon}
        {label}
      </span>
    </button>
  );
}

export default function SettingsTab({ settings, onSave }) {
  // Local copy of settings so changes don't apply until you click Save
  const [local, setLocal] = useState({
    pythonCmd: DEV_DEFAULTS.pythonCmd,
    scriptPath: DEV_DEFAULTS.scriptPath,
    workingDir: DEV_DEFAULTS.workingDir,
    verboseAlways: true,
    // Global chapter-collapse toggle. Affects:
    //   - Search-display "X main / Y entries" diagnostic counts.
    //   - Actual download behavior — split clusters (e.g. 1.1/1.2/1.3/1.4)
    //     merge into one combined chapter file; redundant duplicate uploads
    //     of the same chapter are pruned. See sites/chapter_merger.py
    //     :group_chapters_for_download for the full 6-rule cluster table.
    // Persisted as settings.collapseSplits; useDownloader.queueDownload and
    // .runSearch inject this into args/opts before IPC.
    collapseSplits: true,
    // Inter-chapter image prefetch worker count (Phase G7, 2026-05-08).
    // While chapter N is encoding (CPU-bound), a background thread
    // downloads chapter N+1's images. -1 = match Image Workers (default).
    // 0 = disable prefetch entirely. Positive N = use exactly N workers.
    // Drop to 4 (or 0) when the upstream CDN is throttling and the extra
    // concurrent burst from N+1's downloads compounds throttling.
    prefetchImageWorkers: -1,
    // ── Image prefetch & concurrency (generalized 2026-05-13) ──
    // Apply to any handler with SUPPORTS_FAST_DOWNLOAD=True (currently
    // mangafire and linewebtoon; see sites/base.py:fast_download_images
    // for the implementation).
    //   - imageConcurrency: asyncio.Semaphore bound for image fetches via
    //     curl_cffi async + HTTP/2 multiplex. 8 hits ~5 MB/s (near network
    //     ceiling on home links). Auto-dials down per-host on CDN errors.
    //   - imagePrefetchDepth: how many chapters ahead to keep queued for
    //     image prefetch. Higher helps when main-loop processing is fast
    //     relative to network download (CBZ fast-path, LINE Webtoon).
    //   - imagePrefetchParallel: concurrent prefetch worker threads. =2
    //     means up to 2 chapters in flight while main processes a third.
    //   - noFastDownload: escape hatch — force-disable curl_cffi path.
    // queueDownload (useDownloader.js) injects each into args only when
    // not at default. Pre-2026-05-13 setting `mangafireImageConcurrency`
    // is migrated to `imageConcurrency` at settings-load time below.
    imageConcurrency: 8,
    imagePrefetchDepth: 2,
    imagePrefetchParallel: 2,
    noFastDownload: false,
    // ── MangaFire VRF capture knobs intentionally NOT surfaced in UI ──
    // VRF is MangaFire-specific browser-automation tuning that users
    // shouldn't need to touch. The CLI flags
    // --mangafire-vrf-prefetch-depth (default 4) and
    // --mangafire-vrf-parallel (default 1) still exist for advanced
    // tuning; the UI just inherits the argparse defaults.
    // How often the UI refreshes logs & progress (in milliseconds).
    // Lower = more responsive. Default: 100ms (10 updates/sec).
    logUpdateInterval: 100,
    // When true, update checks scan actual files on disk instead of
    // trusting .aio_series.json. Saved as a top-level setting.
    useFileBasedChapterCheck: false,
    // ── Library Check All filter ──
    // When true (default), "Check All" includes series marked Completed /
    // Finished, not just Ongoing / Releasing. Many aggregators — mangafire
    // most notoriously — slap "Completed" on actively-updating series, so
    // skipping them by default would silently leave the user months behind
    // on a chunk of their library. The cost of an extra check on a truly
    // completed series is ~one Python proc (handled by the parallel pool
    // in <1 ms of wall-time per slot saturation), vs. the cost of missing
    // weeks of releases by trusting the bad metadata. Cross-file: the
    // filter lives in UI-source/electron/main.js:check-all-updates handler;
    // LibraryTab.jsx mirrors the filter for the toolbar's ongoingCount.
    checkAllIncludeCompleted: true,
    // Parallel "Check All" worker count. Clamped to [1, 8] by main.js. 4 is
    // the safe sweet spot — finishes 30 series in 30-60s while keeping any
    // single site from getting hit with 4+ concurrent --list-chapters calls
    // (the provider-aware scheduler in main.js further fans across distinct
    // sites when possible).
    checkAllConcurrency: 4,
    // When ON (default), downloads queued from the UpdatesCenter panel get
    // --seeded-rating-only injected so the multi-source rating skips its
    // image-quality probe phase and ranks alternatives from
    // sites/quality_seed.json's per-site priors instead. Saves 30-60+ s
    // per series on MangaFire-class handlers (the probe runs Playwright
    // VRF per sample chapter plus image-quality scoring). For a 1-5
    // chapter update delta, the probe cost dwarfs the actual download.
    // Off restores full probe accuracy at the per-download cost — pick
    // off only if you've tuned multi-source ranking carefully and want
    // every download to use measured scores. Read in LibraryTab.jsx's
    // buildDownloadArgsForRow; flag plumbed via downloader.js's
    // seededRatingOnly boolMap entry; honored in aio_search_cli.
    // find_alternatives_for_direct_url.
    updateChecksUseSeededRating: true,
    // ── External metadata enrichment (--metadata-source family) ──
    // Top-level "global setting" semantic: applies to EVERY download
    // regardless of which tab spawned it (New / Search / Library / queue).
    // useDownloader.queueDownload injects these into args for every
    // electronAPI.startDownload, so it's a true app-wide preference rather
    // than a per-download form value. Defaults match the Python argparse
    // defaults so a Save with the section untouched is a no-op for the
    // spawn line. Python side: aio-dl.py near --enable-ml-rating (flag
    // registration) + sites/external_metadata.py (the AniList GraphQL
    // client). Grep cross-file: metadataSource, metadata-source.
    metadataSource: "none",
    metadataTagMinRank: 50,
    metadataRefresh: false,
    // Whether the app is running from an installed .exe (bundled mode)
    // or from source (dev mode). Set by main.js, read-only here.
    isPackaged: false,
    defaults: {
      format: "pdf",
      // Global download language. DownloadTab.jsx:32 had its own per-form
      // default of "en"; surfacing it here lets the user pick a different
      // global default (e.g. "ja" for a Japanese-only library) without
      // changing the dropdown on every download. DownloadTab's useEffect
      // at line ~95-99 already spreads settings.defaults onto its form,
      // so this propagates through automatically. Library-tab downloads
      // override with the per-series saved language; Search-tab downloads
      // inherit via App.jsx's defaults-spread.
      language: "en",
      // 100 (not aio-dl.py's argparse default of 85): Phase G4 in aio-dl.py
      // (~line 4272) sets _user_set_quality = (--quality on argv) AND
      // (args.quality < 100). When True, the CBZ byte-preserving fast-path
      // (cbzPreserveOriginals) is bypassed in favor of decode/re-encode.
      // The UI always emits --quality from form state, so a default of 85
      // would force every default CBZ download into the slow legacy path
      // — defeating the cbzPreserveOriginals toggle for everyone except
      // users who manually slide the quality up to 100. The Python
      // argparse default of 85 still applies to direct CLI users; the
      // UI's separate default is intentional. Keep this at 100 unless
      // you also revisit the Phase G4 guard.
      quality: 100,
      scaling: 100,
      keepChapters: false,
      noFinalFile: false,
      keepImages: false,
      noProcessing: false,
      noCleanup: false,
      imageWorkers: 3,
      httpTimeout: 30,
      httpMaxRetries: 6,
      jobs: 1,
      // Multi-source fallback default (added 2026-05-07). DownloadTab's
      // useEffect spreads settings.defaults into its form on mount, so
      // setting these here makes them survive both tab switches and
      // session restarts. Per-job overrides in DownloadTab don't save back.
      multiSource: false,
      multiSourceQualityMin: 0.65,
      // CBZ byte-preservation default (added 2026-05-07). When ON (default),
      // CBZ output uses the original wire bytes from the CDN (lossless,
      // fastest, smallest archives). Setting this to false emits
      // --no-cbz-preserve-originals which forces decode/re-encode even at
      // --scaling 100. The downloader.js boolMap handles the negative-form
      // flag emission. Only meaningful for --format cbz.
      cbzPreserveOriginals: true,
      // Komikku-compatible per-chapter CBZ output (2026-05-12, Komikku LocalSource format).
      // When ON, Python auto-coerces --format cbz --keep-chapters
      // --no-final-file and writes per-chapter ComicInfo.xml + cover.jpg
      // + details.json at <out>/manga/<Series>/. The format selector
      // above is effectively ignored when this is on (Python prints a
      // [Komikku] coercion notice in the log). DownloadTab's DEFAULT_FORM
      // spread picks this up via the useEffect at line ~120-124; App.jsx's
      // search/library wrappers spread it into queueDownload args.
      komikku: false,
      // LINE Webtoon WebP recompression defaults (Phase 1, 2026-05-11).
      // When enabled here, BOTH the New tab AND search/library-initiated
      // downloads inherit these knobs: the New tab's DEFAULT_FORM spread
      // (DownloadTab.jsx:~110) and App.jsx's settings.defaults spread for
      // the search/library onStartDownload wrappers (App.jsx:~155 and
      // :~192) both pick this up. Master toggle is off by default so
      // existing user flows are unchanged; toggling on in Settings makes
      // every new webtoons.com download recompress without per-job UI.
      // Silently no-ops for non-webtoons.com handlers (Python checks
      // handler.name === "linewebtoon" before the encode pass).
      webtoonRecompress: false,
      webtoonRecompressQuality: 85,
      webtoonRecompressMethod: 4,
      // Content-aware JXL/AVIF transcode (opt-in, CBZ-only). Mirrors the
      // --modernize* CLI flags (aio-dl.py, grep '--modernize compatibility
      // checks'). Rides the CBZ byte-passthrough fast-path, so it's only valid
      // with the fast-path conditions (format cbz/komikku, quality 100,
      // scaling 100, preserve-originals on, no-processing off); the toggle in
      // the Modernize section below auto-corrects those on enable, and
      // downloader.js:buildCliArgs (modernizeBlocked) strips the flag if
      // they're ever violated. DownloadTab's DEFAULT_FORM spread + App.jsx's
      // search/library defaults spread propagate these to every download path.
      modernize: false,
      modernizeFormat: "auto",      // auto | jxl | avif | jxl+avif
      modernizeQuality: 90,         // AVIF color quality (1-100)
      modernizeDistance: 1.0,       // JXL grayscale distance (0.0 = lossless)
      modernizeMinSaving: 0.92,     // keep transcode only if < orig * this
    },
    // Per-search defaults — read by SearchTab on mount via the same
    // settings.searchOpts namespace. Surfaced here so the user has one
    // central place to configure both download and search defaults.
    searchOpts: { ...DEFAULT_SEARCH_OPTS },
  });

  // Display-only resolved paths (what main.js would use as defaults when
  // local.pythonCmd / .scriptPath / .workingDir are empty). Shown as
  // placeholders/hints in the path input fields below. NOT persisted —
  // fetched once via the dedicated read-only IPC so the volatile auto-
  // resolved values (AppImage /tmp/.mount_*, macOS App Translocation,
  // DMG-direct) never enter the saveSettings round-trip.
  const [resolved, setResolved] = useState({
    pythonCmd: "",
    scriptPath: "",
    workingDir: "",
  });

  // Fetch resolved paths from main.js once on mount. The handler is a
  // pure read of the computePaths() globals — no side effects, runs in
  // microseconds. We don't refresh on every settings change because
  // those globals don't move at runtime.
  useEffect(() => {
    let cancelled = false;
    const api = typeof window !== "undefined" && window.electronAPI;
    if (api && typeof api.getResolvedPaths === "function") {
      api.getResolvedPaths()
        .then((r) => { if (!cancelled && r) setResolved(r); })
        .catch(() => { /* main.js missing handler → keep empty placeholders */ });
    }
    return () => { cancelled = true; };
  }, []);

  // Load settings when they arrive from Electron
  useEffect(() => {
    if (settings) {
      // Migration: pre-2026-05-13 settings dicts carry
      // `mangafireImageConcurrency` (MangaFire-only); the flag was
      // generalized + renamed to `imageConcurrency` and now applies to
      // any handler with SUPPORTS_FAST_DOWNLOAD. Forward the old key
      // ONCE on read; the next handleSave persists under the new name
      // and the old key falls off naturally on the next save round-trip.
      // Idempotent: no-op when imageConcurrency is already present.
      const migrated = { ...settings };
      if (
        migrated.mangafireImageConcurrency != null &&
        migrated.imageConcurrency == null
      ) {
        migrated.imageConcurrency = migrated.mangafireImageConcurrency;
      }
      // Drop the legacy key so it doesn't get written back on save.
      delete migrated.mangafireImageConcurrency;

      // Volatile-path filter on hydration — mirrors the write-side
      // filter in history.js:saveSettings. Existing users may have
      // pre-2026-05-13 settings.json files carrying stale AppImage /
      // macOS Gatekeeper-translocation paths that were auto-computed
      // and round-tripped before the fix. Without this filter, the
      // stale path would hydrate into local.scriptPath, the input
      // would show the broken path as the value (not a placeholder),
      // and even though saveSettings would reject the bad write, the
      // existing on-disk value would never get cleared. Stripping
      // volatile values here lets the input fall back to the resolved
      // placeholder so users see what's actually going to run.
      // Non-volatile customizations (e.g. a custom `python3.13` venv
      // path the user typed deliberately) pass through unchanged.
      const VOLATILE_PATH_PATTERNS = [
        /^\/tmp\/\.mount_/,
        /\/AppTranslocation\/[0-9A-F-]+\//,
        /\/Volumes\/[^/]+\.app\//,
      ];
      for (const k of ["pythonCmd", "scriptPath", "workingDir"]) {
        const v = migrated[k];
        if (typeof v === "string" && v) {
          const normalized = v.replace(/\\/g, "/");
          if (VOLATILE_PATH_PATTERNS.some((re) => re.test(normalized))) {
            delete migrated[k];
          }
        }
      }

      setLocal((prev) => ({
        ...prev,
        ...migrated,
        defaults: { ...prev.defaults, ...(migrated.defaults || {}) },
        searchOpts: { ...prev.searchOpts, ...(migrated.searchOpts || {}) },
      }));
    }
  }, [settings]);

  const set = (key, value) => setLocal((prev) => ({ ...prev, [key]: value }));
  const setDefault = (key, value) =>
    setLocal((prev) => ({
      ...prev,
      defaults: { ...prev.defaults, [key]: value },
    }));
  const setSearchOpt = (key, value) =>
    setLocal((prev) => ({
      ...prev,
      searchOpts: { ...prev.searchOpts, [key]: value },
    }));

  // Whether the default --webtoon-recompress toggle is valid for the current
  // default format. aio-dl.py rejects recompress with --format pdf/none (no
  // archive to write into); --komikku coerces format→cbz first, so it's
  // allowed then. Mirrors DownloadTab's recompressAllowed. Drives the default
  // toggle's disabled state + the format-select auto-clear below.
  const recompressAllowedDefault =
    local.defaults.komikku ||
    local.defaults.format === "cbz" ||
    local.defaults.format === "epub";

  // Whether --modernize is valid for the current default config. Unlike
  // webtoon-recompress (which also allows epub), modernize is CBZ-ONLY: it
  // emits .jxl/.avif pages that only survive the CBZ byte-passthrough
  // fast-path (other formats re-encode them away). komikku coerces format→cbz
  // so it qualifies too. Drives the toggle's disabled state + the on-enable
  // auto-correct below; mirrors aio-dl.py's '--modernize compatibility checks'.
  const modernizeAllowedDefault =
    local.defaults.komikku || local.defaults.format === "cbz";

  // Fast-path conditions modernize needs beyond the format gate (quality 100,
  // scaling 100, preserve-originals on, no-processing off). The enable handler
  // sets these, but the user can still break them afterward via the
  // sliders/toggles above — surface that as a warning instead of letting
  // aio-dl.py hard-error mid-spawn. buildCliArgs (modernizeBlocked) also strips
  // --modernize defensively in that case, so a broken combo just skips
  // modernize rather than failing the whole download.
  const modernizeConflicts =
    !!local.defaults.modernize && modernizeAllowedDefault && (
      (local.defaults.quality ?? 100) < 100 ||
      (local.defaults.scaling ?? 100) < 100 ||
      local.defaults.cbzPreserveOriginals === false ||
      local.defaults.noProcessing === true
    );

  // Dirty count drives the SaveSettingsButton's pre-click visual
  // ("Save Settings · N changed" + amber ring/dot when > 0, "Up to date"
  // when 0). Recomputed cheaply on any local-state change; the diff
  // walks ~30 top-level keys + ~25 nested keys, well under a frame.
  // See countDirtySettings above for the migration-edge-case rationale.
  const dirty = useMemo(() => countDirtySettings(local, settings), [local, settings]);

  // Awaited (not fire-and-forget) so SaveSettingsButton can chain its
  // sweep-in only after the IPC round-trip resolves successfully — and
  // route to the 'error' branch if history.saveSettings throws (disk
  // full, EACCES, etc.). Pre-change behavior dropped the promise; the
  // failure was an unhandled rejection. `await onSave(saveable)`
  // preserves the rejection so the button's try/catch sees it.
  const handleSave = async () => {
    const { isPackaged, ...saveable } = local;
    await onSave(saveable);
  };

  const handleReset = () => {
    setLocal((prev) => ({
      // Keep isPackaged from the current state (it's set by main.js).
      // Path fields reset to empty in BOTH packaged and dev modes —
      // empty == "use the runtime-resolved default" per the post-2026-05-13
      // round-trip-prevention design. The placeholder shown in the UI
      // (sourced from getResolvedPaths) tells the user what's actually
      // going to run; the empty string is just the absence of an override.
      pythonCmd: "",
      scriptPath: "",
      workingDir: "",
      isPackaged: prev.isPackaged,
      verboseAlways: true,
      collapseSplits: true,
      prefetchImageWorkers: -1,
      imageConcurrency: 8,
      imagePrefetchDepth: 2,
      imagePrefetchParallel: 2,
      noFastDownload: false,
      logUpdateInterval: 100,
      useFileBasedChapterCheck: false,
      // Mirror initial state: Check All includes Completed by default, four
      // parallel workers. See rationale in the initial useState above.
      checkAllIncludeCompleted: true,
      checkAllConcurrency: 4,
      updateChecksUseSeededRating: true,
      // Metadata enrichment defaults — mirror the initial-state block above.
      // Off/default values match the Python argparse defaults so Reset
      // produces a clean "no spawn-line metadata flags" state.
      metadataSource: "none",
      metadataTagMinRank: 50,
      metadataRefresh: false,
      defaults: {
        format: "pdf",
        language: "en",
        // See the rationale on the corresponding line in the initial-state
        // defaults block above — 100, not 85, to keep cbzPreserveOriginals's
        // fast-path active by default. Reset must mirror initial state.
        quality: 100,
        scaling: 100,
        keepChapters: false,
        noFinalFile: false,
        keepImages: false,
        noProcessing: false,
        noCleanup: false,
        imageWorkers: 3,
        httpTimeout: 30,
        httpMaxRetries: 6,
        jobs: 1,
        cbzPreserveOriginals: true,
        multiSource: false,
        multiSourceQualityMin: 0.65,
        // Mirror webtoonRecompress* initial-state defaults so Reset clears
        // them too. See the rationale block in the initial useState above.
        webtoonRecompress: false,
        webtoonRecompressQuality: 85,
        webtoonRecompressMethod: 4,
        // Modernize transcode defaults — mirror the initial-state block above.
        modernize: false,
        modernizeFormat: "auto",
        modernizeQuality: 90,
        modernizeDistance: 1.0,
        modernizeMinSaving: 0.92,
        // Komikku-mode default. Reset mirrors initial-state; see the
        // rationale block in the initial useState above.
        komikku: false,
      },
      searchOpts: { ...DEFAULT_SEARCH_OPTS },
    }));
  };

  const browseScript = async () => {
    if (!window.electronAPI) return;
    const path = await window.electronAPI.pickFile([
      { name: "Python Scripts", extensions: ["py"] },
    ]);
    if (path) set("scriptPath", path);
  };

  const browseWorkingDir = async () => {
    if (!window.electronAPI) return;
    const path = await window.electronAPI.pickFolder();
    if (path) set("workingDir", path);
  };

  const [confirmReinstall, setConfirmReinstall] = useState(false);
  const handleReinstall = async () => {
    if (!window.electronAPI?.reinstallPython) return;
    if (!confirmReinstall) {
      setConfirmReinstall(true);
      setTimeout(() => setConfirmReinstall(false), 4000);
      return;
    }
    setConfirmReinstall(false);
    await window.electronAPI.reinstallPython();
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-1">
        {/* Paths */}
        <SectionHeader>Paths</SectionHeader>

        {/* ── BUNDLED MODE: Show a simple info card instead of editable paths ── */}
        {local.isPackaged ? (
          <Card className="p-3 space-y-2 bg-emerald-500/10 border-emerald-500/30">
            <div className="flex items-center gap-2">
              <Package className="w-4 h-4 text-emerald-500" />
              <span className="text-sm font-medium text-emerald-600 dark:text-emerald-400">
                Bundled Python
              </span>
              <Badge variant="secondary" className="text-[10px] ml-auto">Installed Mode</Badge>
            </div>
            <p className="text-[11px] text-muted-foreground leading-relaxed">
              Python, Playwright, and all dependencies were set up automatically.
              No external Python installation needed.
            </p>
            {/* Still show the working directory so users know where manga is saved */}
            <div className="pt-1">
              <Label className="text-xs">Output Directory (manga saved here)</Label>
              <div className="flex gap-2 mt-1">
                <Input
                  value={local.workingDir}
                  onChange={(e) => set("workingDir", e.target.value)}
                  placeholder={resolved.workingDir || ""}
                  className="flex-1 font-mono text-xs"
                />
                <Button variant="outline" size="sm" onClick={browseWorkingDir}>
                  <FolderOpen className="w-3.5 h-3.5" />
                </Button>
              </div>
              <p className="text-[10px] text-muted-foreground mt-1">
                {local.workingDir
                  ? <>The "manga" folder will be created inside this directory.</>
                  : <>Using auto-resolved default. Leave blank to keep it; type a path to override.</>
                }
              </p>
            </div>
            {/* Reinstall button — re-downloads Python from scratch */}
            <div className="pt-1">
              <Button
                variant="outline"
                size="sm"
                className={confirmReinstall
                  ? "text-xs gap-1.5 text-destructive border-destructive/50 hover:text-destructive hover:border-destructive/50"
                  : "text-xs gap-1.5 text-muted-foreground hover:text-destructive hover:border-destructive/50"
                }
                onClick={handleReinstall}
                onBlur={() => setConfirmReinstall(false)}
              >
                <RefreshCw className="w-3 h-3" />
                {confirmReinstall ? "Click again to confirm reinstall" : "Reinstall Python Environment"}
              </Button>
            </div>
          </Card>
        ) : (
          /* ── DEV MODE: Full editable paths ── */
          <div className="space-y-3">
            <div className="flex items-center gap-2 mb-2">
              <Terminal className="w-3.5 h-3.5 text-muted-foreground" />
              <span className="text-[10px] text-muted-foreground">
                Dev mode — using system Python
              </span>
            </div>

            <div>
              <Label className="text-xs">Python Command</Label>
              <Input
                value={local.pythonCmd}
                onChange={(e) => set("pythonCmd", e.target.value)}
                placeholder={resolved.pythonCmd || "python"}
                className="mt-1 font-mono text-sm"
              />
              <p className="text-[10px] text-muted-foreground mt-1">
                {local.pythonCmd
                  ? <>Custom override. Clear to fall back to auto-resolved <span className="font-mono">{resolved.pythonCmd || "python"}</span>.</>
                  : <>Using auto-resolved <span className="font-mono">{resolved.pythonCmd || "python"}</span>. Type to override (e.g. <span className="font-mono">python3.13</span> for a specific venv).</>
                }
              </p>
            </div>

            <div>
              <Label className="text-xs">aio-dl.py Location</Label>
              <div className="flex gap-2 mt-1">
                <Input
                  value={local.scriptPath}
                  onChange={(e) => set("scriptPath", e.target.value)}
                  placeholder={resolved.scriptPath || ""}
                  className="flex-1 font-mono text-xs"
                />
                <Button variant="outline" size="sm" onClick={browseScript}>
                  <FileText className="w-3.5 h-3.5" />
                </Button>
              </div>
              <p className="text-[10px] text-muted-foreground mt-1">
                {local.scriptPath
                  ? <>Custom override. Clear to fall back to the auto-resolved path shown as placeholder.</>
                  : <>Using auto-resolved default. Leave blank or type a path to override.</>
                }
              </p>
            </div>

            <div>
              <Label className="text-xs">Working Directory</Label>
              <div className="flex gap-2 mt-1">
                <Input
                  value={local.workingDir}
                  onChange={(e) => set("workingDir", e.target.value)}
                  placeholder={resolved.workingDir || ""}
                  className="flex-1 font-mono text-xs"
                />
                <Button variant="outline" size="sm" onClick={browseWorkingDir}>
                  <FolderOpen className="w-3.5 h-3.5" />
                </Button>
              </div>
              <p className="text-[10px] text-muted-foreground mt-1">
                {local.workingDir
                  ? <>Custom override. The "manga" output folder will be created here. Clear to fall back to the auto-resolved path.</>
                  : <>Using auto-resolved default. Leave blank or type a path to override.</>
                }
              </p>
            </div>
          </div>
        )}

        {/* Default Format & Quality */}
        <SectionHeader>Default Format &amp; Quality</SectionHeader>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3">
          <div>
            <Label className="text-xs">Format</Label>
            <Select
              value={local.defaults.format}
              onChange={(e) => {
                const next = e.target.value;
                // Honor the "None (images only)" label promise: aio-dl.py
                // treats --format none as "skip the final book build" and
                // silently passes on it (line ~6196), with no output unless
                // --keep-images or --keep-chapters is also set. Selecting
                // None here auto-enables keepImages so the user actually
                // gets the images the label advertises. They can still
                // manually uncheck keepImages later for a metadata-only
                // run, but the warning below will fire if they do.
                setLocal((prev) => ({
                  ...prev,
                  defaults: {
                    ...prev.defaults,
                    format: next,
                    ...(next === "none" ? { keepImages: true } : {}),
                    // PDF/None can't carry --webtoon-recompress (no archive to
                    // write into; aio-dl.py hard-errors). Auto-clear the
                    // default so we never save a contradictory combo. Skip
                    // when Komikku is on — it coerces format→cbz.
                    ...((next === "pdf" || next === "none") && !prev.defaults.komikku
                      ? { webtoonRecompress: false }
                      : {}),
                    // --modernize is CBZ-only (stricter than webtoon, which
                    // also allows epub): its .jxl/.avif pages only survive CBZ
                    // output. Clear it whenever the new format isn't cbz and
                    // komikku isn't coercing to cbz, so we never persist a
                    // contradictory combo (mirrors modernizeAllowedDefault).
                    ...(next !== "cbz" && !prev.defaults.komikku
                      ? { modernize: false }
                      : {}),
                  },
                }));
              }}
              className="mt-1"
            >
              <option value="pdf">PDF</option>
              <option value="epub">EPUB</option>
              <option value="cbz">CBZ</option>
              <option value="none">None (images only)</option>
            </Select>
            {/* Warning fires only when the user has explicitly unchecked
                both Keep images and Keep chapters under format=none — the
                only path that produces an empty manga folder. The format
                onChange above auto-enables keepImages, so the default
                "select None" path never trips this. */}
            {local.defaults.format === "none"
              && !local.defaults.keepImages
              && !local.defaults.keepChapters && (
              <p className="text-[10px] text-yellow-500 dark:text-yellow-400 mt-1 leading-snug">
                Format = None with no "Keep images" / "Keep chapters" enabled
                produces nothing in the manga folder (only metadata).
                Re-enable one of those toggles to keep raw images or
                per-chapter files.
              </p>
            )}
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <Label className="text-xs">Quality</Label>
              <Badge variant="secondary">{local.defaults.quality}</Badge>
            </div>
            <Slider
              value={local.defaults.quality}
              onValueChange={(v) => setDefault("quality", v)}
              min={1}
              max={100}
            />
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <Label className="text-xs">Scaling</Label>
              <Badge variant="secondary">{local.defaults.scaling}%</Badge>
            </div>
            <Slider
              value={local.defaults.scaling}
              onValueChange={(v) => setDefault("scaling", v)}
              min={1}
              max={100}
            />
          </div>

          {/* Global default download language. DownloadTab's useEffect
              spreads settings.defaults into its form, so picking a
              language here makes it the default in the New tab AND in
              search-initiated downloads (App.jsx:185-194 spreads defaults
              into the queueDownload args). Library-tab downloads still
              use the per-series saved language from .aio_series.json,
              which is correct — that's the language the series was
              originally fetched in. */}
          <div>
            <Label className="text-xs">Default language</Label>
            <Select
              value={local.defaults.language || "en"}
              onChange={(e) => setDefault("language", e.target.value)}
              className="mt-1"
            >
              {LANGUAGES.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </Select>
          </div>
        </div>

        {/* CBZ byte-preservation toggle (Phase F, 2026-05-07).
            Only meaningful when --format cbz; the flag is benign on other
            formats (aio-dl ignores it). When ON (default), CBZ output keeps
            the original wire bytes from the CDN — lossless, fastest,
            smallest. Turn off to force decode/re-encode (uses --quality and
            --scaling). downloader.js emits --no-cbz-preserve-originals only
            when this is === false. */}
        <div className="flex items-center justify-between gap-3 mt-3 pt-3 border-t border-border/50">
          <div className="flex-1">
            <Label className="text-xs cursor-pointer">CBZ: preserve original image bytes</Label>
            <p className="text-[10px] text-muted-foreground mt-0.5">
              When on, CBZ archives the original CDN wire bytes (WebP/JPEG/PNG)
              losslessly. Turn off to force decode + re-encode (slower,
              respects --quality and --scaling).
            </p>
          </div>
          <Switch
            checked={local.defaults.cbzPreserveOriginals !== false}
            onCheckedChange={(v) => setDefault("cbzPreserveOriginals", v)}
          />
        </div>

        {/* Komikku output toggle (2026-05-12, Komikku LocalSource format).
            When ON, Python force-coerces --format cbz / --keep-chapters /
            --no-final-file regardless of the Format selector above. Each
            chapter CBZ gets its own ComicInfo.xml (overrides filename-derived
            chapter number / title / scanlator in Komikku v1.13.5+), plus
            cover.jpg + details.json at the series-folder root. Output stays
            at <workingDir>/manga/<Series>/ — user syncs to phone's
            <Komikku-SAF>/local/ themselves. DownloadTab's useEffect spreads
            settings.defaults into its form on mount, so toggling here
            propagates to every new download. */}
        <SectionHeader>Komikku Output</SectionHeader>
        <div className="flex items-center justify-between gap-3">
          <div className="flex-1">
            <Label className="text-xs cursor-pointer">
              Write Komikku-compatible per-chapter CBZs
            </Label>
            <p className="text-[10px] text-muted-foreground mt-0.5">
              Each chapter is its own CBZ with a per-chapter{" "}
              <span className="font-mono">ComicInfo.xml</span> (chapter number,
              title, scanlator, web URL, upload date). Series folder also gets{" "}
              <span className="font-mono">cover.jpg</span> +{" "}
              <span className="font-mono">details.json</span> (status, genres,
              authors). Forces format=CBZ, keep-chapters, no-final-file.
              Output stays at <span className="font-mono">manga/&lt;Series&gt;/</span>;
              sync that into Komikku's storage root yourself.
            </p>
          </div>
          <Switch
            checked={!!local.defaults.komikku}
            onCheckedChange={(v) => setDefault("komikku", v)}
          />
        </div>

        {/* Modernize Images (JXL/AVIF) ───────────────────────────────
            Opt-in content-aware transcode (--modernize family). Placed under
            Komikku Output because it's CBZ-only and pairs naturally with the
            per-chapter Komikku CBZs (komikku coerces format→cbz). The master
            toggle is gated on modernizeAllowedDefault (komikku || format cbz)
            and, on enable, auto-corrects the fast-path conditions (quality 100
            / scaling 100 / preserve-originals on / no-processing off) so the
            saved config can't hard-error at spawn — mirrors the format=none →
            keepImages auto-enable precedent above. The valued knobs map 1:1 to
            the CLI: codec routing, JXL distance, AVIF quality, min-saving.
            DownloadTab's DEFAULT_FORM spread + App.jsx's defaults spread carry
            these to every download; downloader.js:buildCliArgs maps modernize*
            → --modernize* and strips them if the fast-path is disabled. Needs
            pillow-jxl-plugin in the env (bundled via requirements.txt on
            first-time setup; setup.js logs codec availability). */}
        <SectionHeader>Modernize Images (JXL/AVIF)</SectionHeader>
        <p className="text-[10px] text-muted-foreground -mt-1 mb-2 leading-snug">
          Re-encode JPEG/PNG pages to <span className="font-mono">JXL</span>{" "}
          (grayscale line art) or <span className="font-mono">AVIF</span>{" "}
          (color) before packaging — visually-lossless storage savings for a
          reader that decodes them. Per-page choice; already-efficient
          WebP/AVIF/JXL pages are left untouched, and a page is only replaced
          when the new file actually comes out smaller (never bloats). CBZ only
          — rides the byte-passthrough fast-path, so it pairs with Komikku
          output and keeps the source resolution.
        </p>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <Switch
              checked={!!local.defaults.modernize && modernizeAllowedDefault}
              onCheckedChange={(v) => {
                if (!modernizeAllowedDefault) return;
                if (v) {
                  // Enable + auto-correct the fast-path conditions modernize
                  // requires (aio-dl.py hard-errors otherwise). The quality /
                  // scaling sliders are moot under modernize anyway — it
                  // replaces the page bytes with JXL/AVIF, so a prior re-encode
                  // would just be wasted generation loss. Mirrors the
                  // format=none → keepImages auto-enable above.
                  setLocal((prev) => ({
                    ...prev,
                    defaults: {
                      ...prev.defaults,
                      modernize: true,
                      quality: 100,
                      scaling: 100,
                      cbzPreserveOriginals: true,
                      noProcessing: false,
                    },
                  }));
                } else {
                  setDefault("modernize", false);
                }
              }}
              disabled={!modernizeAllowedDefault}
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label className={cn("text-xs cursor-pointer", !modernizeAllowedDefault && "opacity-40")}>
                Transcode pages to JXL / AVIF
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Applies to every CBZ download — direct URL, search-initiated,
                and library re-downloads. Idempotent: re-running over an
                already-modernized series skips pages that are already JXL/AVIF.
              </p>
              {!modernizeAllowedDefault && (
                <p className="text-[10px] text-yellow-500 dark:text-yellow-400 mt-1 leading-snug">
                  Unavailable while the default format is{" "}
                  <span className="font-mono">{(local.defaults.format || "").toUpperCase()}</span>{" "}
                  — modernize writes JXL/AVIF pages that only survive CBZ output.
                  Switch the default format to CBZ (or enable Komikku) to use it.
                </p>
              )}
            </div>
          </div>
          {local.defaults.modernize && modernizeAllowedDefault && (
            <div className="pl-12 animate-slide-up space-y-3">
              {modernizeConflicts && (
                <p className="text-[10px] text-yellow-500 dark:text-yellow-400 leading-snug">
                  Heads up — modernize needs the CBZ byte-passthrough fast-path,
                  but your current defaults disable it:
                  {(local.defaults.quality ?? 100) < 100 && (
                    <> <span className="font-mono">Quality&nbsp;{local.defaults.quality}</span></>
                  )}
                  {(local.defaults.scaling ?? 100) < 100 && (
                    <> <span className="font-mono">Scaling&nbsp;{local.defaults.scaling}%</span></>
                  )}
                  {local.defaults.cbzPreserveOriginals === false && (
                    <> <span className="font-mono">preserve-originals&nbsp;off</span></>
                  )}
                  {local.defaults.noProcessing === true && (
                    <> <span className="font-mono">no-processing&nbsp;on</span></>
                  )}
                  . The downloader will skip modernize until these are reset
                  (Quality 100, Scaling 100%, CBZ preserve-originals on,
                  No&nbsp;processing off).
                </p>
              )}
              <div>
                <Label className="text-xs">Codec routing</Label>
                <Select
                  value={local.defaults.modernizeFormat ?? "auto"}
                  onChange={(e) => setDefault("modernizeFormat", e.target.value)}
                  className="mt-1"
                >
                  <option value="auto">Auto — JXL for B&amp;W, AVIF for color</option>
                  <option value="jxl">JXL only</option>
                  <option value="avif">AVIF only</option>
                  <option value="jxl+avif">JXL + AVIF — encode both, keep smaller</option>
                </Select>
                <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                  <span className="font-mono">Auto</span> decides per page (the
                  right call for mixed libraries). Oversized pages
                  (&gt; 8192&nbsp;px) always use JXL, except under AVIF-only
                  where they're left untouched.
                </p>
              </div>
              <div className="grid grid-cols-2 gap-x-6 gap-y-3">
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <Label className="text-xs">JXL distance (B&amp;W)</Label>
                    <Badge variant="secondary" className="font-mono tabular-nums">
                      {(local.defaults.modernizeDistance ?? 1.0) === 0
                        ? "lossless"
                        : (local.defaults.modernizeDistance ?? 1.0).toFixed(1)}
                    </Badge>
                  </div>
                  <Slider
                    value={local.defaults.modernizeDistance ?? 1.0}
                    onValueChange={(v) => setDefault("modernizeDistance", v)}
                    min={0}
                    max={3}
                    step={0.1}
                  />
                  <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                    Butteraugli distance for grayscale → JXL.{" "}
                    <span className="font-mono">1.0</span> = visually lossless
                    (default); lower = larger/closer to source;{" "}
                    <span className="font-mono">0.0</span> = mathematically
                    lossless (much larger; only wins on PNG sources).
                  </p>
                </div>
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <Label className="text-xs">AVIF quality (color)</Label>
                    <Badge variant="secondary" className="font-mono tabular-nums">
                      {local.defaults.modernizeQuality ?? 90}
                    </Badge>
                  </div>
                  <Slider
                    value={local.defaults.modernizeQuality ?? 90}
                    onValueChange={(v) => setDefault("modernizeQuality", v)}
                    min={1}
                    max={100}
                  />
                  <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                    AVIF quality for color pages.{" "}
                    <span className="font-mono">90</span> = visually lossless
                    (default); <span className="font-mono">85</span> =
                    aggressive (smaller; artifacts only under pixel-peeping).
                  </p>
                </div>
              </div>
              <div>
                <div className="flex items-center justify-between mb-1">
                  <Label className="text-xs">Minimum saving to replace a page</Label>
                  <Badge variant="secondary" className="font-mono tabular-nums">
                    ≥{Math.round((1 - (local.defaults.modernizeMinSaving ?? 0.92)) * 100)}%
                  </Badge>
                </div>
                <Slider
                  value={local.defaults.modernizeMinSaving ?? 0.92}
                  onValueChange={(v) => setDefault("modernizeMinSaving", v)}
                  min={0.5}
                  max={1}
                  step={0.01}
                />
                <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                  A transcoded page is kept only if it's at least this much
                  smaller than the original — otherwise the original bytes stay.
                  Default <span className="font-mono">≥8%</span>{" "}
                  (<span className="font-mono">min-saving 0.92</span>) skips
                  already-dense pages so the archive never grows.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Metadata Enrichment ────────────────────────────────────
            Opt-in global setting — when on, every download spawn injects
            --metadata-source anilist into the CLI args. useDownloader.
            queueDownload reads from settingsRef.current and adds the
            flag for every electronAPI.startDownload, so the toggle
            applies app-wide (New / Search / Library / queue). The two
            sub-options (tag rank floor, force-refresh) only emit on
            spawn when source !== "none" AND the value differs from
            Python argparse defaults, keeping the spawn line clean.
            Python side: aio-dl.py near --enable-ml-rating registers the
            three flags; sites/external_metadata.py runs the AniList
            GraphQL client. Cross-file: useDownloader.queueDownload
            injection + electron/downloader.js:buildCliArgs:flagMap+boolMap
            CLI translation. */}
        <SectionHeader>Metadata Enrichment</SectionHeader>
        <p className="text-[10px] text-muted-foreground -mt-1 mb-2 leading-snug">
          Pull normalized tags, descriptions, and country/format from the free{" "}
          <a
            href="https://anilist.co"
            target="_blank"
            rel="noopener noreferrer"
            className="font-mono underline decoration-dotted underline-offset-2 hover:text-foreground"
          >
            AniList GraphQL API
          </a>{" "}
          (90 req/min, no auth, no account). Results merge into{" "}
          <span className="font-mono">ComicInfo.xml</span> and{" "}
          <span className="font-mono">.aio_series.json</span> on every download —
          ID-cached so resume / update runs do a single fetch-by-id instead of
          re-searching. Off by default; opt in if you want filterable, ranked,
          spoiler-aware tags in your reader.
        </p>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <Switch
              checked={local.metadataSource === "anilist"}
              onCheckedChange={(v) => set("metadataSource", v ? "anilist" : "none")}
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">
                Enable AniList enrichment
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Adds 1 GraphQL round-trip per series (first download) or 1
                fetch-by-id (cached afterwards). Failures are non-fatal — the
                download continues with site-only metadata and logs a single
                warning line.
              </p>
            </div>
          </div>
          {local.metadataSource === "anilist" && (
            <div className="pl-12 animate-slide-up space-y-3">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <Label className="text-xs">Tag relevance threshold</Label>
                  <Badge variant="secondary" className="font-mono tabular-nums">
                    {local.metadataTagMinRank ?? 50}
                  </Badge>
                </div>
                <Slider
                  value={local.metadataTagMinRank ?? 50}
                  onValueChange={(v) => set("metadataTagMinRank", v)}
                  min={0}
                  max={100}
                  step={5}
                />
                <div className="flex justify-between mt-1 px-0.5">
                  <span className="text-[10px] text-muted-foreground">
                    0 — every tag
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    50 — default
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    100 — only top-rank
                  </span>
                </div>
                <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                  AniList scores each tag <span className="font-mono">0–100</span> by
                  relevance. Tags below this floor are dropped from{" "}
                  <span className="font-mono">{"<Tags>"}</span> /{" "}
                  <span className="font-mono">{"<SpoilerTags>"}</span> /{" "}
                  <span className="font-mono">{"<TagsExtended>"}</span> in the
                  ComicInfo.xml. At <span className="font-mono">50</span> a typical
                  manga has ~15–25 tags; at <span className="font-mono">80</span>{" "}
                  ~3–6 tags.
                </p>
              </div>
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1">
                  <Label className="text-xs cursor-pointer">
                    Always re-fetch (skip the AniList ID cache)
                  </Label>
                  <p className="text-[10px] text-muted-foreground mt-0.5">
                    Force a fresh AniList lookup on every download — even when
                    an AniList ID is already cached in{" "}
                    <span className="font-mono">.aio_series.json</span>. Useful
                    when backfilling a library or after AniList re-tags a
                    series upstream. Costs one extra round-trip per download;
                    leave off unless you specifically need fresh data.
                  </p>
                </div>
                <Switch
                  checked={!!local.metadataRefresh}
                  onCheckedChange={(v) => set("metadataRefresh", v)}
                />
              </div>
            </div>
          )}
        </div>

        {/* Default Toggles */}
        <SectionHeader>Default Toggles</SectionHeader>
        <div className="grid grid-cols-2 gap-3">
          {[
            ["keepChapters", "Keep chapters"],
            ["noFinalFile", "No final file"],
            ["keepImages", "Keep images"],
            ["noProcessing", "No processing"],
            ["noCleanup", "No cleanup"],
          ].map(([key, label]) => (
            <div key={key} className="flex items-center gap-2">
              <Checkbox
                checked={local.defaults[key]}
                onCheckedChange={(v) => setDefault(key, v)}
              />
              <Label className="text-xs cursor-pointer">{label}</Label>
            </div>
          ))}
        </div>

        {/* Default Multi-source Fallback ─────────────────────────────
            DownloadTab's useEffect spreads settings.defaults onto its form on
            mount, so toggling here changes the New tab's default state for
            every new download. Per-job overrides in DownloadTab don't write
            back to settings — that's the same pattern as format/quality. */}
        <SectionHeader>Default Multi-source Fallback</SectionHeader>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <Switch
              checked={!!local.defaults.multiSource}
              onCheckedChange={(v) => setDefault("multiSource", v)}
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">
                Use alternate sources when the primary fails
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Adds ~30-60s of cross-site discovery before each download.
                When the primary CDN throttles or 404s a page, the chapter
                falls over to the next source automatically.
              </p>
            </div>
          </div>
          {local.defaults.multiSource && (
            <div className="pl-12 animate-slide-up">
              <div className="flex items-center justify-between mb-1">
                <Label className="text-xs">Alternative quality floor</Label>
                <Badge variant="secondary" className="font-mono tabular-nums">
                  {(local.defaults.multiSourceQualityMin ?? 0.65).toFixed(2)}
                </Badge>
              </div>
              <Slider
                value={local.defaults.multiSourceQualityMin ?? 0.65}
                onValueChange={(v) => setDefault("multiSourceQualityMin", v)}
                min={0.3}
                max={0.95}
                step={0.05}
              />
              <p className="text-[10px] text-muted-foreground mt-1">
                Sources below this seed/measured quality won't be used as
                fallbacks. Default 0.65 keeps unknown-language Madara extras out.
              </p>
            </div>
          )}
        </div>

        {/* Default Chapter Behavior — moved 2026-05-08 from "Default Search
            Options" because it now affects download behavior, not just the
            search-display "X main / Y entries" diagnostic. The same toggle
            also drives sites/chapter_merger.py:group_chapters_for_download
            on the Python side. SearchTab's inline toggle reads/writes the
            same settings.collapseSplits, so changing one updates both. */}
        <SectionHeader>Default Chapter Behavior</SectionHeader>
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">Collapse split chapters</Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                When sources split chapter 1 into 1.1/1.2/1.3/1.4 (no integer 1),
                combine them into a single Chapter 1 file. When integer 1 exists
                alongside the splits, they're treated as redundant duplicates and
                dropped. True partials like 1.5 alongside Ch 1 are preserved.
                Affects both downloads and the search-coverage display. Turn off
                for series that legitimately use decimal numbering.
              </p>
            </div>
            <Switch
              checked={local.collapseSplits !== false}
              onCheckedChange={(v) => set("collapseSplits", v)}
            />
          </div>

          {/* Inter-chapter image prefetch — Phase G7. */}
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">Prefetch workers for next chapter</Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Background workers that download chapter N+1's images while
                chapter N is encoding. <span className="font-mono">-1</span> = match Image Workers (default). <span className="font-mono">0</span> =
                disable prefetch entirely. Positive number = exact worker count. Drop to
                4 (or 0) when the upstream CDN is rate-limiting (Cloudflare 5xx storms)
                — the extra concurrent burst from N+1 can compound throttling. Typically
                saves 2-5s per chapter on MangaFire-style long-strip encodes when on.
              </p>
            </div>
            <Input
              type="number"
              min={-1}
              max={32}
              step={1}
              value={local.prefetchImageWorkers ?? -1}
              onChange={(e) => {
                // The Python --prefetch-image-workers flag is argparse
                // type=int. A decimal value here (e.g. user typing 3.7 in
                // the spinner) would round-trip to settings.json and crash
                // the next spawn with "invalid int". Truncate to integer
                // and clamp to the input range.
                const raw = e.target.value;
                if (raw === "" || raw === "-") {
                  set("prefetchImageWorkers", -1);
                  return;
                }
                const parsed = Number(raw);
                if (!Number.isFinite(parsed)) {
                  set("prefetchImageWorkers", -1);
                  return;
                }
                const truncated = Math.trunc(parsed);
                const clamped = Math.max(-1, Math.min(32, truncated));
                set("prefetchImageWorkers", clamped);
              }}
              className="w-20 shrink-0 font-mono tabular-nums"
            />
          </div>
        </div>

        {/* Default Network */}
        <SectionHeader>Default Network Settings</SectionHeader>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <Label className="text-xs">Image Workers</Label>
            <Input
              type="number"
              min={1}
              max={10}
              step={1}
              value={local.defaults.imageWorkers}
              onChange={(e) => {
                // Number("") returns 0; argparse would crash on 0 for
                // a "min=1" field. Truncate to int and clamp into the
                // [min, max] range; fall back to a sensible default
                // when the parse can't yield a finite value.
                const v = Number(e.target.value);
                if (!Number.isFinite(v)) { setDefault("imageWorkers", 3); return; }
                setDefault("imageWorkers", Math.min(10, Math.max(1, Math.trunc(v))));
              }}
              className="mt-1"
            />
          </div>
          <div>
            <Label className="text-xs">HTTP Timeout</Label>
            <Input
              type="number"
              min={5}
              step={1}
              value={local.defaults.httpTimeout}
              onChange={(e) => {
                const v = Number(e.target.value);
                if (!Number.isFinite(v) || v < 5) { setDefault("httpTimeout", 30); return; }
                setDefault("httpTimeout", Math.trunc(v));
              }}
              className="mt-1"
            />
          </div>
          <div>
            <Label className="text-xs">Max Retries</Label>
            <Input
              type="number"
              min={0}
              step={1}
              value={local.defaults.httpMaxRetries}
              onChange={(e) => {
                const v = Number(e.target.value);
                if (!Number.isFinite(v) || v < 0) { setDefault("httpMaxRetries", 6); return; }
                setDefault("httpMaxRetries", Math.trunc(v));
              }}
              className="mt-1"
            />
          </div>
        </div>

        {/* Image Prefetch & Concurrency (generalized 2026-05-13) ──
            Apply to any handler with SUPPORTS_FAST_DOWNLOAD=True (currently
            mangafire and linewebtoon; see sites/base.py:fast_download_images).
            curl_cffi async + HTTP/2 multiplex over a single keep-alive TLS
            session. Bench (MangaFire 83-page chapter): cloudscraper@3 =
            10.20s -> curl_cffi@8 = 6.04s (1.69x). LINE Webtoon and any
            future fast-download handler benefits the same way. */}
        <SectionHeader>Image Prefetch & Concurrency</SectionHeader>
        <p className="text-[10px] text-muted-foreground -mt-1 mb-2">
          Tuning for the curl_cffi fast image-download path (used by MangaFire
          and LINE Webtoon today). Auto-dials concurrency down per-host on
          CDN errors. Sites without fast-download support still use Image
          Workers above.
        </p>
        <div className="space-y-3">
          {/* Image concurrency for the curl_cffi async fetcher. */}
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">Image concurrency</Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Concurrent in-flight image fetches via HTTP/2 multiplex. Default
                {" "}<span className="font-mono">8</span> hits ~5 MB/s — typical home-network ceiling.
                Past <span className="font-mono">12</span> is diminishing returns. Drop to
                {" "}<span className="font-mono">3</span> if a CDN starts rate-limiting (rare on
                cookieless edge caches, but defensive). Auto-dials down on
                rate-limit / 5xx errors during a download.
              </p>
            </div>
            <Input
              type="number"
              min={1}
              max={32}
              step={1}
              value={local.imageConcurrency ?? 8}
              onChange={(e) => {
                // Int-parse + clamp pattern matches imageWorkers above.
                // Python's --image-concurrency is argparse type=int; a
                // decimal here would crash the next spawn.
                const raw = e.target.value;
                if (raw === "") { set("imageConcurrency", 8); return; }
                const v = Number(raw);
                if (!Number.isFinite(v)) { set("imageConcurrency", 8); return; }
                set("imageConcurrency", Math.max(1, Math.min(32, Math.trunc(v))));
              }}
              className="w-20 shrink-0 font-mono tabular-nums"
            />
          </div>

          {/* Image prefetch depth. 0 disables prefetch entirely. */}
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">Prefetch depth</Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                How many chapters ahead to keep queued for image prefetch.
                Default <span className="font-mono">2</span> means ~one extra chapter
                buffered while the main loop processes the current one.
                Higher helps when main-loop work is fast vs network download
                (e.g. CBZ fast-path on LINE Webtoon).
                {" "}<span className="font-mono">0</span> disables prefetch entirely.
              </p>
            </div>
            <Input
              type="number"
              min={0}
              max={8}
              step={1}
              value={local.imagePrefetchDepth ?? 2}
              onChange={(e) => {
                const raw = e.target.value;
                if (raw === "") { set("imagePrefetchDepth", 2); return; }
                const v = Number(raw);
                if (!Number.isFinite(v) || v < 0) { set("imagePrefetchDepth", 2); return; }
                set("imagePrefetchDepth", Math.max(0, Math.min(8, Math.trunc(v))));
              }}
              className="w-20 shrink-0 font-mono tabular-nums"
            />
          </div>

          {/* Concurrent prefetch worker threads. */}
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">Prefetch workers in parallel</Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Concurrent prefetch worker threads. Default
                {" "}<span className="font-mono">2</span> = up to 2 chapters downloading at
                once while the main thread processes a third. <span className="font-mono">1</span> =
                legacy single-in-flight behavior. Total concurrent connections
                per host ≈ this × image concurrency. Webtoons.com and
                MangaFire's edge cache tolerate 2 well in practice.
              </p>
            </div>
            <Input
              type="number"
              min={1}
              max={4}
              step={1}
              value={local.imagePrefetchParallel ?? 2}
              onChange={(e) => {
                const raw = e.target.value;
                if (raw === "") { set("imagePrefetchParallel", 2); return; }
                const v = Number(raw);
                if (!Number.isFinite(v) || v < 1) { set("imagePrefetchParallel", 2); return; }
                set("imagePrefetchParallel", Math.max(1, Math.min(4, Math.trunc(v))));
              }}
              className="w-20 shrink-0 font-mono tabular-nums"
            />
          </div>

          {/* Force-disable curl_cffi escape hatch. */}
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">
                Force-disable fast download path
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Escape hatch — when on, ALL handlers fall back to the legacy
                ThreadPoolExecutor + cloudscraper path regardless of their
                per-handler SUPPORTS_FAST_DOWNLOAD flag. Useful for curl_cffi
                version regressions or weird CDN-vs-impersonation issues.
                Off by default; only flip on when troubleshooting.
              </p>
            </div>
            <Switch
              checked={!!local.noFastDownload}
              onCheckedChange={(v) => set("noFastDownload", v)}
            />
          </div>
        </div>

        {/* MangaFire VRF capture knobs (--mangafire-vrf-prefetch-depth,
            --mangafire-vrf-parallel) were removed from the UI on
            2026-05-13. They're advanced Patchright/Cloudflare tuning
            most users shouldn't touch; the argparse defaults (depth=4,
            parallel=1) are bench-good. Advanced users can still pass
            the CLI flags directly. */}

        {/* LINE Webtoon WebP Recompression (Phase 1, 2026-05-11) ──
            webtoons.com-only image recompression. Targets the ~45GB-per-
            series problem caused by archival-quality lossless PNGs on
            newer chapters (verified: Eleceed Ch 57 → 91% smaller at q85).
            Python-side `handler.name === "linewebtoon"` gates the actual
            re-encode pass, so these defaults are safe to enable for users
            with mixed-site libraries — non-webtoons downloads silently
            skip the recompression pass. App.jsx's settings.defaults spread
            for search/library wrappers (:155, :192) AND DownloadTab's
            DEFAULT_FORM merge (:110-113) both pick these up. */}
        <SectionHeader>LINE Webtoon Recompression</SectionHeader>
        <p className="text-[10px] text-muted-foreground -mt-1 mb-2 leading-snug">
          Re-encode webtoons.com <em className="not-italic font-semibold">lossless PNG</em> pages
          to lossy WebP before packaging — only fires when the active handler is{" "}
          <span className="font-mono">linewebtoon</span>, silently ignored for
          every other site. Skips JPEG-served chapters automatically
          (webtoons.com only ships PNG once a series gets popular — Eleceed
          flips at Ch 57; recompressing the small early JPEGs would be
          generation-loss for ~50 KB of savings). Typical impact on a
          PNG-heavy series: 45 GB → ~5 GB at q85. Requires CBZ or EPUB output;
          PDF is rejected at startup.
        </p>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <Switch
              checked={!!local.defaults.webtoonRecompress && recompressAllowedDefault}
              onCheckedChange={(v) => recompressAllowedDefault && setDefault("webtoonRecompress", v)}
              disabled={!recompressAllowedDefault}
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label className={cn("text-xs cursor-pointer", !recompressAllowedDefault && "opacity-40")}>
                Recompress webtoons.com pages to WebP
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Applies to every webtoons.com download — direct URL, search-
                initiated, and library re-downloads.
              </p>
              {!recompressAllowedDefault && (
                <p className="text-[10px] text-yellow-500 dark:text-yellow-400 mt-1 leading-snug">
                  Unavailable while the default format is{" "}
                  <span className="font-mono">{(local.defaults.format || "").toUpperCase()}</span>{" "}
                  — recompression needs CBZ or EPUB output. Change the default
                  format above (or enable Komikku) to use it.
                </p>
              )}
            </div>
          </div>
          {local.defaults.webtoonRecompress && recompressAllowedDefault && (
            <div className="pl-12 animate-slide-up grid grid-cols-2 gap-x-6 gap-y-3">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <Label className="text-xs">Quality</Label>
                  <Badge variant="secondary" className="font-mono tabular-nums">
                    {local.defaults.webtoonRecompressQuality ?? 85}
                  </Badge>
                </div>
                <Slider
                  value={local.defaults.webtoonRecompressQuality ?? 85}
                  onValueChange={(v) => setDefault("webtoonRecompressQuality", v)}
                  min={1}
                  max={100}
                />
                <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                  <span className="font-mono">85</span> = storage-optimized
                  (default). <span className="font-mono">90</span> =
                  archival-safe (~60% larger files). Above 95 is wasted
                  bytes on color webtoon content.
                </p>
              </div>
              <div>
                <div className="flex items-center justify-between mb-1">
                  <Label className="text-xs">Encoder effort</Label>
                  <Badge variant="secondary" className="font-mono tabular-nums">
                    {local.defaults.webtoonRecompressMethod ?? 4}
                  </Badge>
                </div>
                <Slider
                  value={local.defaults.webtoonRecompressMethod ?? 4}
                  onValueChange={(v) => setDefault("webtoonRecompressMethod", v)}
                  min={0}
                  max={6}
                />
                <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                  <span className="font-mono">0</span> = fastest,{" "}
                  <span className="font-mono">6</span> = smallest. Default
                  4 is the sweet spot; 6 buys ~5% smaller files at ~2-3×
                  the encode time — fine for overnight bulk runs.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Default Search Options ─────────────────────────────────
            Same settings.searchOpts namespace SearchTab reads/writes from.
            Surfacing them here gives the user one central place; SearchTab's
            inline toggles still update settings.searchOpts on every change,
            so the two surfaces stay in sync. */}
        <SectionHeader>Default Search Options</SectionHeader>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3">
          <div>
            <Label className="text-xs">Search language</Label>
            <Select
              value={local.searchOpts?.searchLanguage ?? "en"}
              onChange={(e) => setSearchOpt("searchLanguage", e.target.value)}
              className="mt-1"
            >
              {LANGUAGES.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </Select>
          </div>
          <div className="flex items-center justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">Curated sites only</Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                ~3× faster; skips long-tail aggregators
              </p>
            </div>
            <Switch
              checked={!!local.searchOpts?.seededOnly}
              onCheckedChange={(v) => setSearchOpt("seededOnly", v)}
            />
          </div>
          <div className="flex items-center justify-between gap-3">
            <div className="flex-1">
              <Label className="text-xs cursor-pointer">Multi-source by default</Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Pre-fetch alternative sources when searching
              </p>
            </div>
            <Switch
              checked={!!local.searchOpts?.multiSource}
              onCheckedChange={(v) => setSearchOpt("multiSource", v)}
            />
          </div>
          {local.searchOpts?.multiSource && (
            <div className="col-span-2 animate-slide-up">
              <div className="flex items-center justify-between mb-1">
                <Label className="text-xs">Search alternative quality floor</Label>
                <Badge variant="secondary" className="font-mono tabular-nums">
                  {(local.searchOpts?.multiSourceQualityMin ?? 0.65).toFixed(2)}
                </Badge>
              </div>
              <Slider
                value={local.searchOpts?.multiSourceQualityMin ?? 0.65}
                onValueChange={(v) => setSearchOpt("multiSourceQualityMin", v)}
                min={0.3}
                max={0.95}
                step={0.05}
              />
            </div>
          )}
          <div>
            <Label className="text-xs">Per-site timeout (s)</Label>
            <Input
              type="number"
              min={5}
              max={60}
              value={local.searchOpts?.searchTimeout ?? 20}
              onChange={(e) => setSearchOpt("searchTimeout", Number(e.target.value) || 20)}
              className="mt-1"
            />
          </div>
          <div>
            <Label className="text-xs">Min title-match</Label>
            <Input
              type="number"
              min={0.3}
              max={1.0}
              step={0.05}
              value={local.searchOpts?.searchMinMatch ?? 0.55}
              onChange={(e) => setSearchOpt("searchMinMatch", Number(e.target.value) || 0.55)}
              className="mt-1 font-mono"
            />
          </div>
          <div>
            <Label className="text-xs">Parallel sites</Label>
            <Input
              type="number"
              min={1}
              max={16}
              value={local.searchOpts?.searchParallelism ?? 6}
              onChange={(e) => setSearchOpt("searchParallelism", Number(e.target.value) || 6)}
              className="mt-1"
            />
          </div>
        </div>

        {/* Library */}
        <SectionHeader>Library</SectionHeader>
        <div className="flex items-center gap-2">
          <Checkbox
            checked={local.useFileBasedChapterCheck ?? false}
            onCheckedChange={(v) => set("useFileBasedChapterCheck", v)}
          />
          <Label className="text-xs cursor-pointer">
            Check chapters against files on device (instead of download history)
          </Label>
        </div>
        <p className="text-[10px] text-muted-foreground mt-1 ml-6">
          <strong>Off (default):</strong> Uses the saved download history (.aio_series.json) to
          know which chapters you have. Faster, but won't notice deleted files.
          <br />
          <strong>On:</strong> Scans your actual files and extracts chapter numbers from filenames.
          Catches missing or deleted files, but only works with individual chapter files
          or combined files with chapter ranges in the name.
        </p>

        {/* ── Check All — include "Completed" series ──
            Aggregators (mangafire most notoriously) routinely mis-label
            ongoing series as "Completed". Defaulting this ON gives a
            forgiving scan that catches mislabeled series; opt out only if
            your library lives on reliable status sources (MangaDex etc.)
            AND you want to save a few seconds per scan. */}
        <div className="flex items-center gap-2 mt-4">
          <Checkbox
            checked={local.checkAllIncludeCompleted !== false}
            onCheckedChange={(v) => set("checkAllIncludeCompleted", v)}
          />
          <Label className="text-xs cursor-pointer">
            Check "Completed" series too (recommended)
          </Label>
        </div>
        <p className="text-[10px] text-muted-foreground mt-1 ml-6">
          <strong>On (default):</strong> "Check All" scans every series with a saved URL, regardless
          of status. Catches the very common case where a site (mangafire, etc.) wrongly
          marks an ongoing series as Completed.
          <br />
          <strong>Off:</strong> Only scans series whose status is Ongoing or Releasing.
          Faster, but misses mislabeled series.
        </p>

        {/* ── Check All — parallel worker count ──
            Capped to [1, 8] in main.js. 4 is the safe sweet spot for typical
            libraries; the provider-aware scheduler fans across distinct
            sites at that count. Drop to 2 only if your library hits one
            site heavily and you see throttling; bump to 6-8 only if scans
            consistently bottleneck on a single site's per-request latency. */}
        <div className="flex items-center gap-3 mt-4">
          <Label className="text-xs whitespace-nowrap">
            Parallel checks
          </Label>
          <Input
            type="number"
            min={1}
            max={8}
            value={local.checkAllConcurrency ?? 4}
            onChange={(e) =>
              set(
                "checkAllConcurrency",
                Math.max(1, Math.min(8, Number(e.target.value) || 4))
              )
            }
            className="w-20 font-mono"
          />
          <Badge variant="secondary" className="text-[10px]">
            1–8
          </Badge>
        </div>
        <p className="text-[10px] text-muted-foreground mt-1">
          How many series the "Check All" sweep checks at the same time. Workers prefer
          jobs on different sites so no single CDN gets hammered. Default 4.
        </p>

        {/* ── Update-check downloads: skip image-quality probe ──
            Multi-source ranks alternatives by running an image-quality probe
            (download sample images, score them). On MangaFire-class handlers
            the probe burns ~30-60+ s per series. For a 1-5 chapter update
            delta that probe cost dwarfs the actual download. With this on
            (default), the multi-source picker uses sites/quality_seed.json
            priors as the ranking signal instead of running the probe. The
            picker's tiebreaker logic ALREADY falls back to the seed when
            probe scores are equal, so the seed-only mode is a "trust the
            curated priors" shortcut. Only affects downloads queued from
            the UpdatesCenter; regular New-tab downloads still get full
            probe accuracy. */}
        <div className="flex items-center gap-2 mt-4">
          <Checkbox
            checked={local.updateChecksUseSeededRating !== false}
            onCheckedChange={(v) => set("updateChecksUseSeededRating", v)}
          />
          <Label className="text-xs cursor-pointer">
            Update-check downloads use fast seed-based rating (recommended)
          </Label>
        </div>
        <p className="text-[10px] text-muted-foreground mt-1 ml-6">
          <strong>On (default):</strong> Downloads queued from the Updates Center skip the
          multi-source image-quality probe (which runs Playwright + per-source image scoring
          and adds ~30-60+ seconds per series). Ranking falls back to the curated quality
          priors in <code className="font-mono text-[9px]">sites/quality_seed.json</code>.
          For a typical 1-5 chapter update delta the probe takes ~10× longer than the
          actual download.
          <br />
          <strong>Off:</strong> Update-check downloads run the full probe like any other
          multi-source download. More accurate per-source ranking; significantly slower.
        </p>

        {/* Verbose */}
        <SectionHeader>Logging</SectionHeader>
        <div className="flex items-center gap-2">
          <Checkbox
            checked={local.verboseAlways}
            onCheckedChange={(v) => set("verboseAlways", v)}
          />
          <Label className="text-xs cursor-pointer">
            Always use verbose mode (--verbose flag on every download)
          </Label>
        </div>

        {/* Log update interval */}
        <div className="mt-3">
          <div className="flex items-center justify-between mb-1">
            <Label className="text-xs">Log Update Speed</Label>
            <Badge variant="secondary">{local.logUpdateInterval}ms</Badge>
          </div>
          <Slider
            value={local.logUpdateInterval}
            onValueChange={(v) => set("logUpdateInterval", v)}
            min={50}
            max={2000}
            step={50}
          />
          <div className="flex justify-between mt-1 px-0.5">
            <span className="text-[10px] text-muted-foreground">50ms (fastest)</span>
            <span className="text-[10px] text-muted-foreground">2000ms (lightest)</span>
          </div>
          <p className="text-[10px] text-muted-foreground mt-1">
            How often logs and progress bars refresh. Lower = more responsive, higher = less CPU.
          </p>
        </div>
      </div>

      {/* Save/Reset buttons. SaveSettingsButton (defined above) carries
          its own dirty-state visual + post-save sweep + error branch;
          the Reset stays a plain outline Button. */}
      <div className="flex-shrink-0 p-4 border-t bg-background/80 backdrop-blur-sm flex gap-2">
        <SaveSettingsButton dirty={dirty} onSave={handleSave} />
        <Button variant="outline" onClick={handleReset}>
          <RotateCcw className="w-4 h-4" />
        </Button>
      </div>
    </div>
  );
}
