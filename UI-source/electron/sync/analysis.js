// ============================================================
// DEVICE SYNC — DELTA TAGS AND ANOMALIES (pure)
//
// Owns what a series' PC/device difference MEANS, as opposed to what to do
// about it (planner.js):
//   * deltaTags(pcLabeled, devLabeled) — why the chapter sets differ
//     (pc-ahead, device-ahead, sub-chapter-split, ...). Three of them mean the
//     labels can't be trusted for delete coverage: DELETE_BLOCKING_TAGS.
//   * contentAnomalies(...) — the anomalies that come from file contents and
//     names (format, size, bundles, reader compatibility, unparsed files).
//   * makeAnomaly(kind, seriesKey, data) — the one builder every anomaly goes
//     through, planner-raised ones included, so kind, severity and id are
//     defined in one table (ANOMALY_KINDS).
//
// Anomaly ids are `${kind}:${seriesKey}`: stable across plans, which is what
// a target's `acknowledged` list stores (sync:config-op ack-anomaly).
//
// The tag heuristics replace CompareManga _delta_findings' hand reading; each
// is commented with the case it exists for. They only ever REMOVE
// preselection (never add a delete), so a false positive costs a click.
//
// Read by: planner.js. Depends on: chapter-labels.js (labels come labeled).
// ============================================================

const { compareLabels } = require("./chapter-labels");

// Labels on these tags don't line up with the PC's, so "the PC covers label
// L" proves nothing and rule 5 pre-selects no delete in the series.
const DELETE_BLOCKING_TAGS = Object.freeze(new Set(["numbering-mismatch", "different-start", "device-ahead"]));

const TAG_KINDS = Object.freeze([
  "pc-ahead",
  "device-ahead",
  "pc-gaps",
  "sub-chapter-split",
  "ch0-asymmetry",
  "different-start",
  "numbering-mismatch",
  "volumes-not-comparable",
  "mixed",
]);

// severity: error = shown as needing attention until acknowledged (it used to
// block the prompt's Sync now, removed by approved design D2); warning =
// shown prominently; info = shown in the series sheet only.
const ANOMALY_KINDS = Object.freeze({
  "fork-collision": { severity: "info" },
  "merge-in-library": { severity: "warning" },
  "format-mismatch": { severity: "warning" },
  "size-mismatch": { severity: "warning" },
  "single-bundled-file": { severity: "info" },
  "empty-pc-folder": { severity: "warning" },
  "malformed-json": { severity: "warning" },
  "image-only": { severity: "warning" },
  "reader-compat": { severity: "warning" },
  "unparsed-files": { severity: "info" },
  "name-too-long": { severity: "error" },
  // A PC folder name with nothing left after sanitizing (only forbidden
  // characters or dots).
  "name-invalid": { severity: "error" },
  "name-taken": { severity: "error" },
  "folder-unreadable": { severity: "error" },
  "loose-root-files": { severity: "info" },
});

// The seriesKey of target-level anomalies (loose files in the root).
const ROOT_SERIES_KEY = "(root)";

/**
 * Build one anomaly. Throws on an unknown kind: a typo would otherwise make
 * an anomaly no acknowledgment can ever match.
 * @returns {{id:string, kind:string, severity:string, seriesKey:string, data:object, acknowledged:boolean}}
 */
function makeAnomaly(kind, seriesKey, data, acknowledged) {
  const spec = ANOMALY_KINDS[kind];
  if (!spec) throw new Error(`unknown sync anomaly kind: ${kind}`);
  const ack = acknowledged instanceof Set ? acknowledged : new Set(acknowledged || []);
  const id = `${kind}:${seriesKey}`;
  return { id, kind, severity: spec.severity, seriesKey, data: data || {}, acknowledged: ack.has(id) };
}

function _num(label) {
  const n = parseFloat(label);
  return Number.isFinite(n) ? n : null;
}

function _chapterLabels(labeled) {
  const out = new Set();
  for (const f of labeled || []) if (f.unit === "chapter" && f.label != null) out.add(f.label);
  return out;
}

function _extremes(labels) {
  let min = null;
  let max = null;
  for (const l of labels) {
    const n = _num(l);
    if (n == null) continue;
    if (min == null || n < min) min = n;
    if (max == null || n > max) max = n;
  }
  return { min, max };
}

function _hasValue(labels, value) {
  for (const l of labels) if (_num(l) === value) return true;
  return false;
}

