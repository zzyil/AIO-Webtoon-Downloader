// ============================================================
// Queue recipes: a waiting download stores HOW to build its args, not the args.
//
// Why: a queued download used to carry CLI args resolved at enqueue time, so a
// Settings save (modernize, its effort, MTL, image concurrency, format, …)
// never reached items already waiting. User rule (2026-10-17): every setting
// is resolved when the item STARTS; the only exception is a field the user
// explicitly changed in a per-download form (the New tab, or the Search tab's
// "Download settings" dialog), which keeps the user's value.
//
// A recipe is `{ kind, input }`. `input` holds only job data (what makes this
// download distinct: chapters, the search's prefetched alternatives, a Sync
// row's flags…) plus the form's sparse edits. finalArgsFor(recipe, settings)
// re-runs the SAME builder the UI path used, against the settings passed in,
// so with unchanged settings the args are identical to what that path built
// before recipes existed.
//
//   kind          input                                   who queues it
//   defaultsPlus  { jobArgs }                             Sync (App.jsx onQueueDownload)
//   library       { meta:{format,language,site},          LibraryTab (update downloads)
//                   chapters, folderPath, kind? }         kind "volume" = volume run;
//                                                         absent = chapters (old items)
//   search        { searchArgs, dialogEdits, extraArgs }  SearchTab dialog; App.startFoundDownload
//                                                         dialogEdits.downloadVolumes = volume run
//   newTab        { formEdits, urlCount }                 DownloadTab
//
// Decisions the user may flip (2026-10-17): a search's / Sync row's
// multi-source flag is job data (pinned), not a Setting; Library updates keep
// the series' own format/language/site; "changed" = differs from the default
// at edit time, so setting a field back to its default follows Settings again.
//
// Consumers: hooks/useDownloader.js (grep finalArgsFor) resolves at spawn and
// for the Queue tab's badges. A recipe is persisted in download_queue.json, so
// changing an input shape needs to keep reading the old one.
// Relative imports only: tools/_test_queue_recipe.js bundles this with esbuild.
// ============================================================

import {
  buildLibraryDownloadArgs,
  searchDialogInitial,
  searchDialogArgs,
  confirmSearchArgs,
} from "./downloadArgs.js";
import { formDefaults, buildNewTabArgs } from "./downloadForm.js";

// App-wide settings every download honors, whatever path queued it (moved
// verbatim from useDownloader.queueDownload, where it was injected at enqueue).
// Recipe/caller args spread AFTER this and win on conflict. Each knob is
// skipped while it sits at Python's own default, so older saved settings keep
// producing identical spawn lines.
export function globalArgs(s) {
  return {
    verbose: s?.verboseAlways !== false,
    // XF-4: opt-in default OFF (absent → OFF). Matches main.js/searcher.js's
    // `=== true`; the old `!== false` collapsed on download but not on the
    // update-check, so fragments stuck as "+N new" forever.
    collapseSplits: s?.collapseSplits === true,
    // Curated-sites toggle. Persisted under settings.searchOpts.seededOnly
    // because SettingsTab + SearchTab both write that namespace; mirrored here
    // so download paths see the same flag. Only takes effect with
    // --multi-source: aio-dl.py reads seeded_only inside
    // find_alternatives_for_direct_url to skip the long-tail Madara extras
    // during the cross-site fan-out. searcher.js uses the identical check.
    ...(s?.searchOpts?.seededOnly ? { seededOnly: true } : {}),
    // Global machine-translation policy, injected here so search-initiated and
    // library-update downloads honor it too. The New tab emits its own mtl when
    // the user changed it (lib/downloadForm.js:buildNewTabArgs), which wins on
    // the spread. Skipped at "avoid" — that's the Python default.
    ...(s?.defaults?.mtl && s.defaults.mtl !== "avoid"
      ? { mtl: s.defaults.mtl }
      : {}),
    ...(s?.defaults?.excludeGroup
      ? { excludeGroup: s.defaults.excludeGroup }
      : {}),
    // -1 sentinel = "match --image-workers"; the Python side resolves it.
    ...(s?.prefetchImageWorkers != null && s.prefetchImageWorkers !== -1
      ? { prefetchImageWorkers: s.prefetchImageWorkers }
      : {}),
    // Fast-download knobs. Python defaults: imageConcurrency=8,
    // imagePrefetchDepth=2, imagePrefetchParallel=2. (Settings saved before
    // 2026-05-13 carry `mangafireImageConcurrency`; SettingsTab migrates it.)
    ...(s?.imageConcurrency != null && s.imageConcurrency !== 8
      ? { imageConcurrency: s.imageConcurrency }
      : {}),
    ...(s?.imagePrefetchDepth != null && s.imagePrefetchDepth !== 2
      ? { imagePrefetchDepth: s.imagePrefetchDepth }
      : {}),
    ...(s?.imagePrefetchParallel != null && s.imagePrefetchParallel !== 2
      ? { imagePrefetchParallel: s.imagePrefetchParallel }
      : {}),
    ...(s?.noFastDownload === true ? { noFastDownload: true } : {}),
    // External metadata enrichment (--metadata-source family, SettingsTab →
    // Metadata Enrichment). The valued knobs are gated on metadataSource so
    // they're never emitted without the master toggle. Python side:
    // sites/external_metadata.py.
    ...(s?.metadataSource && s.metadataSource !== "none"
      ? { metadataSource: s.metadataSource }
      : {}),
    ...(s?.metadataSource && s.metadataSource !== "none"
        && s?.metadataTagMinRank != null && s.metadataTagMinRank !== 50
      ? { metadataTagMinRank: s.metadataTagMinRank }
      : {}),
    ...(s?.metadataSource && s.metadataSource !== "none"
        && s?.metadataRefresh === true
      ? { metadataRefresh: true }
      : {}),
    // User-disabled sites → --disable-sites on EVERY spawn. aio-dl.py drops
    // them as multi-source alternatives (and guard-filters the persisted alt
    // cache). Skipped when empty so default spawns stay clean.
    ...(Array.isArray(s?.disabledSites) && s.disabledSites.length
      ? { disabledSites: s.disabledSites }
      : {}),
  };
}

