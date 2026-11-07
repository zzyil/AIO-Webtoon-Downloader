// ============================================================
// DEVICE SYNC — NAMES (pure)
//
// Owns how names are made and compared on a sync target:
//   * sanitizeSegment  — a PC-derived name turned into one safe path segment;
//   * slotKey          — "is this the same file/folder on THIS target?";
//   * compactKey       — loose identity for the adoption ladder's name match;
//   * resolveTargetName — the device folder an UNBOUND series gets
//                         (alias, then naming policy; parent plan rule 1);
//   * deriveSearchQuery — batch find-sources' query from a device folder name.
//
// Read by: planner.js (binding, collisions, adoption), chapter-labels.js
// (whole-file detection), find-sources.js (queries).
// Depends on: library.js normalizeSeriesUrl (alias URL keys).
//
// A BOUND folder's name is never derived here: rule 1 keeps it byte-exact, and
// the gone-folder re-push (decision 10) reuses it byte-exact too, because
// sanitizeSegment's NFKC and trailing-dot trim would change the name Komikku
// keys a series on.
// ============================================================

const { normalizeSeriesUrl } = require("../library");

// One path segment on both Windows folders and Android shared storage. The
// set is CompareManga's manga_ops.sanitize_tablet_segment, plus DEL.
const FORBIDDEN_SEGMENT_CHARS = /[/\\:*?"<>|\x00-\x1f\x7f]/g;

// Linux NAME_MAX, and the FAT/exFAT/NTFS limits are all at or above it in
// bytes for the names we produce; Komikku itself caps at 240 (komikkuspec).
const MAX_SEGMENT_BYTES = 255;

function utf8Bytes(s) {
  return Buffer.byteLength(String(s), "utf8");
}

/**
 * Turn a PC-derived name into one target path segment.
 * Returns {name, error}: error is null, "empty" (nothing left after
 * cleaning), or "too-long" (> 255 UTF-8 bytes; name is still returned so the
 * caller can show it). Never returns "." or "..".
 */
function sanitizeSegment(raw) {
  let s = String(raw == null ? "" : raw).normalize("NFKC").replace(FORBIDDEN_SEGMENT_CHARS, "");
  // Trailing dots and spaces: Windows silently drops them, so a folder target
  // and an adb target would otherwise disagree about the name.
  s = s.trim().replace(/[ .]+$/, "");
  if (!s || s === "." || s === "..") return { name: "", error: "empty" };
  if (utf8Bytes(s) > MAX_SEGMENT_BYTES) return { name: s, error: "too-long" };
  return { name: s, error: null };
}

/**
 * Validity of a name used byte-exact (a bound or gone folder's recorded
 * name, a device file name). Null when usable, else the reason. The path
 * level checks (root containment, 1,018-byte remote cap) live in
 * transports.js validatePath.
 */
function segmentError(name) {
  const s = String(name == null ? "" : name);
  if (!s || s === "." || s === "..") return "empty";
  if (/[/\x00\r\n]/.test(s)) return "forbidden-char";
  if (utf8Bytes(s) > MAX_SEGMENT_BYTES) return "too-long";
  return null;
}

// Mirror of aio-dl.py:949 (allocate_series_output_dir's clean_title). AIO
// appends " (hid=<hid>)" only on a genuine title collision.
const HID_SUFFIX_RE = /\s*\(hid=[^)]+\)\s*$/;

function stripHidSuffix(name) {
  const s = String(name == null ? "" : name);
  const stripped = s.replace(HID_SUFFIX_RE, "").trim();
  return stripped || s.trim();
}

/**
 * Identity of a name on one target. NFC always; lowercase too when the
 * target's probe measured case-insensitive storage (parent rule 11).
 *
 * toLowerCase, not a full Unicode casefold: JS has none, and the storage's own
 * folding (ext4/f2fs casefold on Android, NTFS's upcase table) differs from
 * any table we could ship anyway. Over-matching is the safe direction — the
 * slot guard then refuses to delete — and where availability of a NEW name
 * matters, the planner also requires a STA2 ENOENT from the storage itself.
 */
