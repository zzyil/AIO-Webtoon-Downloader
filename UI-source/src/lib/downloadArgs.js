// Pure download-args builders shared by the UI and lib/queueRecipe.js.
//   - buildLibraryDownloadArgs: library / update-check downloads (below).
//   - searchDialogInitial / searchDialogArgs / confirmSearchArgs: the Search
//     tab's "Download settings" dialog (end of file).
// queueRecipe.js re-runs these against the settings in force when a queued
// item STARTS, so a Settings save reaches items that are still waiting.
//
// Shared builder for library/update-check download argument dicts.
//
// Both LibraryTab surfaces that queue an update download — the per-series
// detail view ("Download Missing Chapters", UpdateSection.handleDownloadNew)
// and the Updates Center per-row queue (buildDownloadArgsForRow) — assemble
// the SAME args object from the series metadata + the user's saved
// settings.defaults.
//
// LibraryTab no longer calls this directly: it queues a "library" recipe and
// lib/queueRecipe.js calls this at spawn, spreading settings.defaults UNDER the
// result. The args are mapped to CLI flags in electron/downloader.js:buildCliArgs.
// Flag names / conditional-inclusion here must stay byte-identical to what
// those call sites emitted before extraction.
//
// Update-check downloads FORCE lazy multi-source. Every download built here
// comes from a "check for updates" flow, where the delta is typically 1-2
// chapters — far shorter than the ~30-80 s eager cross-site alternatives
// discovery that plain --multi-source runs before the first chapter. So when
// the user has --multi-source on in settings (def.multiSource === true) we set
// multiSourceLazy:true here, which OVERRIDES a global lazy opt-out
// (settings.defaults.multiSourceLazy === false): this builder's result wins
// over the settings.defaults spread (queueRecipe.js kind "library"), and
// downloader.js's chokepoint emits --multi-source-lazy whenever multiSource is
// on and multiSourceLazy !== false. Normal New-tab / Search downloads never
// route through here, so they keep honoring the global lazy toggle unchanged.
// Python side: aio-dl.py --multi-source-lazy / _ms_lazy_pending.
//
// Params:
//   meta   — the series' .aio_series.json metadata (seriesMeta). May be {}.
//   d      — settings.defaults (the download-defaults dict). May be {}.
//   chaptersStr — the chapter range string (from chaptersToRangeString).
//   verboseAlways — settings.verboseAlways (the raw value; `?? true` applied here).
//   folderPath — the series folder this download belongs to, forwarded as
//     --series-dir. Optional, and only load-bearing when a series is FORKED
//     across two folders (a site rename used to create those; grep
//     seriesIdentityKey): the update-check row reports under the folder
//     holding the bulk of the series, and without this the engine would
//     re-derive the target itself and could pick the other one. Python
//     validates it and falls back to its own identity lookup when the path is
//     stale, so passing a folder that has since been merged away is harmless.
//
// NOTE the XF-2 quality default of 100 (NOT 85): any --quality < 100 flips the
// child's _user_set_quality True and disables the CBZ byte-passthrough
// fast-path (silent lossy re-encode). Keep it 100.
//
// KIND ("chapter" default | "volume"). A volume download (MangaFire native
// volumes) passes the series' VOLUME format/language in `meta` (libraryRecipe
// reads them from the ledger's "volumes" block) and sets downloadVolumes:true.
// A chapter build omits the key, so its args stay identical to before; the
// recipe strips any downloadVolumes from the settings.defaults spread
// (queueRecipe.js kind "library"), so a stray saved value can never flip a
// chapter update into a volume run. It refuses (throws) while Komikku is on:
// aio-dl.py's --komikku coerces --download-volumes OFF, which would turn
// "volumes 1-3" into "chapters 1-3" without anyone noticing. A queued item
// that throws here is dropped by useDownloader (grep _resolveQueueArgs).
//
// KEEP-CHAPTERS IS DELIBERATELY *NOT* SPECIAL-CASED HERE, and that is worth a
// note because it briefly was. Every download this builder produces is a DELTA
// — `chapters` is only the missing range — so its chapter set does not cover
// the combined `<Title>.<fmt>` already on disk, and aio-dl.py's overwrite guard
// (grep _final_file_would_shrink) declines to rebuild that archive rather than
// truncating 53 chapters to 3. That leaves the run's chapters with nowhere
// durable to go unless --keep-chapters is on.
//
// Python fixes that in Python: the same predicate runs BEFORE the chapter loop
// and coerces keep_chapters itself, announced with one `[Delta]` line, exactly
// as --komikku coerces its three flags (grep "Forcing" in aio-dl.py). Doing it
// here instead would mean every caller of the engine has to remember to
// compensate for a decision only the engine can make — this builder AND
// core/DownloadForm.kt's Android twin, forever.
export const KOMIKKU_VOLUMES_MESSAGE =
  "Volume downloads are off while Komikku mode is on (Komikku output is per chapter).";