function _obj(v, what) {
  if (v == null) return {};
  if (typeof v !== "object" || Array.isArray(v)) {
    throw new Error(`queue recipe: ${what} must be an object`);
  }
  return v;
}

// The recipe's own args (no globalArgs). Throws on a malformed recipe — a
// restored download_queue.json can hold anything — so the caller can drop that
// one item instead of retrying it forever (grep _resolveQueueArgs).
export function resolveRecipe(recipe, s) {
  if (!recipe || typeof recipe !== "object") throw new Error("queue recipe: missing");
  const input = _obj(recipe.input, "input");
  const defaults = s?.defaults || {};
  switch (recipe.kind) {
    case "defaultsPlus":
      return { ...defaults, ..._obj(input.jobArgs, "jobArgs") };
    case "library": {
      // The series' own format/language/site beat the global ones (same as
      // before recipes); everything else follows Settings.
      const built = buildLibraryDownloadArgs(
        _obj(input.meta, "meta"),
        defaults,
        input.chapters,
        s?.verboseAlways,
        input.folderPath,
        input.kind === "volume" ? "volume" : "chapter",
      );
      // The kind is the recipe's, never a setting: drop any downloadVolumes a
      // defaults dict carries (downloadForm.js DEFAULT_FORM explains why it is
      // job data).
      const { downloadVolumes: _ignored, ...rest } = defaults;
      return { ...rest, ...built };
    }
    case "search": {
      const searchArgs = _obj(input.searchArgs, "searchArgs");
      const form = {
        ...searchDialogInitial(s, searchArgs.multiSource),
        ..._obj(input.dialogEdits, "dialogEdits"),
      };
      // Same as "library": the kind is the dialog's (searchDialogArgs sets
      // downloadVolumes only in volume mode), never a defaults leftover.
      const { downloadVolumes: _dv, ...rest } = defaults;
      return {
        ...rest,
        ...confirmSearchArgs(searchArgs, searchDialogArgs(form)),
        ..._obj(input.extraArgs, "extraArgs"),
      };
    }
    case "newTab": {
      const edits = _obj(input.formEdits, "formEdits");
      // No settings.defaults spread: the form folds the defaults in itself
      // (formDefaults), exactly as the New tab always has.
      return buildNewTabArgs({ ...formDefaults(s), ...edits }, s, {
        edits,
        urlCount: Number(input.urlCount) || 1,
      });
    }
    default:
      throw new Error(`queue recipe: unknown kind ${JSON.stringify(recipe.kind)}`);
  }
}

// Recipe for a library update download. Keeps only the three series fields
// buildLibraryDownloadArgs reads, so a queued item doesn't carry (or persist)
// the whole .aio_series.json.
//
// kind "volume": `chapters` holds VOLUME labels, and format/language come from
// the ledger's "volumes" block (never the chapter format: series_ledger.py
// kind_view). A block without a format leaves it undefined, so the builder
// falls back to the shared default format (user decision 2026-11-06: one
// format setting for both kinds). `kind` is only written for volumes, so a
// chapter recipe persists exactly as before.
export function libraryRecipe(meta, chapters, folderPath, kind = "chapter") {
  const m = meta || {};
  if (kind === "volume") {
    const v = m.volumes && typeof m.volumes === "object" && !Array.isArray(m.volumes) ? m.volumes : {};
    return {
      kind: "library",
      input: {
        meta: { format: v.format || undefined, language: v.language || m.language, site: m.site },
        chapters,
        folderPath: folderPath || undefined,
        kind: "volume",
      },
    };
  }
  return {
    kind: "library",
    input: {
      meta: { format: m.format, language: m.language, site: m.site },
      chapters,
      folderPath: folderPath || undefined,
    },
  };
}

// The args a recipe spawns with under settings `s`.
export function finalArgsFor(recipe, s) {
  return { ...globalArgs(s), ...resolveRecipe(recipe, s) };
}
