// ============================================================
// SEARCH SOURCE CARD
//
// Single source within a candidate (one row in the JSON's
// candidates[i].sources[]). Renders cover, site label, quality
// bar, badges (DMCA / official-publisher / other edition / chapter
// count), the "same file / same scan as …" note, and a Download
// button that pushes to the existing queue. What the rating shows
// (number vs reason label) is lib/qualityDisplay.js's rule, shared
// with Device Sync.
//
// Compact card sized for a horizontal scroll-snap row of 4-8
// sources per candidate. ~160px wide.
// ============================================================

import React, { useEffect, useState } from "react";
import { Button, Badge } from "@/components/ui/primitives";
import { Download, Image as ImageIcon, AlertTriangle, Clock } from "lucide-react";
import { cn } from "@/lib/utils";
import { buildPrefetchedAlts } from "@/lib/prefetchedAlts";
import { editionNote, measuredRating, measuredTitle, qualityStatus, sameNote } from "@/lib/qualityDisplay";

// Map a 0..1 quality score to one of 4 visual tiers. Returns Tailwind
// classes that match the existing primitives' destructive/warning/
// success badge palette so the bar feels native.
function qualityTier(score) {
  if (score == null) return { color: "bg-muted", label: "—" };
  if (score >= 0.7) return { color: "bg-green-500", label: "good" };
  if (score >= 0.4) return { color: "bg-yellow-500", label: "ok" };
  return { color: "bg-red-500", label: "weak" };
}

// Source-level "currently broken / degraded handler" flags. Keyed by
// site name — must match handler.name in the Python `sites/<file>.py`
// (which is a stable identifier, not a display string). When a handler
// is known to be functionally broken (downloads usually fail, output is
// incomplete, etc.) we render a red AlertTriangle next to the site name
// so users see the warning BEFORE clicking Download and wasting a slot
// in their queue. The download path itself isn't blocked — users may
// still try, and the source might recover.
//
// Add/remove entries here when handlers break / get fixed. Each value
// is the tooltip text the user sees on hover.
//
// Entries: none currently. comix was flagged here (2026-05-27) while its
// chapter images came off a single-threaded canvas scrape that timed out
// mid-chapter; that warning was retired with comix's 2026-07-11 rewrite to
// plain directly-fetchable webp <img> pages plus its 2026-07-12 promotion to
// a first-class search / --multi-source / quality-probed source. The empty
// map keeps the render affordance (the icon just renders for nothing) ready
// for the next handler that breaks.
const BROKEN_HANDLERS = {};

