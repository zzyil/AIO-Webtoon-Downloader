// Shared builder for library/update-check download argument dicts.
//
// Both LibraryTab surfaces that queue an update download — the per-series
// detail view ("Download Missing Chapters", UpdateSection.handleDownloadNew)
// and the Updates Center per-row queue (buildDownloadArgsForRow) — assemble
// the SAME args object from the series metadata + the user's saved
// settings.defaults.
//
// The produced arg dict is forwarded to useDownloader.queueDownload (via
// App.jsx's onStartDownload wrapper, which spreads settings.defaults UNDER it)
// and ultimately mapped to CLI flags in electron/downloader.js:buildCliArgs.
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
// over the settings.defaults spread (App.jsx onStartDownload), and
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
//
// NOTE the XF-2 quality default of 100 (NOT 85): any --quality < 100 flips the
// child's _user_set_quality True and disables the CBZ byte-passthrough
// fast-path (silent lossy re-encode). Keep it 100.
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
export function buildLibraryDownloadArgs(
  meta,
  d,
  chaptersStr,
  verboseAlways,
) {
  const m = meta || {};
  const def = d || {};
  const args = {
    format: m.format || def.format || "pdf",
    quality: def.quality ?? 100,
    chapters: chaptersStr,
    language: m.language || "en",
    site: m.site || undefined,
    verbose: verboseAlways ?? true,
  };
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
  if (def.multiSource === true) {
    args.multiSourceLazy = true;
  }
  return args;
}
