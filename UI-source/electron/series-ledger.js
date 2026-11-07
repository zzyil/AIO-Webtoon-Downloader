// ============================================================
// series-ledger.js: the JS twin of series_ledger.py + the name rules of
// item_kinds.py that the Electron main process needs.
//
// What it owns (main-process side only; the renderer never requires it):
//   - The per-kind namespaces of `.aio_series.json`. Chapter state stays at
//     the top level (chapters_downloaded / chapters_ignored / format /
//     language); volume state lives in ONE optional sub-object
//       "volumes": {schema, downloaded, ignored, format, language, ...}
//     Python owns the write side of a finished run (series_ledger.py
//     merge_volume_run); this file only reads views and applies the user's
//     cross-outs (main.js set-chapters-ignored).
//   - trackedKinds: which kinds the update check lists and diffs. A
//     volume-only series must never show "+300 new chapters".
//   - The tmp-dir and volume-file name rules (tmpvol_<hid>, "<base> Vol.NN").
//
// Twins (change both sides together):
//   series_ledger.py  trackedKinds ↔ tracked_kinds, kindView ↔ kind_view,
//                     applyIgnored ↔ set_ignored
//   item_kinds.py     parseTmpDirName ↔ parse_tmp_dir_name,
//                     VOLUME_FILE_RE, parseVolumeNumberFromName
//   library_state.py  volumeFileBase ↔ volume_file_base
// Shared cases: tests/fixtures/series_ledger_cases.json, read by both
// tests/test_series_ledger.py and tools/_test_series_ledger.js.
//
// Consumers: main.js (update check, set-chapters-ignored), library.js
// (volume counts), series-merge.js (label rules + volume union),
// downloader.js (resume scan). Stdlib-free on purpose: no fs here.
// ============================================================

const CHAPTER = "chapter";
const VOLUME = "volume";
const VOLUMES_KEY = "volumes";
const VOLUME_SCHEMA = 1;

const CHAPTER_TMP_PREFIX = "tmp_";
const VOLUME_TMP_PREFIX = "tmpvol_";

// Canonical item label. Matches aio-dl.py's _chap_label_str / --list-chapters'
// f"{num:g}" for every label a site actually produces ("4.0" → "4", "02" →
// "2"); anything non-numeric passes through. series-merge.js re-exports this
// as chapterLabel so every writer of .aio_series.json spells a label the same.
function itemLabel(c) {
  const n = Number(c);
  return Number.isFinite(n) ? String(n) : String(c);
}

// Non-numeric labels sort last among themselves rather than poisoning the
// comparator with NaN (which makes Array#sort order-dependent).
function compareItemLabels(a, b) {
  const fa = parseFloat(a);
  const fb = parseFloat(b);
  if (Number.isNaN(fa) || Number.isNaN(fb)) {
    if (Number.isNaN(fa) && Number.isNaN(fb)) return a < b ? -1 : a > b ? 1 : 0;
    return Number.isNaN(fa) ? 1 : -1;
  }
  return fa - fb;
}

function sortedLabels(labels) {
  return [...new Set([...(labels || [])].map(itemLabel))].sort(compareItemLabels);
}

