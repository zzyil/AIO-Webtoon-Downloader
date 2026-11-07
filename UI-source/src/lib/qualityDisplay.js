// ============================================================
// qualityDisplay — what a source's image-quality rating looks like on screen.
//
// ONE set of rules for every surface that shows a search source's rating, so
// they can't drift:
//   * components/SearchSourceCard.jsx (the Search tab), over the raw
//     --search-json source (quality_basis, img_quality_score,
//     img_quality_metadata).
//   * components/sync/findView.js (Device Sync's Find sources / coverage
//     sheets), over find-sources.js's compact source (qualityBasis, rating,
//     qualityReason).
// Android mirrors these rules in core/SearchModels.kt (grep measuredRating).
//
// The producer is the search's matched-page ranker (sites/match_session.py,
// rate_sources / quality_metadata): a rating is measured only when the source's
// pages were compared with other sites' copies of the same pages (basis
// "matched", or "partial" = compared within a smaller group). Every other
// basis has no comparable number, and (user decision 2026-10-21, plan
// sync-temp/plans/image-ranker-rework.md "P4 design") shows a reason label
// instead: a seed prior next to measured ratings reads as a measurement.
// ============================================================

// "chapter_probe" = the pre-rework ranker's measured basis; Device Sync rows
// persisted before the rework still carry it (and a measured rating).
const MEASURED = new Set(["matched", "partial", "chapter_probe"]);

/** The rating to show as a number, or null when this source has no measured one. */
export function measuredRating(basis, score) {
  return MEASURED.has(basis) && Number.isFinite(score) ? score : null;
}

// Per-reason explanations (match_session.SourceVerdict.reason; the list is in
// its _finalize, grep `reason=`). Unknown reasons fall back to the basis text.
const REASON_TITLES = {
  list_failed: "The site's chapter list failed to load in this search.",
  no_readable: "The site lists no readable chapters (publisher link-outs or locked chapters only).",
  pages_failed: "Every page fetch failed on the sampled chapters (the site's image host looks down).",
  list_timeout: "The site's chapter list didn't arrive in time.",
  list_queued: "The search's time ran out before the site's chapter list was fetched.",
  no_shared_chapter: "It shares no chapter number with the other sites.",
  pages_timeout: "Its pages didn't arrive in time.",
  pages_cut: "The search's time ran out before its pages were compared.",
  not_run: "The search's time ran out before its pages were compared.",
  strip_short: "Its strips were too short to line up with the other sites' strips.",
  strip_unaligned: "Its strips didn't line up with any other site's (likely a different cut or a paged re-format).",
  strip_unregistered: "Its strips lined up with another site's, but no section of them could be compared.",
};

const BASIS = {
  solo: { label: "not compared", title: "The only site that served pages, so there was nothing to compare it with." },
  unmatched: { label: "not compared", title: "None of its pages matched another site's copy of the same page (different chapters, or a different cut)." },
  pending: { label: "not measured", title: "It wasn't measured in this search." },
  unreachable: { label: "unreachable", title: "The site didn't serve pages in this search. It is left out of fallbacks." },
  seed: { label: "not measured", title: "Not probed in this search: it was outside the sources the search compares." },
  cover: { label: "not measured", title: "Not measured from chapter pages." },
};

/**
 * {label, title} for a source WITHOUT a measured rating (shown where the
 * number would be), or null when the rating is measured. `title` is the
 * tooltip: the basis, then the reason when the probe recorded one.
 */
export function qualityStatus(basis, reason) {
  if (MEASURED.has(basis)) return null;
  const b = BASIS[basis] || BASIS.seed;
  const why = reason && REASON_TITLES[reason];
  let label = b.label;
  if (basis === "pending" && reason && /timeout/.test(reason)) label = "timed out";
  return { label, title: why ? `${b.title} ${why}` : b.title };
}

/** Tooltip line for a measured rating's basis ("partial" ratings are relative to their own group). */
export function measuredTitle(basis) {
  if (basis === "partial")
    return "Compared only within a smaller group of sites that share no pages with the main group, so the rating is relative to that group.";
  return "Measured on pages matched across sites; 0.95 = the best copy found for this series.";
}

/**
 * The "same as …" note (user decision 2026-10-21: two tiers), from
 * img_quality_metadata.same_file / same_scan (site names). Same file =
 * identical bytes or pixels; same scan = the same scan re-encoded or resized
 * by another host (match_session.SAME_SCAN_DB). null when neither.
 */
export function sameNote(meta) {
  const file = (meta && meta.same_file) || [];
  const scan = (meta && meta.same_scan) || [];
  if (!file.length && !scan.length) return null;
  const parts = [];
  if (file.length) parts.push(`same file as ${file.join(", ")}`);
  if (scan.length) parts.push(`same scan as ${scan.join(", ")}`);
  const text = parts.join("; ");
  return {
    label: text.charAt(0).toUpperCase() + text.slice(1),
    title:
      (file.length ? `Identical pages to ${file.join(", ")}. ` : "") +
      (scan.length ? `The same scan as ${scan.join(", ")}, re-encoded or resized by a different host. ` : "") +
      "Copies of one scan look alike; the rating says which copy is cleanest.",
  };
}

/**
 * The "Other edition" badge (user decision 2026-10-21) for a source whose pages
 * are a different edition from most sites' (match_session._editions: the color
 * check), or null. `colored` = this source's pages are in color
 * (img_quality_metadata.colored); null/undefined = unknown.
 */
export function editionNote(edition, colored) {
  if (edition !== "alternate") return null;
  const how =
    colored === true
      ? "Its pages are in color where most sites serve this series in black-and-white."
      : colored === false
        ? "Its pages are black-and-white where most sites serve this series in color."
        : "Its pages differ from most sites' copies.";
  return { label: "Other edition", title: `${how} It is never a fallback for other sites; downloading from it falls back only to the same edition.` };
}