// "5" vs "5.1"/"5.2": the integer part of a decimal label (null for "5a"
// and plain integers).
function _splitBase(label) {
  const m = String(label).match(/^(-?\d+)\.\d+$/);
  return m ? m[1] : null;
}

function _subChapterSplit(a, b) {
  // A whole chapter n on side a whose parts n.x sit on side b instead.
  for (const l of b) {
    const base = _splitBase(l);
    if (base != null && a.has(base) && !b.has(base)) return true;
  }
  return false;
}

/**
 * Why a series' chapter sets differ. Both inputs are labelFile() results
 * (chapter-format files only). Returns tags in TAG_KINDS order.
 */
function deltaTags(pcLabeled, devLabeled) {
  const P = _chapterLabels(pcLabeled);
  const D = _chapterLabels(devLabeled);
  const tags = new Set();
  const pcVolumes = (pcLabeled || []).some((f) => f.unit === "volume");
  const devVolumes = (devLabeled || []).some((f) => f.unit === "volume");
  // A volume holds an unknown run of chapters, so the two sides can't be
  // compared label by label.
  if ((pcVolumes && D.size) || (devVolumes && P.size)) tags.add("volumes-not-comparable");
  // Two naming conventions in one device folder (legacy "Title Ch 5" next to
  // Komikku "Ch.005"): usually two sync generations, often with duplicates.
  const conventions = new Set(
    (devLabeled || []).filter((f) => f.unit === "chapter" || f.unit === "range").map((f) => f.convention),
  );
  if (conventions.size > 1) tags.add("mixed");

  if (P.size && D.size) {
    const p = _extremes(P);
    const d = _extremes(D);
    if (p.max != null && d.max != null) {
      if (p.max > d.max) tags.add("pc-ahead");
      // The device has later chapters than the PC: they exist nowhere else,
      // so this is the content-loss risk.
      if (d.max > p.max) tags.add("device-ahead");
    }
    // Device chapters inside the PC's own range that the PC lacks.
    for (const l of D) {
      const n = _num(l);
      if (!P.has(l) && n != null && p.min != null && n > p.min && n < p.max) {
        tags.add("pc-gaps");
        break;
      }
    }
    if (_subChapterSplit(P, D) || _subChapterSplit(D, P)) tags.add("sub-chapter-split");
    if (P.has("0") !== D.has("0")) tags.add("ch0-asymmetry");
    // Different first chapter (0 aside) that neither side has: an offset
    // numbering (a season restart, a running index). A device that merely
    // dropped its read chapters is not tagged, because the PC still holds
    // the device's first label.
    const pNon0 = new Set([...P].filter((l) => _num(l) !== 0));
    const dNon0 = new Set([...D].filter((l) => _num(l) !== 0));
    const pm = _extremes(pNon0).min;
    const dm = _extremes(dNon0).min;
    if (pm != null && dm != null && Math.abs(pm - dm) >= 2 && !_hasValue(pNon0, dm) && !_hasValue(dNon0, pm)) {
      tags.add("different-start");
    }
    // Barely overlapping label sets of real size: the two sides number the
    // series differently, so label equality is coincidence.
    if (P.size >= 5 && D.size >= 5) {
      let both = 0;
      for (const l of D) if (P.has(l)) both += 1;
      if (both / Math.min(P.size, D.size) < 0.5) tags.add("numbering-mismatch");
    }
  }
  return TAG_KINDS.filter((t) => tags.has(t));
}

function deleteBlockingTags(tags) {
  return (tags || []).filter((t) => DELETE_BLOCKING_TAGS.has(t));
}

function _byLabel(labeled) {
  const m = new Map();
  for (const f of labeled || []) {
    if (f.unit !== "chapter" || f.label == null) continue;
    if (!m.has(f.label)) m.set(f.label, f);
  }
  return m;
}

/**
 * The anomalies a series' files raise. Planner-raised kinds (names, forks,
 * holds) are built by planner.js through makeAnomaly.
 *
 * @param {object} a
 * @param {string} a.seriesKey
 * @param {Array<object>} a.pcLabeled     PC chapter-format files, labeled, each with `mirrored`.
 * @param {Array<object>} a.devLabeled    Device chapter-format files, labeled.
 * @param {string[]} a.pcSidecars         Sidecar names the PC folder holds.
 * @param {string[]} a.devNames           Every device file name in the folder.
 * @param {string[]} [a.malformedJson]    PC/device JSON files that failed to parse.
 * @param {object} a.cfg                  resolveTargetConfig result.
 * @param {number} a.sizeWarnPct          syncSizeWarnPct (0 = off).
 * @param {Iterable<string>} [a.acknowledged]
 */