export default function SearchSourceCard({
  source,           // SourceEntry from JSON (site, url, cover, scores, etc.)
  officialPublisher, // string | null — non-null if any chapter via this site is is_official=true
  multiSourceUsed,   // bool — adds --multi-source to the queued download by default
  fallbackCover,     // string | null — sibling candidate cover when this source's cover is blocked
  candidate,         // full SeriesCandidate this source belongs to — needed to
                     // bundle sibling sources into the prefetched-alts payload
                     // so aio-dl.py can skip its own cross-site search. Optional
                     // for back-compat (older callers that don't pass it just
                     // skip the prefetch path and trigger a re-search like before).
  onQueue,           // (url, opts) => void — pushes to dl.queueDownload
  index,             // for staggered slide-up animation delay
}) {
  const [imgErr, setImgErr] = useState(false);
  const [fallbackImgErr, setFallbackImgErr] = useState(false);

  useEffect(() => {
    setImgErr(false);
    setFallbackImgErr(false);
  }, [source.cover, fallbackCover]);

  // quality_basis: search_orchestrator._quality_basis (match_session.rate_sources
  // decides it; "seed" = not probed). img_quality_metadata:
  // match_session.quality_metadata (reason, samples_*, same_file/same_scan,
  // colored, …). Only a measured rating is a number; the rest show a reason
  // label (lib/qualityDisplay.js).
  const basis = source.quality_basis || "seed";
  const m = source.img_quality_metadata;
  const final = measuredRating(basis, source.img_quality_score);
  const tier = qualityTier(final);
  const status = qualityStatus(basis, m?.reason);
  const same = sameNote(m);
  const edition = editionNote(source.edition ?? m?.edition, m?.colored);
  const fetches =
    m?.samples_attempted
      ? ` · ${m.samples_succeeded ?? 0}/${m.samples_attempted} page fetches` +
        ((m.samples_timed_out ?? 0) > 0 ? ` (${m.samples_timed_out} timed out)` : "")
      : "";
  const barTitle =
    (final != null ? `rating ${final.toFixed(2)} · ${measuredTitle(basis)}` : status?.title ?? "") +
    fetches +
    (source.dmca_likely ? " · DMCA / publisher-hosted" : "");
  // Chapter count: the VERIFIED count when the site measured one, else its
  // metadata claim. MangaDex's verified count excludes publisher link-outs
  // (sites/mangadex.py, grep includeExternalUrl), so Goblin Slayer reads 0,
  // not the 203 rows it lists. Same rule as Device Sync's Find sources
  // (electron/sync/find-sources.js, grep actual_chapter_count) and Android
  // (SearchModels.kt chapterCount).
  const chapterCount = source.actual_chapter_count ?? source.chapter_count_hint ?? null;
  const chapterCountTitle =
    source.actual_chapter_count != null &&
    source.chapter_count_hint != null &&
    source.actual_chapter_count !== source.chapter_count_hint
      ? `${source.actual_chapter_count} downloadable · the site's info claims ${source.chapter_count_hint}`
      : undefined;
  const displayCover = source.cover && !imgErr
    ? source.cover
    : (fallbackCover && !fallbackImgErr ? fallbackCover : null);

  const handleDownload = () => {
    // Phase B / Fix B (2026-05-07): when we have the parent candidate's other
    // sources, bundle them into prefetchedAlts. SearchTab's options dialog
    // drops the payload unless multi-source is enabled for this download.
    // Keeping it available lets the user enable fallback from the popup
    // without forcing aio-dl.py to re-search the entire site list.
    const args = {
      // KEY MUST MATCH downloader.js:buildCliArgs's `multiSource` boolMap
      // entry (camelCase). Earlier draft used snake_case `multi_source`
      // which silently dropped the flag — search-driven multi-source
      // downloads behaved identically to single-source.
      multiSource: multiSourceUsed,
    };

    // The candidate's other sites, primary stripped, in the search's rank
    // order. Shared with Device Sync's Find sources (lib/prefetchedAlts.js).
    // downloader.js intercepts `prefetchedAlts`, writes it to a JSON file
    // under ~/.aio-dl/cache/, and replaces it with `multiSourcePrefetched`
    // (a path) which buildCliArgs's flagMap turns into the CLI arg.
    const alts = buildPrefetchedAlts(candidate?.sources, source, candidate?.canonical_title, candidate?.canonical_year);
    if (alts) args.prefetchedAlts = alts;

    onQueue?.(source.url, args, { source, candidate });
  };

  return (
    <div
      className={cn(
        "group relative w-[160px] flex-shrink-0 rounded-md border bg-card",
        "overflow-hidden transition-all duration-150 hover:border-primary/40 hover:shadow-md",
        "animate-slide-up",
      )}
      style={{ animationDelay: `${Math.min(index * 30, 300)}ms` }}
    >
      {/* Cover — 3:4 aspect ratio matches typical manga cover proportions */}
      <div className="relative aspect-[3/4] bg-muted overflow-hidden">
        {displayCover ? (
          <img
            src={displayCover}
            alt={source.title || source.site}
            loading="lazy"
            onError={() => {
              if (source.cover && displayCover === source.cover) {
                setImgErr(true);
              } else {
                setFallbackImgErr(true);
              }
            }}
            className="w-full h-full object-cover"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-muted-foreground/40">
            <ImageIcon className="w-10 h-10" />
          </div>
        )}

        {/* DMCA overlay — top-right corner, tucked over cover */}
        {source.dmca_likely && (
          <div
            className="absolute top-1.5 right-1.5"
            title="Most chapters this site lists can't be downloaded from it: taken down (DMCA) or hosted by the publisher (a link out, no pages)."
          >
            <Badge variant="destructive" className="text-[9px] px-1.5 py-0 leading-tight">
              DMCA
            </Badge>
          </div>
        )}

        {/* Official-publisher badge — top-left when this site has any
            licensed chapters per winner_chapter_map. Gold-tinted via
            warning variant since success is reserved for "good quality". */}
        {officialPublisher && (
          <div className="absolute top-1.5 left-1.5">
            <Badge variant="warning" className="text-[9px] px-1.5 py-0 leading-tight">
              ★ {officialPublisher}
            </Badge>
          </div>
        )}

        {/* Other-edition badge — bottom-left. The search's color check
            (sites/match_session.py _editions) found this site's pages are a
            different edition from most sites' (e.g. fan-colored). Such a
            source never becomes another download's fallback
            (lib/prefetchedAlts.js editionOf); the tooltip says which way it
            differs. */}
        {edition && (
          <div className="absolute bottom-1.5 left-1.5" title={edition.title}>
            <Badge variant="default" className="text-[9px] px-1.5 py-0 leading-tight bg-background/90">
              {edition.label}
            </Badge>
          </div>
        )}
      </div>

      {/* Body */}
      <div className="px-2.5 py-2 space-y-1.5">
        {/* Site name + broken-handler warning + chapter count */}
        <div className="flex items-center gap-1 min-w-0">
          <span className="font-mono text-[11px] font-medium text-foreground truncate">
            {source.site}
          </span>
          {/* Broken-handler danger icon. Sourced from BROKEN_HANDLERS map at
              the top of this file — keyed by source.site, value is the
              tooltip text. Inline span carries the title because lucide-react's
              SVGs don't surface native tooltips reliably across browsers.
              Cross-file: BROKEN_HANDLERS comment for current entries and
              the why; remove an entry when the handler is fixed. */}
          {BROKEN_HANDLERS[source.site] && (
            <span
              className="shrink-0 inline-flex items-center"
              title={BROKEN_HANDLERS[source.site]}
            >
              <AlertTriangle
                className="w-3 h-3 text-red-500"
                aria-label={`${source.site} is currently broken: ${BROKEN_HANDLERS[source.site]}`}
              />
            </span>
          )}
          {chapterCount != null && (
            <span
              className="text-[10px] text-muted-foreground tabular-nums shrink-0 ml-auto"
              title={chapterCountTitle}
            >
              {chapterCount}ch
            </span>
          )}
        </div>

        {/* Quality bar — width = the measured rating, color by tier; empty
            when there is no measured rating. Tooltip: what the rating means
            for this basis (or why there is none), plus page-fetch counts. */}
        <div className="h-1 rounded-full bg-muted overflow-hidden" title={barTitle}>
          <div
            className={cn("h-full transition-all duration-300", tier.color)}
            style={{ width: `${Math.max(0, Math.min(1, final || 0)) * 100}%` }}
          />
        </div>

        {/* Rating number, or the reason label when there is none (user
            decision 2026-10-21: measured ratings are the only numbers). The
            Clock flags page fetches that timed out: the rating is FAIR but
            rests on fewer pages; older JSON lacks the field → no icon. Title
            on a span because lucide SVGs don't surface native tooltips
            reliably (see the site-name icons above). */}
        <div className="flex items-center justify-end gap-1" title={barTitle}>
          {(m?.samples_timed_out ?? 0) > 0 && (
            <span
              className="shrink-0 inline-flex items-center"
              title={`${m.samples_timed_out} of ${m.samples_attempted} page fetches timed out (the site is slow).`}
            >
              <Clock
                className="w-3 h-3 text-muted-foreground"
                aria-label={`${m.samples_timed_out} of ${m.samples_attempted} page fetches timed out`}
              />
            </span>
          )}
          <span className="font-mono text-[10px] text-muted-foreground tabular-nums">
            {final != null ? final.toFixed(2) : status?.label ?? "—"}
          </span>
        </div>

        {/* Same file / same scan note (match_session rate_sources
            same_file / same_scan): which other sites serve this copy. */}
        {same && (
          <div className="text-[10px] leading-tight text-muted-foreground truncate" title={same.title}>
            {same.label}
          </div>
        )}

        {/* Download button */}
        <Button
          size="sm"
          variant="default"
          className="w-full h-7 text-[11px] gap-1"
          onClick={handleDownload}
        >
          <Download className="w-3 h-3" />
          Download
        </Button>
      </div>
    </div>
  );
}
