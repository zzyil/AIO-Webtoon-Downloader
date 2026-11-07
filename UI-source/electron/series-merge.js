// ============================================================
// MERGE DUPLICATE SERIES FOLDERS
//
// One series, two folders. A site RENAMING a series used to fork a second
// folder — aio-dl.py keyed the folder on the site's title string, so when
// MangaDex retitled "Isekai de Slow Life o (Ganbou)" to "...wo (Ganbou)" the
// next download created a new folder carrying an IDENTICAL .series_hid and the
// series' chapters ended up split 59/5 across the two. aio-dl.py's
// allocate_series_output_dir now prevents that; this module is how the user
// cleans up the forks already on disk.
//
// Reached from the amber duplicate badge in LibraryTab's grid, ALWAYS as a dry
// run the user confirms before anything moves.
//
// THE RULES THAT MAKE THIS SAFE TO POINT AT SOMEONE'S LIBRARY:
//   * Nothing is ever OVERWRITTEN. A chapter file present in both folders is a
//     COLLISION: the source's copy stays put and is reported.
//   * The source folder is removed only when nothing of value is left in it —
//     no collisions, no unrecognized files. Otherwise it survives and the
//     report says why.
//   * Both folders must be direct children of the library root, so a bad path
//     can never reach outside it.
//   * They must provably be the same series (identical site+hid or URL), or at
//     least share an anilist_id — the weaker claim the badge shows as a
//     "possible duplicate", which the user is vouching for by confirming.
//   * Refused while a download for this series is running, or while any
//     running download's URL is unknown: that process is writing archives into
//     one of these very folders.
//
// WHY ITS OWN MODULE (not main.js): it is the one piece of this app that
// deletes user data, so it has to be drivable by an offline test. Dependencies
// arrive as plain DATA (libraryRoot, runningDownloads) rather than as electron
// handles — same reason update-check-record.js takes an injected `send`.
//
// Cross-file: electron/library.js:findDuplicateSeries decides what gets badged
// and supplies the identity rules; aio-dl.py:_warn_duplicate_series_folders is
// the CLI's equivalent report. Offline regression: tools/_test_series_merge.js.
// grep seriesIdentityKey.
// ============================================================

const fs = require("fs");
const path = require("path");

const {
  seriesIdentityKey,
  normalizeSeriesUrl,
  extractChaptersFromFiles,
  getImageChaptersOnDevice,
  imageChapterToken,
} = require("./library");

const SERIES_META_FILE = ".aio_series.json";

const PAYLOAD_EXTS = new Set([".pdf", ".epub", ".cbz"]);

// There can only be ONE of these per series, so a copy in the source is
// redundant rather than conflicting: moved when the target has none, dropped
// along with the folder when it does.
const SINGLETON_FILES = new Set([
  "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
  "details.json", ".cover.jpg", "download_params.json",
]);

// Identity/bookkeeping. Merged, or already present on the target — never
// moved, and their presence must not stop the source folder being removed.
const METADATA_FILES = new Set([
  SERIES_META_FILE, ".series_hid", ".mangafire_hid", ".DS_Store",
]);

// Label rules (aio-dl.py _chap_label_str twin) now live in series-ledger.js,
// which also owns the volume namespace; re-exported below under the old names
// because main.js and the tests import them from here. One definition is the
// only way the writers of .aio_series.json can't drift into spelling an item
// differently.
const {
  itemLabel: chapterLabel,
  compareItemLabels: compareChapterLabels,
  volumeBlock,
  VOLUMES_KEY,
  VOLUME_SCHEMA,
} = require("./series-ledger");

async function pathExists(p) {
  try {
    await fs.promises.access(p);
    return true;
  } catch {
    return false;
  }
}

/**
 * Chapter NUMBERS an archive filename covers, as a Set of normalized tokens.
 *
 * Delegates to library.js:extractChaptersFromFiles so this parses filenames
 * EXACTLY the way the update check does — Komikku ("Ch.005.5 - Title.cbz"),
 * legacy ("Title Ch 5~5.pdf") and combined ranges ("Title Ch 1-50.pdf") alike.
 * A range is expanded to its whole numbers (bounded, because a corrupt name
 * could otherwise claim a colossal span); a name that yields nothing returns an
 * empty Set, and the caller falls back to matching on the filename.
 */