function contentAnomalies(a) {
  const out = [];
  const ack = new Set(a.acknowledged || []);
  const cfg = a.cfg;
  const pc = a.pcLabeled || [];
  const dev = a.devLabeled || [];

  const unmirrored = {};
  for (const f of pc) {
    if (f.mirrored) continue;
    unmirrored[f.ext] = (unmirrored[f.ext] || 0) + 1;
  }
  const pcByLabel = _byLabel(pc.filter((f) => f.mirrored));
  const deviceFormats = {};
  for (const f of dev) {
    const p = f.unit === "chapter" ? pcByLabel.get(f.label) : null;
    if (p && p.ext !== f.ext) deviceFormats[f.ext] = (deviceFormats[f.ext] || 0) + 1;
  }
  if (Object.keys(unmirrored).length || Object.keys(deviceFormats).length) {
    out.push(makeAnomaly("format-mismatch", a.seriesKey, { unmirrored, deviceFormats }, ack));
  }

  // Direction-aware: a much smaller device copy is usually a truncated or
  // lower-quality file; a much larger one usually the PC's re-encode.
  if (a.sizeWarnPct > 0) {
    const devByLabel = _byLabel(dev);
    let smaller = 0;
    let larger = 0;
    const examples = [];
    for (const [label, p] of pcByLabel) {
      const d = devByLabel.get(label);
      if (!d || !p.size) continue;
      const pct = ((d.size - p.size) / p.size) * 100;
      if (Math.abs(pct) <= a.sizeWarnPct) continue;
      if (pct < 0) smaller += 1;
      else larger += 1;
      if (examples.length < 5) examples.push({ label, pcSize: p.size, deviceSize: d.size });
    }
    if (smaller || larger) {
      out.push(
        makeAnomaly(
          "size-mismatch",
          a.seriesKey,
          { deviceSmaller: smaller, deviceLarger: larger, direction: smaller >= larger ? "device-smaller" : "device-larger", examples },
          ack,
        ),
      );
    }
  }

  // One file holding the whole series on one side, many chapters on the
  // other: nothing compares, so nothing is offered for delete by label.
  const bundled = (side) => side.length === 1 && side[0].unit !== "chapter";
  const chapters = (side) => side.filter((f) => f.unit === "chapter").length;
  if ((bundled(dev) && chapters(pc) >= 3) || (bundled(pc.filter((f) => f.mirrored)) && chapters(dev) >= 3)) {
    out.push(makeAnomaly("single-bundled-file", a.seriesKey, { side: bundled(dev) ? "device" : "pc" }, ack));
  }

  if (a.malformedJson && a.malformedJson.length) {
    out.push(makeAnomaly("malformed-json", a.seriesKey, { files: a.malformedJson.slice() }, ack));
  }

  const issues = [];
  const flags = cfg.flags || {};
  if (flags.missingCover && !(a.pcSidecars || []).includes("cover.jpg") && !(a.devNames || []).includes("cover.jpg")) {
    issues.push("no-cover");
  }
  if (flags.pdf && (pc.some((f) => f.ext === "pdf") || dev.some((f) => f.ext === "pdf"))) issues.push("pdf");
  if (flags.tilde && (pc.some((f) => f.mirrored && f.tilde) || dev.some((f) => f.tilde))) issues.push("tilde");
  if (issues.length) out.push(makeAnomaly("reader-compat", a.seriesKey, { issues }, ack));

  const unparsedPc = pc.filter((f) => f.mirrored && f.unit === "unknown").map((f) => f.name);
  const unparsedDev = dev.filter((f) => f.unit === "unknown").map((f) => f.name);
  if (unparsedPc.length || unparsedDev.length) {
    out.push(
      makeAnomaly(
        "unparsed-files",
        a.seriesKey,
        { pc: unparsedPc.length, device: unparsedDev.length, examples: [...unparsedPc, ...unparsedDev].slice(0, 5) },
        ack,
      ),
    );
  }
  return out;
}

/** Sorted, de-duplicated chapter labels (for strips and reports). */
function sortedLabels(labels) {
  return [...new Set(labels)].sort(compareLabels);
}

module.exports = {
  DELETE_BLOCKING_TAGS,
  TAG_KINDS,
  ANOMALY_KINDS,
  ROOT_SERIES_KEY,
  makeAnomaly,
  deltaTags,
  deleteBlockingTags,
  contentAnomalies,
  sortedLabels,
};