export function buildLibraryDownloadArgs(
  meta,
  d,
  chaptersStr,
  verboseAlways,
  folderPath,
  kind = "chapter",
) {
  const m = meta || {};
  const def = d || {};
  const isVolume = kind === "volume";
  if (isVolume && def.komikku === true) {
    throw new Error(KOMIKKU_VOLUMES_MESSAGE);
  }
  const args = {
    format: m.format || def.format || "pdf",
    quality: def.quality ?? 100,
    chapters: chaptersStr,
    language: m.language || "en",
    site: m.site || undefined,
    seriesDir: folderPath || undefined,
    verbose: verboseAlways ?? true,
  };
  if (isVolume) args.downloadVolumes = true;
  if (def.scaling && def.scaling !== 100) args.scaling = def.scaling;
  if (def.keepChapters) args.keepChapters = true;
  if (def.noFinalFile) args.noFinalFile = true;
  if (def.keepImages) args.keepImages = true;
  if (def.noProcessing) args.noProcessing = true;
  if (def.noCleanup) args.noCleanup = true;
  if (def.imageWorkers && def.imageWorkers !== 3) args.imageWorkers = def.imageWorkers;
  if (def.httpTimeout && def.httpTimeout !== 30) args.httpTimeout = def.httpTimeout;
  if (def.httpMaxRetries && def.httpMaxRetries !== 6) args.httpMaxRetries = def.httpMaxRetries;
  // Force lazy multi-source for update-check downloads (see the header note).
  // Only meaningful when --multi-source is on in settings; when it is, this
  // overrides a global multiSourceLazy:false opt-out so a 1-2 chapter update
  // never pays the ~30-80 s eager cross-site discovery. downloader.js gates
  // the actual --multi-source-lazy flag on args.multiSource, so this stays a
  // clean no-op when multi-source is off.
  // Volumes have no multi-source (aio-dl.py forces it off), so nothing to arm.
  if (def.multiSource === true && !isVolume) {
    args.multiSourceLazy = true;
  }
  return args;
}

// ── Search tab "Download settings" dialog (SearchTab.jsx:SearchDownloadOptionsDialog) ──
// Pure so lib/queueRecipe.js (kind "search") can rebuild a waiting search
// item's args from the settings in force when it STARTS. The dialog keeps only
// the fields the user changed (lib/downloadForm.js:applyEdits, baseline =
// searchDialogInitial); every other field follows Settings.

// What the dialog shows before any edit. `multiSource` is the SEARCH's own
// flag, not a Setting: the prefetched alternatives only exist for that search
// (see confirmSearchArgs), so it is job data and never follows Settings.
export function searchDialogInitial(settings, multiSource) {
  const d = settings?.defaults || {};
  return {
    format: d.format || "pdf",
    epubLayout: d.epubLayout || "vertical",
    chapters: "all",
    group: d.group || "",
    language: d.language || settings?.searchOpts?.searchLanguage || "en",
    quality: d.quality ?? 100,
    keepChapters: !!d.keepChapters,
    keepImages: !!d.keepImages,
    noFinalFile: !!d.noFinalFile,
    // Follows settings.defaults.komikku like the New tab's toggle
    // (downloadForm.js:formDefaults). Python's --komikku coerces format→cbz,
    // keep-chapters and no-final-file itself, so the dialog leaves those alone.
    komikku: !!d.komikku,
    multiSource: !!multiSource,
    // Chapters | Volumes switch (ItemKindSwitch). Job data like the New tab's
    // (downloadForm.js DEFAULT_FORM.downloadVolumes): always starts on
    // chapters, never follows Settings.
    downloadVolumes: false,
  };
}

// The dialog's form (initial + edits) → the args it contributes.
export function searchDialogArgs(form) {
  const chapters = (form.chapters || "").trim();
  const group = (form.group || "").trim();
  const args = {
    format: form.format,
    quality: form.quality,
    language: form.language,
    keepChapters: form.keepChapters,
    keepImages: form.keepImages,
    noFinalFile: form.noFinalFile,
    // Always emitted (false included): queueRecipe.js spreads settings.defaults
    // UNDER these args, so omitting it on "off" would let a Settings komikku:true
    // win over the user switching it off in the dialog.
    komikku: !!form.komikku,
    multiSource: form.multiSource,
  };
  if (form.format === "epub") args.epubLayout = form.epubLayout;
  if (chapters && chapters.toLowerCase() !== "all") args.chapters = chapters;
  // Volume mode (the dialog only offers it for a MangaFire source; `chapters`
  // then holds VOLUME numbers). Refused under Komikku for the same reason as
  // downloadForm.js buildNewTabArgs: --komikku turns --download-volumes off,
  // so "volumes 1-3" would quietly become chapters 1-3. multiSource goes off
  // so confirmSearchArgs drops the prefetched alternatives, and no group is
  // sent: aio-dl.py ignores both for volumes ("[Volumes] Forcing").
  if (form.downloadVolumes) {
    if (form.komikku) throw new Error(KOMIKKU_VOLUMES_MESSAGE);
    args.downloadVolumes = true;
    args.multiSource = false;
  } else if (group) {
    args.group = group;
  }
  return args;
}

// The search result's own args (site, prefetchedAlts, multiSource, …) under the
// dialog's. Prefetched alternatives are dropped when multi-source ends up off
// (the user unticked it in the dialog), since they only feed --multi-source.
export function confirmSearchArgs(searchArgs, dialogArgs) {
  const out = { ...(searchArgs || {}), ...(dialogArgs || {}) };
  if (!out.multiSource) {
    delete out.prefetchedAlts;
    delete out.multiSourcePrefetched;
  }
  return out;
}