function isPlainObject(v) {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

function volumeBlock(meta) {
  const b = isPlainObject(meta) ? meta[VOLUMES_KEY] : null;
  return isPlainObject(b) ? b : null;
}

// One kind's progress as {downloaded, ignored, format, language}. The volume
// view never inherits the CHAPTER format (a volume block without one yields
// null and the caller picks a default), but does fall back to the series
// language, which is what a self-healed block needs.
function kindView(meta, kind) {
  const m = isPlainObject(meta) ? meta : {};
  if (kind === CHAPTER) {
    return {
      downloaded: (m.chapters_downloaded || []).map(itemLabel),
      ignored: (m.chapters_ignored || []).map(itemLabel),
      format: m.format ?? null,
      language: m.language ?? null,
    };
  }
  if (kind === VOLUME) {
    const b = volumeBlock(m) || {};
    return {
      downloaded: (b.downloaded || []).map(itemLabel),
      ignored: (b.ignored || []).map(itemLabel),
      format: b.format ?? null,
      language: b.language || m.language || null,
    };
  }
  throw new Error(`unknown item kind: ${JSON.stringify(kind)}`);
}

// Which kinds this series tracks, in the fixed order [chapter, volume].
//   chapter: chapters_downloaded is non-empty, or there is no volume evidence
//            at all (a legacy ledger, or none, has always meant chapters).
//   volume:  the volume block exists, or volume files are on disk (self-heal
//            for an older build that rewrote the ledger and dropped the block).
// An EMPTY volume block still tracks volumes: the user chose volume mode.
function trackedKinds(meta, { volumeFilesPresent = false } = {}) {
  const m = isPlainObject(meta) ? meta : {};
  const hasVolumes = volumeBlock(m) !== null || !!volumeFilesPresent;
  const out = [];
  const chapters = m.chapters_downloaded;
  if ((Array.isArray(chapters) && chapters.length > 0) || !hasVolumes) out.push(CHAPTER);
  if (hasVolumes) out.push(VOLUME);
  return out;
}

// Add (ignored=true) or remove (false) cross-outs for ONE kind. Returns
// {meta, ignored} with a NEW meta object; the input is not mutated. Same
// "emptied → key removed" rule as the Python writers, so a fully un-crossed
// series reads the same as one that never had a cross-out. Blank entries are
// skipped before labelling: Number("") is 0, which would cross out item 0.
// An empty volume list never CREATES the block: a block alone makes the
// series track volumes (trackedKinds), so restoring the last cross-out on a
// series without one must leave the file without one.
function applyIgnored(meta, kind, items, ignored) {
  const out = { ...(isPlainObject(meta) ? meta : {}) };
  const current = kindView(out, kind).ignored;
  const next = new Set(current);
  for (const c of Array.isArray(items) ? items : [items]) {
    if (c === null || c === undefined || String(c).trim() === "") continue;
    if (ignored) next.add(itemLabel(c));
    else next.delete(itemLabel(c));
  }
  const list = [...next].sort(compareItemLabels);
  if (kind === CHAPTER) {
    if (list.length) out.chapters_ignored = list;
    else delete out.chapters_ignored;
  } else if (kind === VOLUME) {
    const existing = volumeBlock(out);
    if (existing || list.length) {
      const block = { ...(existing || { schema: VOLUME_SCHEMA, downloaded: [] }) };
      if (list.length) block.ignored = list;
      else delete block.ignored;
      out[VOLUMES_KEY] = block;
    }
  } else {
    throw new Error(`unknown item kind: ${JSON.stringify(kind)}`);
  }
  return { meta: out, ignored: list };
}

// "tmpvol_<hid>" → {hid, kind:"volume"}, "tmp_<hid>" → {hid, kind:"chapter"},
// anything else → null. The longer prefix is checked first; "tmpvol_" does not
// start with "tmp_", so a chapter hid that begins "vol_" still reads as a
// chapter dir.
function parseTmpDirName(name) {
  const s = String(name || "");
  if (s.startsWith(VOLUME_TMP_PREFIX) && s.length > VOLUME_TMP_PREFIX.length) {
    return { hid: s.slice(VOLUME_TMP_PREFIX.length), kind: VOLUME };
  }
  if (s.startsWith(CHAPTER_TMP_PREFIX) && s.length > CHAPTER_TMP_PREFIX.length) {
    return { hid: s.slice(CHAPTER_TMP_PREFIX.length), kind: CHAPTER };
  }
  return null;
}

function tmpDirName(hid, kind) {
  return (kind === VOLUME ? VOLUME_TMP_PREFIX : CHAPTER_TMP_PREFIX) + hid;
}

// A volume archive stem ENDS in "Vol.<NN>[~d]", preceded by start or a space.
// End-anchoring rejects Komikku's chapter form "Vol.01 Ch.005".
const VOLUME_FILE_RE = /(?:^|\s)Vol\.(\d+(?:~\d+)?)$/i;
const RAW_VOLUME_DIR_RE = /^Volume_([0-9]+(?:[.~][0-9]+)?)$/i;

// The base name a volume run writes for `title` (aio-dl.py sanitize_filename).
function volumeFileBase(title) {
  let s = String(title || "").replace(/[\\/*?:"<>|]/g, "").replace(/_/g, " ");
  s = s.replace(/\s+/g, " ").trim().replace(/[ .]+$/, "");
  return s || null;
}

// Volume label from a per-volume archive name, or null. `base`, when given,
// must be exactly the text before " Vol." — that is what keeps the combined
// archive of a series titled "... Vol.3" from reading as volume 3.
function parseVolumeNumberFromName(filename, base = null) {
  const stem = String(filename).replace(/\.(?:cbz|pdf|epub|zip)$/i, "");
  const m = VOLUME_FILE_RE.exec(stem);
  if (!m) return null;
  if (base !== null && base !== undefined) {
    if (stem.slice(0, m.index).trim() !== String(base).trim()) return null;
  }
  const n = Number(m[1].replace("~", "."));
  return Number.isFinite(n) ? String(n) : null;
}

function parseRawVolumeDir(name) {
  const m = RAW_VOLUME_DIR_RE.exec(String(name));
  if (!m) return null;
  const n = Number(m[1].replace("~", "."));
  return Number.isFinite(n) ? String(n) : null;
}

module.exports = {
  CHAPTER,
  VOLUME,
  VOLUMES_KEY,
  VOLUME_SCHEMA,
  VOLUME_FILE_RE,
  itemLabel,
  compareItemLabels,
  sortedLabels,
  volumeBlock,
  kindView,
  trackedKinds,
  applyIgnored,
  parseTmpDirName,
  tmpDirName,
  volumeFileBase,
  parseVolumeNumberFromName,
  parseRawVolumeDir,
};
