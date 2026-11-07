// ============================================================
// prefetchedAlts — the multi-source fallback payload a search-driven
// download carries: the matched series' OTHER sites, so aio-dl.py builds its
// alternatives from them (--multi-source-prefetched) instead of re-searching
// every site.
//
// ONE builder for both callers, so the two can't drift:
//   * SearchSourceCard.jsx handleDownload (the Search tab), over the raw
//     --search-json candidate (sources carry site/url/title/cover, edition,
//     quality_basis).
//   * components/sync/findView.js rowQueueArgs (Device Sync's Find sources),
//     over find-sources.js's compact candidate (same keys; qualityBasis).
// Consumed by electron/downloader.js:start, which writes the payload to
// ~/.aio-dl/cache/ms_prefetched_<id>.json and swaps it for
// `multiSourcePrefetched` (grep prefetchedAlts there). The caller decides
// whether multi-source is on; SearchTab's confirmSearchDownload strips the
// payload when the user turns it off.
// ============================================================

// A source's edition as the search's matched-page probe called it
// (sites/match_session.py): "alternate" = a fan-colored or otherwise different
// edition. Unknown / undecided counts as the original.
const editionOf = (s) => (s && s.edition === "alternate" ? "alternate" : "original");

/**
 * Editions (ranker rework decision 2026-10-18): fallbacks stay in the PRIMARY's
 * edition, so a B&W download never fills a chapter from a colored copy and a
 * colored pick never falls back to B&W. Sources the search found unreachable
 * (`quality_basis` "unreachable": every page fetch failed) are left out too.
 * Same rules as aio_search_cli._filter_and_rank_alt_sources (grep editionOf).
 *
 * @param {Array<{site, url, title?, cover?, edition?, quality_basis?}>} sources  the series' sites in the search's rank order
 * @param {{site, url}} primary  the site being downloaded
 * @param {string|null} title    the search's canonical title
 * @param {number|null} year     the search's canonical year
 * @returns {object|null} {primary:{site,url}, alternatives:[{site,url,title,cover}], title, year},
 *   or null when the series has no other site. Order is kept: the search
 *   ranked the sources (official first, DMCA last, quality descending), and
 *   that order is chapter_merger's alignment input order.
 */
export function buildPrefetchedAlts(sources, primary, title, year) {
  if (!Array.isArray(sources) || sources.length < 2 || !primary) return null;
  const primaryEdition = editionOf(sources.find((s) => s.url === primary.url) || primary);
  const alternatives = sources
    .filter((s) => s.url !== primary.url)
    .filter((s) => editionOf(s) === primaryEdition)
    .filter((s) => (s.quality_basis || s.qualityBasis) !== "unreachable")
    .map((s) => ({
      site: s.site,
      url: s.url,
      // `|| undefined`, not null: JSON.stringify drops the key, which is the
      // shape the Search tab has always sent.
      title: s.title || undefined,
      cover: s.cover || undefined,
    }));
  if (!alternatives.length) return null;
  return {
    primary: { site: primary.site, url: primary.url },
    alternatives,
    title: title || "",
    year: year || null,
  };
}
