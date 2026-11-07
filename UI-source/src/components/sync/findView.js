// ============================================================
// findView — pure view model for finding sources of folders that are only
// on a target (UI pass U4; spec §4 "Find sources (batch)" sheet and the
// single find's Search banner)
//
// Rows come from the batch runner (electron/sync/find-sources.js, persisted
// per target; the hook keeps them in findRows[targetId] by folder):
//   {folder, query, include, state, candidates[≤5], pickUrl, multiSource,
//    pinnedUrl, skip, error, searchedAt}
//   candidate {title, canonicalTitle, year, best, sources[every site]}
//   source {site, url, title, cover, rating, chapters, qualityBasis,
//           qualityReason, edition, titleMatch}
// (the runner's header owns the field meanings).
//
// WHAT A QUEUED ROW DOWNLOADS (rowQueueArgs): the Search tab's rules,
// reused, not re-derived (SearchSourceCard.handleDownload): the picked site
// is the URL; multiSource is the toggle the row's OWN search ran with (the
// Search tab's dialog defaults to the search's snapshot, and this sheet has
// no dialog); when that is on, the series' other sites ride as
// prefetchedAlts. A pinned URL is a series the search didn't pick, so it
// sends nothing and follows the download defaults.
// The sheet shows exactly the target's CURRENT device-only folders
// (summary.deviceOnly), each joined with its row when one exists; a folder
// the runner hasn't seen yet reads "not searched".
//
// READY vs CHECK MATCH (F2, user choice 2026-10-17): Queue takes a row only
// when the user has said which series it is (a pickUrl or a pin) or the
// search was sure on its own (matchReady). A guessed series downloads a
// whole wrong manga into the folder, so a doubtful row costs one click.
//
// LINKED is asked from the config, not remembered (U4-11): a folder the
// target already holds an alias for (sync/service.js OPS["add-alias"]) was
// queued before, so it shows the alias URL and can't be queued twice. The
// folder leaves device-only once the download lands and the planner binds
// it through that alias (planner step 5).
//
// CHAPTER COVERAGE (coverage plan C3, sync-temp/plans/find-sources-coverage.md):
// the runner keeps the search's winner_chapter_map on candidate 0 only
// (`chapterMap`, find-sources.js compactCandidates). rowCoverage says what
// the open site list can show for a row; siteCoverageState places one site
// in that map. Both are copy-free; the sheet words them.
//
// Consumers: FindSourcesSheet.jsx, App.jsx (findQueueArgs), the DEV gallery.
// Tested by tools/_test_device_sync_view.js.
// ============================================================

import { buildPrefetchedAlts } from "../../lib/prefetchedAlts.js";
import { measuredRating, measuredTitle, qualityStatus } from "../../lib/qualityDisplay.js";

/**
 * The series match a row's download comes from: the FIRST candidate listing
 * pickUrl, else candidate 0 (no pick, or a pick no result lists any more).
 * null when pinned or nothing was found.
 */
export function rowCandidate(row) {
  if (!row || row.pinnedUrl) return null;
  const list = row.candidates || [];
  if (row.pickUrl) {
    const c = list.find((x) => (x.sources || []).some((s) => s && s.url === row.pickUrl));
    if (c) return c;
  }
  return list[0] || null;
}

/** The site a row downloads from (a source of rowCandidate), or null. */
export function rowSource(row) {
  const c = rowCandidate(row);
  if (!c) return null;
  const picked = row.pickUrl ? (c.sources || []).find((s) => s && s.url === row.pickUrl) : null;
  return picked || c.best || (c.sources || [])[0] || null;
}

/** The URL a row would queue: a pinned URL wins over the picked site. */
export function rowUrl(row) {
  if (!row) return null;
  if (row.pinnedUrl) return row.pinnedUrl;
  const s = rowSource(row);
  return (s && s.url) || null;
}

/**
 * Download args a row adds (before findQueueArgs): {} when pinned or when
 * the row predates the runner recording multiSource (a migrated row not yet
 * re-searched: the download defaults decide); else {multiSource} plus
 * prefetchedAlts when it is on and the series has another site. A single-
 * site series still sends multiSource: true, like the Search tab, and
 * aio-dl.py then looks for fallbacks itself.
 */
export function rowQueueArgs(row) {
  if (!row || row.pinnedUrl || typeof row.multiSource !== "boolean") return {};
  const c = rowCandidate(row);
  const s = rowSource(row);
  if (!c || !s) return {};
  const args = { multiSource: row.multiSource };
  if (row.multiSource) {
    const alts = buildPrefetchedAlts(c.sources, s, c.canonicalTitle, c.year);
    if (alts) args.prefetchedAlts = alts;
  }
  return args;
}

