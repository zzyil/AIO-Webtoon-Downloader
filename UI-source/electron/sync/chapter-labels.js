// ============================================================
// DEVICE SYNC — CHAPTER LABELS (pure)
//
// Owns what a file name means as a chapter: labelFile() returns a unit
// (chapter / range / volume / whole / unknown) and, for chapters, a
// CONSERVATIVE label. Delete coverage (planner.js, parent rule 5) and loss
// detection compare these labels, so the label must never merge two real
// chapters.
//
// WHY NOT library.js's numbers: _normalizeChapterToken goes through
// parseFloat, which makes "005.10" and "005.1" both "5.1" (library.js, grep
// _normalizeChapterToken). For the update check that only costs a re-check;
// here it would let Ch 5.1's file "cover" Ch 5.10 and pre-select the delete
// of a real chapter. So: leading zeros are stripped from the integer part,
// "~" becomes ".", an all-zero decimal is dropped ("5.0" → "5"), any other
// decimal is kept verbatim ("5.10" ≠ "5.1"), a letter suffix is lowercased.
//
// RECOGNITION PARITY: the Komikku and legacy branches recognize exactly the
// files library.js:extractChaptersFromFiles recognizes (same KOMIKKU_CH_RE,
// same last-" Ch " split). tools/_test_device_sync_core.js asserts it.
// Order after those: device conventions seen on the old tablet (CompareManga
// census: `_<site>_Ch_N`, `Chap/Chapter N`, `#N -`, `Vol N`, `<folder>.<ext>`),
// then the user's syncChapterPatterns.
//
// Read by: planner.js, analysis.js, service.js (sync:label-preview).
// Depends on: library.js KOMIKKU_CH_RE, series-merge.js compareChapterLabels,
// naming.js compactKey/stripHidSuffix.
// ============================================================

const { KOMIKKU_CH_RE } = require("../library");
const { compareChapterLabels } = require("../series-merge");
const { compactKey, stripHidSuffix } = require("./naming");

// The sign only exists for parity: extractChaptersFromFiles accepts any
// parseFloat-able legacy token, so "Ch -1" is a chapter there too.
const LABEL_TOKEN_RE = /^(-?)(\d+)(?:\.(\d+))?([a-z])?$/i;

/**
 * Conservative canonical form of a chapter token ("005", "5~5", "005.10",
 * "012a"), or null when the token isn't one. See the header for why.
 */
function conservativeLabel(token) {
  const m = String(token == null ? "" : token).trim().replace("~", ".").match(LABEL_TOKEN_RE);
  if (!m) return null;
  const int = m[2].replace(/^0+(?=\d)/, "");
  const dec = m[3] && !/^0+$/.test(m[3]) ? `.${m[3]}` : "";
  const suffix = m[4] ? m[4].toLowerCase() : "";
  const sign = m[1] && (int !== "0" || dec) ? "-" : "";
  return `${sign}${int}${dec}${suffix}`;
}

function _nameNoExt(name) {
  return String(name).replace(/\.[^.]+$/, "");
}

function _extOf(name) {
  const m = String(name).match(/\.([^./\\]+)$/);
  return m ? m[1].toLowerCase() : "";
}