function slotKey(name, caseInsensitive) {
  const n = String(name == null ? "" : name).normalize("NFC");
  return caseInsensitive ? n.toLowerCase() : n;
}

/**
 * Loose comparison key for the adoption ladder's "name" tier: case, accents,
 * spaces and punctuation don't count. "SPY_x_FAMILY" and "SPY×FAMILY" agree;
 * so do "Shōnen" and "Shonen". Callers strip a hid suffix first.
 */
function compactKey(s) {
  return String(s == null ? "" : s)
    .normalize("NFKD")
    .replace(/\p{M}+/gu, "")
    .toLowerCase()
    .replace(/×/g, "x")
    .replace(/ø/g, "o")
    .replace(/ß/g, "ss")
    .replace(/[^\p{L}\p{N}]+/gu, "");
}

/**
 * Search query for a device-only folder (batch find-sources), following
 * CompareManga batch_search.py's hand-applied rules: underscores become
 * spaces, an "(Official)" format hint is dropped ("(Colored)" is kept: it
 * disambiguates a re-release), and dashes that wrap a word ("-Song-") go,
 * while in-word hyphens ("One-Punch") stay. The query stays editable per row.
 */
function deriveSearchQuery(folderName) {
  const base = stripHidSuffix(folderName);
  let q = base.replace(/_/g, " ").replace(/\s*\(official\)\s*/gi, " ");
  q = q.replace(/(^|\s)-+(?=\S)/g, "$1").replace(/(\S)-+(?=\s|$)/g, "$1");
  q = q.replace(/\s+/g, " ").trim();
  return q || base.trim();
}

/**
 * The first entry of a per-target series list (aliases, excludes, `only`)
 * that refers to this series, or null. Precedence: series identity, then
 * normalized URL, then PC folder name — the order the parent plan keys these
 * lists by, so a retitle (new folder name) still finds an identity-keyed entry.
 *
 * @param {{identityKey:?string, url:?string, folder:string}} series
 * @param {Array<{identityKey?:string, url?:string, pcFolder?:string}>} entries
 */
function findSeriesEntry(series, entries) {
  const list = Array.isArray(entries) ? entries : [];
  if (series.identityKey) {
    const a = list.find((x) => x && x.identityKey && x.identityKey === series.identityKey);
    if (a) return a;
  }
  const url = normalizeSeriesUrl(series.url);
  if (url) {
    const a = list.find((x) => x && x.url && normalizeSeriesUrl(x.url) === url);
    if (a) return a;
  }
  return list.find((x) => x && x.pcFolder && x.pcFolder === series.folder) || null;
}

const NAMING_POLICIES = Object.freeze(["strip-hid", "as-is", "title"]);

/**
 * Device folder name for an UNBOUND series: alias first, else the target's
 * naming policy. Returns {name, source: 'alias'|'policy', error} with error as
 * in sanitizeSegment. A bound series never comes through here (rule 1).
 */
function resolveTargetName(series, cfg) {
  const alias = findSeriesEntry(series, cfg && cfg.aliases);
  if (alias && alias.deviceFolder) {
    // An alias names an existing device folder, so it is used byte-exact.
    const err = segmentError(alias.deviceFolder);
    return { name: alias.deviceFolder, source: "alias", error: err };
  }
  const policy = (cfg && cfg.namingPolicy) || "strip-hid";
  let raw;
  if (policy === "as-is") raw = series.folder;
  else if (policy === "title") raw = series.title || stripHidSuffix(series.folder);
  else raw = stripHidSuffix(series.folder);
  const { name, error } = sanitizeSegment(raw);
  return { name, source: "policy", error };
}

/**
 * Rule 1's collision fallback: two different series resolving to one name
 * each keep their PC folder name verbatim (AIO added the hid suffix precisely
 * because the titles collide).
 */
function verbatimName(series) {
  return sanitizeSegment(series.folder);
}

module.exports = {
  MAX_SEGMENT_BYTES,
  NAMING_POLICIES,
  utf8Bytes,
  sanitizeSegment,
  segmentError,
  stripHidSuffix,
  slotKey,
  compactKey,
  deriveSearchQuery,
  findSeriesEntry,
  resolveTargetName,
  verbatimName,
};