function chapterNumbersForFile(name) {
  const { chapters, ranges } = extractChaptersFromFiles([{ name }]);
  const out = new Set(chapters);
  for (const r of ranges) {
    if (!Number.isFinite(r.start) || !Number.isFinite(r.end)) continue;
    if (r.end < r.start || r.end - r.start > 5000) continue;
    for (let n = Math.ceil(r.start); n <= Math.floor(r.end); n += 1) out.add(String(n));
  }
  return out;
}

/**
 * Every chapter number already present in `folder`, from its archive names.
 *
 * WHY THIS EXISTS, and it is the whole reason merging is safe: the same series
 * does NOT keep one filename style forever. The real fork that motivated this
 * module had "Ch.002 - Learning Alchemy.cbz" in one folder and bare
 * "Ch.060.cbz" in the other, and two providers of one series agree even less.
 * Matching on filenames alone would call those distinct and happily move a
 * second copy of a chapter the target already has — turning a merge into a
 * silent duplication. Collisions are decided on the NUMBER.
 */
async function chapterNumbersInFolder(folder) {
  let names = [];
  try {
    const dirents = await fs.promises.readdir(folder, { withFileTypes: true });
    names = dirents
      .filter((d) => d.isFile() && PAYLOAD_EXTS.has(path.extname(d.name).toLowerCase()))
      .map((d) => ({ name: d.name }));
  } catch {
    return new Set();
  }
  const { chapters, ranges } = extractChaptersFromFiles(names);
  const out = new Set(chapters);
  for (const r of ranges) {
    if (!Number.isFinite(r.start) || !Number.isFinite(r.end)) continue;
    if (r.end < r.start || r.end - r.start > 5000) continue;
    for (let n = Math.ceil(r.start); n <= Math.floor(r.end); n += 1) out.add(String(n));
  }
  return out;
}