// Device conventions, in the order the old tablet's names need them. Each is
// matched against the name without its extension.
const DEVICE_CONVENTIONS = Object.freeze([
  // "<Series>_mangafire_Ch_12" — the old scripts' underscored form.
  { id: "underscored", unit: "chapter", re: /_[a-z]+_Ch_(\d+(?:[.~]\d+)?)\s*$/i },
  { id: "chap", unit: "chapter", re: /^Chap(?:ter)?\.?\s+(\d+(?:[.~]\d+)?)\b/i },
  // "#12 - [Season 2] Ep. 3": the running index, which is what the file
  // order follows; a series that wants the episode needs a custom pattern.
  { id: "hash", unit: "chapter", re: /^#(\d+(?:\.\d+)?)\s*-/ },
  // "Vol 3" and "Vol 3 <title>" (the census found 8 of the latter that no
  // CompareManga regex parsed). Volumes are a unit of their own: never
  // compared with chapters, never pre-selected for delete.
  { id: "volume", unit: "volume", re: /^Vol(?:ume)?\.?\s*(\d+(?:\.\d+)?)\b/i },
]);

/**
 * Parse syncChapterPatterns: one regex per line, each with a named group
 * `ch`, matched case-insensitively against the file name without extension.
 * Blank lines and lines starting with "#" are skipped. Invalid lines are
 * reported, never thrown, so one bad line can't disable the others.
 * @returns {{patterns: RegExp[], errors: Array<{line:number, text:string, message:string}>}}
 */
function parseChapterPatterns(text) {
  const patterns = [];
  const errors = [];
  const lines = String(text == null ? "" : text).split(/\r?\n/);
  lines.forEach((raw, i) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    if (line.length > 500) {
      errors.push({ line: i + 1, text: line.slice(0, 60), message: "pattern is longer than 500 characters" });
      return;
    }
    let re;
    try {
      re = new RegExp(line, "i");
    } catch (e) {
      errors.push({ line: i + 1, text: line, message: e.message });
      return;
    }
    if (!/\(\?<ch>/.test(line)) {
      errors.push({ line: i + 1, text: line, message: "pattern needs a named group (?<ch>…)" });
      return;
    }
    patterns.push(re);
  });
  return { patterns, errors };
}

function _numeric(label) {
  const n = parseFloat(label);
  return Number.isFinite(n) ? n : Infinity;
}

/**
 * What a file name means as a chapter.
 *
 * @param {string} name        File name (depth-1, with extension).
 * @param {string} folderName  Its series folder (for the "whole" unit).
 * @param {RegExp[]} [patterns] From parseChapterPatterns.
 * @returns {{unit:'chapter'|'range'|'volume'|'whole'|'unknown', label:?string,
 *   start?:string, end?:string, sortValue:number, convention:string, tilde:boolean}}
 *   label is the conservative chapter label (chapter), "<start>-<end>"
 *   (range), the volume number (volume), else null. tilde marks a legacy
 *   "~" decimal, which Komikku's ChapterRecognition misreads. appLabel
 *   (chapters only) = the name is one AIO writes (Komikku "Ch.N…", legacy
 *   "Title Ch N" with nothing after N), so the label is the chapter's own
 *   number, not a running index ("#N -") or a guess from a longer token.
 *   planner.js lets only such a label pre-select a foreign file's
 *   replacement (grep appLabel).
 */
function labelFile(name, folderName, patterns) {
  const stem = _nameNoExt(name);

  const k = stem.match(KOMIKKU_CH_RE);
  if (k) {
    const label = conservativeLabel(k[1]);
    if (label != null) return _chapter(label, "komikku", false, true);
  }

  const chIdx = stem.lastIndexOf(" Ch ");
  if (chIdx !== -1) {
    const chPart = stem.slice(chIdx + 4).trim();
    if (chPart) {
      // Same range shape as extractChaptersFromFiles.
      const r = chPart.match(/^(\d+(?:~\d+)?)\s*-\s*(\d+(?:~\d+)?)$/);
      if (r) {
        const start = conservativeLabel(r[1]);
        const end = conservativeLabel(r[2]);
        return {
          unit: "range",
          label: `${start}-${end}`,
          start,
          end,
          sortValue: _numeric(start),
          convention: "legacy",
          tilde: chPart.includes("~"),
        };
      }
      // extractChaptersFromFiles accepts anything parseFloat reads as a
      // number ("5 - Extra" → 5); the label is the leading number.
      if (!Number.isNaN(parseFloat(chPart.replace("~", ".")))) {
        const lead = chPart.match(/^(\d+(?:[.~]\d+)?[a-z]?)(?![a-z])/i);
        const label =
          (lead && conservativeLabel(lead[1])) ||
          conservativeLabel(String(parseFloat(chPart.replace("~", "."))));
        // "Title Ch 5" is AIO's legacy name; "Title Ch 5 - Extra" is not one it
        // writes, so its leading number is a guess (appLabel false).
        const whole = /^\d+(?:[.~]\d+)?[a-z]?$/i.test(chPart);
        if (label != null) return _chapter(label, "legacy", chPart.includes("~"), whole);
      }
    }
  }

  for (const c of DEVICE_CONVENTIONS) {
    const m = stem.match(c.re);
    if (!m) continue;
    const label = conservativeLabel(m[1]);
    if (label == null) continue;
    if (c.unit === "volume") {
      return { unit: "volume", label, sortValue: _numeric(label), convention: c.id, tilde: false };
    }
    return _chapter(label, c.id, m[1].includes("~"), false);
  }

  if (folderName) {
    // The folder name with or without its "(hid=…)" suffix: AIO adds the
    // suffix on a title collision, and a bundled file may carry either.
    const stemKey = compactKey(stem);
    const wholeKeys = [compactKey(stripHidSuffix(folderName)), compactKey(folderName)];
    if (stemKey && wholeKeys.includes(stemKey)) {
      return { unit: "whole", label: null, sortValue: Infinity, convention: "whole", tilde: false };
    }
  }

  for (const re of patterns || []) {
    const m = stem.match(re);
    const token = m && m.groups && m.groups.ch;
    const label = token != null ? conservativeLabel(token) : null;
    if (label != null) return _chapter(label, "custom", String(token).includes("~"), false);
  }

  return { unit: "unknown", label: null, sortValue: Infinity, convention: "none", tilde: false };
}

function _chapter(label, convention, tilde, appLabel) {
  return { unit: "chapter", label, sortValue: _numeric(label), convention, tilde, appLabel };
}

const _collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/**
 * Chapter-label order: series-merge.js's numeric comparator, tie-broken
 * by a numeric collator so "5a" / "5b" and equal numbers order stably
 * (main can't import the renderer's naturalCompare).
 */
function compareLabels(a, b) {
  return compareChapterLabels(a, b) || _collator.compare(String(a), String(b));
}

/**
 * sync:label-preview's answer for one name: the unit and label, or the
 * pattern errors when the supplied patterns don't parse.
 */
function labelPreview({ name, folderName, patterns }) {
  const parsed = parseChapterPatterns(patterns);
  if (parsed.errors.length) return { error: parsed.errors };
  const r = labelFile(String(name || ""), folderName, parsed.patterns);
  const out = { unit: r.unit, label: r.label };
  if (r.start != null) out.start = r.start;
  if (r.end != null) out.end = r.end;
  return out;
}

module.exports = {
  conservativeLabel,
  parseChapterPatterns,
  labelFile,
  compareLabels,
  labelPreview,
  extOf: _extOf,
};