// The search orchestrator's own cut-offs for "this title match is strong"
// (sites/search_orchestrator.py IS_OFFICIAL_REQUIRES_TITLE_MATCH) and "these
// two are too close to call on title alone" (TIEBREAKER_WINDOW). Copied, not
// read: the runner gets only the search JSON. Keep in sync by grep.
const READY_TITLE_MATCH = 0.85;
const READY_LEAD = 0.1;

/** A match's title score: its best site's (the search ranks matches by it). null when unknown. */
export function matchTitleScore(c) {
  let best = null;
  for (const s of (c && c.sources) || []) if (s && typeof s.titleMatch === "number" && (best == null || s.titleMatch > best)) best = s.titleMatch;
  return best;
}

/**
 * The search was sure on its own: match 0 scores >= READY_TITLE_MATCH and
 * leads match 1 by >= READY_LEAD (or has no rival). A row searched before
 * titleMatch was recorded is never ready (unknown is not sure).
 */
export function matchReady(row) {
  const list = (row && row.candidates) || [];
  const top = matchTitleScore(list[0]);
  if (top == null || top < READY_TITLE_MATCH) return false;
  const next = list.length > 1 ? matchTitleScore(list[1]) : null;
  // 1e-9: 0.95 - 0.85 is 0.0999… in floating point.
  return next == null || top - next >= READY_LEAD - 1e-9;
}

/**
 * The picked match's sites in search order for the picker, each with
 * `picked` and its fallback rank (1-based, the order the download tries
 * them) when the row's search ran with fallback sites on, else null.
 * [] when pinned or nothing was found.
 */
export function rowSites(row) {
  const c = rowCandidate(row);
  const picked = rowSource(row);
  if (!c || !picked) return [];
  let n = 0;
  return (c.sources || []).filter(Boolean).map((s) => {
    const isPicked = s.url === picked.url;
    if (!isPicked) n += 1;
    return { source: s, picked: isPicked, fallback: !isPicked && row.multiSource === true ? n : null };
  });
}

/** The row's other series matches (not the picked one), in search order. */
export function otherMatches(row) {
  const c = rowCandidate(row);
  return ((row && row.candidates) || []).filter((x) => x && x !== c);
}

/**
 * What a row's open site list can say about chapter coverage:
 *   {kind: "fallbacks-off"}  searched with fallback sites off: the search
 *                            builds no map then (aio_search_cli.py, only
 *                            with --multi-source)
 *   {kind: "other-match"}    the pick is another match ("Not this series?");
 *                            only match 0 is compared
 *   {kind: "map", map}       match 0 carries the map
 *   {kind: "search-again"}   no map on match 0: searched before the runner
 *                            kept it (incl. a migrated row, multiSource null)
 * null when pinned or nothing was found. Checked in this order: fallbacks
 * off explains every missing map, so it wins over other-match.
 */
export function rowCoverage(row) {
  const c = rowCandidate(row);
  if (!c) return null;
  if (row.multiSource === false) return { kind: "fallbacks-off" };
  if (c !== (row.candidates || [])[0]) return { kind: "other-match" };
  if (c.chapterMap) return { kind: "map", map: c.chapterMap };
  return { kind: "search-again" };
}

/**
 * One site of the picked match against the coverage model
 * (lib/chapterCoverage.js coverageModel), matched by site name (the map has
 * no URLs; the aligner already merged one series listed twice on a site):
 *   {kind: "covered", site}     strip + count
 *   {kind: "low-match", site}   numbering overlaps the anchor < 50 %: its
 *                               strip would be partial by construction, so
 *                               the match % and count instead
 *   {kind: "empty", site}       the site listed no chapters
 *   {kind: "not-compared"}      not in the map (its rating cleared neither
 *                               the seed nor the probe bar, plan User call 2);
 *                               still a fallback
 */
export function siteCoverageState(model, source) {
  const s = model && source ? model.sites.find((x) => x.site === source.site) : null;
  if (!s) return { kind: "not-compared" };
  if (s.empty) return { kind: "empty", site: s };
  if (s.lowMatch) return { kind: "low-match", site: s };
  return { kind: "covered", site: s };
}

/** The target's alias for a device folder, or null. */
export function aliasFor(target, folder) {
  const list = (target && Array.isArray(target.aliases) && target.aliases) || [];
  return list.find((a) => a && a.deviceFolder === folder) || null;
}