async function readSeriesMetaOrNull(folderPath) {
  try {
    const parsed = JSON.parse(
      await fs.promises.readFile(path.join(folderPath, SERIES_META_FILE), "utf8")
    );
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * What merging `sourceFolder` into `targetFolder` would do. Pure — reads only.
 *
 * Every entry lands in exactly one bucket so the confirmation dialog can show
 * the whole folder accounted for: `moves` (goes across), `collisions` (target
 * already has that chapter — stays put), `redundant` (a second cover/details
 * the target doesn't need), `leftovers` (anything unrecognized, never touched).
 *
 * `claims` holds the chapter numbers already spoken for — seeded from the
 * target and ADDED TO as the plan claims more. Threading one mutable pair of
 * sets through every source folder in a merge is what stops two sources that
 * both hold chapter 60 from each moving a copy in: the second sees the first's
 * claim. Omit it and the function seeds its own from the target (the shape the
 * standalone test uses).
 */
async function planFolderMerge(targetFolder, sourceFolder, claims) {
  const plan = {
    sourceFolder,
    moves: [],
    collisions: [],
    redundant: [],
    leftovers: [],
    error: null,
    canRemoveSource: false,
  };

  const claimed = claims?.archives || (await chapterNumbersInFolder(targetFolder));
  const imageClaimed = claims?.images || new Set();

  let dirents;
  try {
    dirents = await fs.promises.readdir(sourceFolder, { withFileTypes: true });
  } catch (err) {
    plan.error = err.message;
    return plan;
  }

  for (const dirent of dirents) {
    const name = dirent.name;
    const from = path.join(sourceFolder, name);

    if (dirent.isFile()) {
      if (METADATA_FILES.has(name)) continue;
      const to = path.join(targetFolder, name);
      const ext = path.extname(name).toLowerCase();
      if (PAYLOAD_EXTS.has(ext)) {
        // Number first, filename second. A chapter the target already holds
        // under a DIFFERENT name is still a collision — see
        // chapterNumbersInFolder for why that is the load-bearing check.
        const numbers = [...chapterNumbersForFile(name)];
        const dupNumber = numbers.find((n) => claimed.has(n));
        if (dupNumber !== undefined) {
          plan.collisions.push({ name, from, to, chapter: dupNumber });
        } else if (await pathExists(to)) {
          plan.collisions.push({ name, from, to });
        } else {
          for (const n of numbers) claimed.add(n);
          plan.moves.push({ name, from, to });
        }
      } else if (SINGLETON_FILES.has(name.toLowerCase())) {
        if (await pathExists(to)) plan.redundant.push({ name, from });
        else plan.moves.push({ name, from, to });
      } else {
        plan.leftovers.push({ name, from });
      }
      continue;
    }

    // images/Chapter_<n>/ for --format none series. Merged one chapter dir at a
    // time rather than as a tree, so a chapter present in both is a single
    // reportable collision instead of a half-merged directory.
    if (dirent.isDirectory() && name.toLowerCase() === "images") {
      let chapterDirs = [];
      try {
        chapterDirs = await fs.promises.readdir(from, { withFileTypes: true });
      } catch {}
      // Numbers, not directory names, for the same reason archives use them:
      // "Chapter_5" and "Chapter_5.0" are the same chapter, and the legacy
      // "ch_" prefix is still accepted by the scanner.
      const targetImageChapters = getImageChaptersOnDevice(targetFolder);
      for (const child of chapterDirs) {
        const label = `images/${child.name}`;
        const childFrom = path.join(from, child.name);
        if (!child.isDirectory()) {
          plan.leftovers.push({ name: label, from: childFrom });
          continue;
        }
        const childTo = path.join(targetFolder, "images", child.name);
        const token = imageChapterToken(child.name);
        const number = token == null ? null : String(parseFloat(token));
        const alreadyThere =
          (number != null && (targetImageChapters.has(number) || imageClaimed.has(number))) ||
          (await pathExists(childTo));
        if (alreadyThere) {
          plan.collisions.push({ name: label, from: childFrom, to: childTo, chapter: number });
        } else {
          if (number != null) imageClaimed.add(number);
          plan.moves.push({ name: label, from: childFrom, to: childTo });
        }
      }
      continue;
    }

    plan.leftovers.push({ name, from });
  }

  plan.canRemoveSource = plan.collisions.length === 0 && plan.leftovers.length === 0;
  return plan;
}

/**
 * Union the chapter bookkeeping and adopt the freshest display fields.
 *
 * Identity (url/hid/site/format/language) is the TARGET's and never moves —
 * the target is where the series lives from now on. Display fields
 * (title/status/cover/authors/genres + the AniList block) come from whichever
 * member was downloaded most recently, because that is the one that saw the
 * site last, which for a rename-induced fork is precisely the fork.
 */
function mergeSeriesMetaObjects(targetMeta, sourceMetas) {
  const all = [targetMeta || {}, ...sourceMetas.filter(Boolean)];
  const merged = { ...(targetMeta || {}) };

  const unionLabels = (key) => {
    const out = new Set();
    for (const m of all) for (const c of m[key] || []) out.add(chapterLabel(c));
    return [...out].sort(compareChapterLabels);
  };

  merged.chapters_downloaded = unionLabels("chapters_downloaded");

  // Kept disjoint from chapters_downloaded exactly as aio-dl.py does (grep
  // merged_skipped): a fragment that turns out to be present somewhere counts
  // as downloaded, not skipped.
  const downloaded = new Set(merged.chapters_downloaded);
  merged.chapters_skipped_fragments = unionLabels("chapters_skipped_fragments")
    .filter((c) => !downloaded.has(c));
  const ignored = unionLabels("chapters_ignored").filter((c) => !downloaded.has(c));
  // Emptied out → drop the key, so a merged series reads the same on disk as
  // one that never had a cross-out (aio-dl.py writes it under the same rule).
  if (ignored.length > 0) merged.chapters_ignored = ignored;
  else delete merged.chapters_ignored;

  // Per-format union. Absent stays absent — readers must treat a missing key
  // as "unknown", never as "covers nothing".
  const finalFiles = {};
  for (const m of all) {
    for (const [fmt, labels] of Object.entries(m.final_file_chapters || {})) {
      const set = new Set([...(finalFiles[fmt] || []), ...(labels || []).map(chapterLabel)]);
      finalFiles[fmt] = [...set].sort(compareChapterLabels);
    }
  }
  if (Object.keys(finalFiles).length > 0) merged.final_file_chapters = finalFiles;

  const freshest = all
    .filter((m) => m.last_downloaded_at)
    .sort((a, b) => String(b.last_downloaded_at).localeCompare(String(a.last_downloaded_at)))[0];
  if (freshest) {
    for (const key of [
      "title", "status", "cover", "authors", "genres", "last_downloaded_at",
      "anilist_id", "mal_id", "country_of_origin", "media_format",
      "anilist_synonyms", "anilist_tags", "anilist_spoiler_tags",
    ]) {
      if (freshest[key] !== undefined) merged[key] = freshest[key];
    }
  }

  // null means "the pool was deliberately floored, so the real total is
  // unknown". The largest known number is the honest answer; all-unknown stays
  // null rather than becoming a made-up 0.
  const totals = all
    .map((m) => m.total_available_at_download)
    .filter((v) => typeof v === "number");
  merged.total_available_at_download = totals.length ? Math.max(...totals) : null;

  // Volume namespace (series-ledger.js header). Only written when some member
  // has a block, so a chapter-only merge stays byte-identical to before.
  // Same rules as the chapter side: labels unioned, ignored kept disjoint from
  // downloaded, the target's own format/language win (it is where the series
  // lives now), the freshest stamp and the largest known total are kept.
  const blocks = all.map(volumeBlock).filter(Boolean);
  if (blocks.length > 0) {
    const vUnion = (key) => {
      const out = new Set();
      for (const b of blocks) for (const c of b[key] || []) out.add(chapterLabel(c));
      return [...out].sort(compareChapterLabels);
    };
    const own = volumeBlock(targetMeta) || {};
    const firstWith = (key) => blocks.find((b) => b[key] != null)?.[key];
    const vDownloaded = vUnion("downloaded");
    const vDownloadedSet = new Set(vDownloaded);
    const vIgnored = vUnion("ignored").filter((c) => !vDownloadedSet.has(c));
    const vTotals = blocks
      .map((b) => b.total_available_at_download)
      .filter((v) => typeof v === "number");
    const vStamps = blocks.map((b) => b.last_downloaded_at).filter(Boolean).map(String).sort();
    const block = {
      ...own,
      schema: own.schema ?? VOLUME_SCHEMA,
      downloaded: vDownloaded,
      format: own.format ?? firstWith("format") ?? null,
      language: own.language ?? firstWith("language") ?? null,
      total_available_at_download: vTotals.length ? Math.max(...vTotals) : null,
    };
    if (vStamps.length) block.last_downloaded_at = vStamps[vStamps.length - 1];
    if (vIgnored.length) block.ignored = vIgnored;
    else delete block.ignored;
    merged[VOLUMES_KEY] = block;
  }

  return merged;
}

async function moveEntry(from, to) {
  await fs.promises.mkdir(path.dirname(to), { recursive: true });
  try {
    await fs.promises.rename(from, to);
    return;
  } catch (err) {
    // Only a cross-device move justifies the slow path; anything else is a
    // real failure and must surface.
    if (err?.code !== "EXDEV") throw err;
  }
  await fs.promises.cp(from, to, { recursive: true, errorOnExist: true });
  await fs.promises.rm(from, { recursive: true, force: true });
}

/**
 * Merge one or more source folders into a target. See the block header.
 *
 * @param {object}   opts
 * @param {string}   opts.targetFolder    - the folder the series lives in afterwards
 * @param {string[]} opts.sourceFolders   - folders to fold into it
 * @param {boolean}  opts.dryRun          - default TRUE; nothing is touched
 * @param {string}   opts.libraryRoot     - resolved library root (containment guard)
 * @param {Array}    opts.runningDownloads- [{ title, url }] from Downloader.getRunning()
 * @returns {Promise<object>} { ok, dryRun, targetFolder, chaptersBefore,
 *   chaptersAfter, plans, ... } or { ok:false, error }
 */
async function mergeSeriesFolders({
  targetFolder,
  sourceFolders,
  dryRun = true,
  libraryRoot,
  runningDownloads = [],
} = {}) {
  if (!targetFolder || typeof targetFolder !== "string") return { ok: false, error: "no_target" };
  if (!libraryRoot || typeof libraryRoot !== "string") return { ok: false, error: "no_library_root" };

  const sources = (Array.isArray(sourceFolders) ? sourceFolders : [sourceFolders])
    .filter((s) => typeof s === "string" && s.trim());
  if (sources.length === 0) return { ok: false, error: "no_sources" };

  const root = path.resolve(libraryRoot);
  const target = path.resolve(targetFolder);
  const resolvedSources = [...new Set(sources.map((s) => path.resolve(s)))];

  // Containment: series folders are direct children of the library root, so
  // this is both the tightest and the simplest correct guard.
  for (const folder of [target, ...resolvedSources]) {
    if (path.dirname(folder) !== root) return { ok: false, error: "outside_library", folder };
    if (!(await pathExists(folder))) return { ok: false, error: "missing_folder", folder };
  }
  if (resolvedSources.includes(target)) return { ok: false, error: "target_is_source" };

  const targetMeta = await readSeriesMetaOrNull(target);
  const sourceMetas = [];
  for (const folder of resolvedSources) sourceMetas.push(await readSeriesMetaOrNull(folder));

  // Identity. Refusing here is what stops a mis-click folding two unrelated
  // series into one folder — an operation nothing could undo.
  const targetKey = seriesIdentityKey(targetMeta);
  for (let i = 0; i < resolvedSources.length; i += 1) {
    const meta = sourceMetas[i];
    const key = seriesIdentityKey(meta);
    const sameSeries = !!(targetKey && key && targetKey === key);
    const sameAnilist = !!(
      targetMeta?.anilist_id && meta?.anilist_id && targetMeta.anilist_id === meta.anilist_id
    );
    if (!sameSeries && !sameAnilist) {
      return { ok: false, error: "identity_mismatch", folder: resolvedSources[i] };
    }
  }

  // A live download is writing archives into one of these folders.
  if (runningDownloads.length > 0) {
    const seriesUrls = new Set(
      [targetMeta, ...sourceMetas].map((m) => normalizeSeriesUrl(m?.url)).filter(Boolean)
    );
    for (const job of runningDownloads) {
      const jobUrl = normalizeSeriesUrl(job?.url);
      // An unidentifiable running job cannot be proven unrelated, so it counts.
      if (!jobUrl || seriesUrls.has(jobUrl)) {
        return { ok: false, error: "download_running", title: job?.title || job?.url || "" };
      }
    }
  }

  // Seeded from the target once, then carried across every source so a chapter
  // claimed by the first source is a collision for the second.
  const claims = {
    archives: await chapterNumbersInFolder(target),
    images: new Set(),
  };
  const plans = [];
  for (const folder of resolvedSources) {
    plans.push(await planFolderMerge(target, folder, claims));
  }

  const mergedMeta = mergeSeriesMetaObjects(targetMeta, sourceMetas);
  const summary = {
    targetFolder: target,
    chaptersBefore: (targetMeta?.chapters_downloaded || []).length,
    chaptersAfter: mergedMeta.chapters_downloaded.length,
    plans: plans.map((p) => ({
      sourceFolder: p.sourceFolder,
      moves: p.moves.map((m) => m.name),
      collisions: p.collisions.map((m) => m.name),
      redundant: p.redundant.map((m) => m.name),
      leftovers: p.leftovers.map((m) => m.name),
      willRemoveSource: p.canRemoveSource,
      error: p.error,
    })),
  };

  if (dryRun) return { ok: true, dryRun: true, ...summary };

  const unreadable = plans.find((p) => p.error);
  if (unreadable) return { ok: false, error: "unreadable_source", folder: unreadable.sourceFolder };

  // Files first, metadata second, folder removal last. A crash between steps
  // leaves the chapters in the target with metadata that merely UNDERSTATES
  // what is there — which the next check heals — rather than a metadata file
  // claiming chapters that never moved.
  const moved = [];
  for (const plan of plans) {
    for (const entry of plan.moves) {
      await moveEntry(entry.from, entry.to);
      moved.push(entry.name);
    }
  }

  // No target metadata means nothing to merge INTO — the files still moved,
  // which is the useful half, and the next scan rebuilds from disk.
  if (targetMeta) {
    await fs.promises.writeFile(
      path.join(target, SERIES_META_FILE),
      JSON.stringify(mergedMeta, null, 2),
      "utf8"
    );
  }

  const removed = [];
  const kept = [];
  for (const plan of plans) {
    if (!plan.canRemoveSource) {
      kept.push(plan.sourceFolder);
      continue;
    }
    await fs.promises.rm(plan.sourceFolder, { recursive: true, force: true });
    removed.push(plan.sourceFolder);
  }

  return { ok: true, dryRun: false, ...summary, moved, removed, kept };
}

module.exports = {
  mergeSeriesFolders,
  planFolderMerge,
  mergeSeriesMetaObjects,
  chapterLabel,
  compareChapterLabels,
};