/**
 * One view row per current device-only folder, in the ledger's order.
 * state: unsearched | queued | searching | done | error | cancelled (runner
 * states, plus `unsearched` for a folder without a row).
 */
export function findRows(items, rowsByFolder, target) {
  const rows = rowsByFolder || {};
  return (items || []).map((d) => {
    const r = rows[d.name] || null;
    const alias = aliasFor(target, d.name);
    const url = rowUrl(r);
    const skip = !!(r && r.skip);
    // confirmed: the user named the series (a pick or a pin). ready: the
    // search was sure. needsCheck: found, but neither, so Queue skips it.
    const confirmed = !!(r && (r.pinnedUrl || r.pickUrl));
    const ready = !!(r && !r.pinnedUrl && matchReady(r));
    const needsCheck = !!url && !confirmed && !ready && !skip && !alias;
    return {
      key: d.key,
      folder: d.name,
      query: (r && r.query) || d.query || d.name,
      state: r ? r.state : "unsearched",
      candidates: (r && r.candidates) || [],
      pickUrl: (r && r.pickUrl) || null,
      multiSource: r && typeof r.multiSource === "boolean" ? r.multiSource : null,
      pinnedUrl: (r && r.pinnedUrl) || null,
      error: (r && r.error) || null,
      skip,
      url,
      linked: alias ? alias.url || alias.identityKey || alias.pcFolder || "" : null,
      confirmed,
      ready,
      needsCheck,
      queueable: !skip && !alias && !!url && (confirmed || ready),
    };
  });
}

/** Folders a Search press would run: not skipped, not linked, not already found. */
export function searchable(rows) {
  return (rows || []).filter((r) => !r.skip && r.linked == null && r.state !== "done");
}

// The two helpers below take a candidate (they read its best site) or a
// source itself (a source has no `best` key).
function _site(x) {
  return x && "best" in x ? x.best : x;
}

// A row saved before qualityBasis existed has none: its rating came from the
// pre-rework probe, which only stored a measured or seed number. It reads as
// unmeasured ("seed"), like a source the search did not probe.
function _basis(b) {
  return (b && b.qualityBasis) || "seed";
}

/**
 * The rating text: "0.92" when measured, else the reason label ("not
 * compared", "unreachable", …) — the Search tab's rule (lib/qualityDisplay.js).
 * Never the old `composite` (that was the title match; see the runner's header).
 */
export function ratingText(c) {
  const b = _site(c);
  if (!b) return "";
  const r = measuredRating(_basis(b), b.rating);
  if (r != null) return r.toFixed(2);
  return qualityStatus(_basis(b), b.qualityReason)?.label || "";
}

/** "0.92 · ch.97" (or "not compared · ch.97") for a candidate's best site, or for one site. */
export function candidateScore(c) {
  const b = _site(c);
  if (!b) return "";
  const parts = [];
  const r = ratingText(b);
  if (r) parts.push(r);
  if (b.chapters != null) parts.push(`ch.${b.chapters}`);
  return parts.join(" · ");
}

/** Tooltip for the rating text: what a measured rating means, or why there is none. */
export function ratingTitle(c) {
  const b = _site(c);
  if (!b) return undefined;
  const basis = _basis(b);
  if (measuredRating(basis, b.rating) != null) return measuredTitle(basis);
  return qualityStatus(basis, b.qualityReason)?.title;
}

/** True when the text is a reason label, not a number (the sheets mute it). */
export function unmeasured(c) {
  const b = _site(c);
  return !!b && measuredRating(_basis(b), b.rating) == null;
}

/**
 * Extra download args for a series queued for a target (U4-8): a Komikku
 * reader downloads in Komikku mode; every other reader keeps the defaults.
 */
export function findQueueArgs(target) {
  return target && target.profile === "komikku" ? { komikku: true } : {};
}

const WAITING_COPY = {
  "check-all": "Waiting for Library Check All to finish",
  search: "Waiting for the Search tab's search to finish",
};

/** The sheet's status line for the runner's batch status (hook `find`). */
export function findStatusLine(find, targetId, rows) {
  const list = rows || [];
  if (find && find.targetId === targetId) {
    if (find.waiting) return WAITING_COPY[find.waiting] || "Waiting";
    return `Searching ${Math.min(find.done + 1, find.total)} of ${find.total}…`;
  }
  const found = list.filter((r) => r.state === "done" && r.candidates.length).length;
  const searched = list.filter((r) => r.state === "done" || r.state === "error").length;
  if (!searched) return null;
  return `${found} of ${list.length} found a source.`;
}
