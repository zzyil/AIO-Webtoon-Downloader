// ============================================================
// SEARCH SOURCE CARD
//
// Single source within a candidate (one row in the JSON's
// candidates[i].sources[]). Renders cover, site label, quality
// bar, badges (DMCA / official-publisher / chapter-count), and
// a Download button that pushes to the existing queue.
//
// Compact card sized for a horizontal scroll-snap row of 4-8
// sources per candidate. ~160px wide.
// ============================================================

import React, { useEffect, useState } from "react";
import { Button, Badge } from "@/components/ui/primitives";
import { Download, Image as ImageIcon, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";

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
// Entries:
//   - comix (added 2026-05-27): the canvas-scrape fallback for
//     unscrambled chapter images can't render all pages within the 300s
//     per-chapter time budget. Chapter downloads time out at single-
//     digit page counts (e.g. 3/193 pages in the 2026-05-27 Shangri-La
//     Frontier run). The Patchright bridge is single-threaded so
//     increasing parallelism doesn't help. See sites/comix.py for the
//     in-progress work and the PR-31 context doc's "fix comix" pointer.
const BROKEN_HANDLERS = {
  comix: (
    "Currently broken: canvas-scrape can't capture all pages within the "
    + "per-chapter time budget. Downloads typically time out at a small "
    + "number of pages. You can still try, but expect failure."
  ),
};

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

  const final = source.img_quality_score != null ? source.img_quality_score : source.seed_quality;
  const tier = qualityTier(final);
  // Phase H aggregate metadata from sites/base.py:_probe_chapter_aggregate.
  // null when un-measured (cache miss + probe failed). Drives the format chip
  // beside the site name, the bpp/B&W/outlier breakdown in the quality-bar
  // tooltip, and the ⚠ icon next to the score when an outlier flag is set.
  const m = source.img_quality_metadata;
  const formatLabel = m?.format ? m.format.toLowerCase() : null;
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

    if (candidate?.sources?.length > 1) {
      // Strip the primary itself; keep everything else as alternatives. The
      // ordering preserves the search-time quality ranking (candidate.sources
      // is sorted official-first / DMCA-last / quality-desc by the orchestrator),
      // which becomes the alignment input order in chapter_merger.
      const alternatives = candidate.sources
        .filter((s) => s.url !== source.url)
        .map((s) => ({
          site: s.site,
          url: s.url,
          title: s.title || undefined,
          cover: s.cover || undefined,
        }));
      if (alternatives.length > 0) {
        // downloader.js intercepts `prefetchedAlts`, writes it to a JSON file
        // under ~/.aio-dl/cache/, and replaces it with `multiSourcePrefetched`
        // (a path) which buildCliArgs's flagMap turns into the CLI arg.
        args.prefetchedAlts = {
          primary: { site: source.site, url: source.url },
          alternatives,
          title: candidate.canonical_title || "",
          year: candidate.canonical_year || null,
        };
      }
    }

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
          <div className="absolute top-1.5 right-1.5">
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
      </div>

      {/* Body */}
      <div className="px-2.5 py-2 space-y-1.5">
        {/* Site name + broken-handler warning + format chip + chapter count */}
        <div className="flex items-center gap-1 min-w-0">
          <span className="font-mono text-[11px] font-medium text-foreground truncate">
            {source.site}
          </span>
          {/* Broken-handler danger icon. Sourced from BROKEN_HANDLERS map at
              the top of this file — keyed by source.site, value is the
              tooltip text. Red AlertTriangle distinguishes "handler is
              broken" (this) from the yellow outlier-flag AlertTriangle on
              the score row below (which is "this source's image quality is
              suspect"). Inline span carries the title because lucide-react's
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
          {formatLabel && (
            // Format chip from Phase H metadata. Lowercase font-mono lines up
            // with the site name visually; muted color keeps it secondary.
            <span className="font-mono text-[9px] text-muted-foreground/70 uppercase tracking-wide shrink-0">
              {formatLabel}
            </span>
          )}
          {source.chapter_count_hint != null && (
            <span className="text-[10px] text-muted-foreground tabular-nums shrink-0 ml-auto">
              {source.chapter_count_hint}ch
            </span>
          )}
        </div>

        {/* Quality bar — width proportional to final score, color by tier.
            Tooltip on hover surfaces the seed / measured / final breakdown
            plus Phase H per-source metadata (format, bpp, content type,
            outlier flag) so the user can audit why a source ranked where
            it did without leaving the card. */}
        <div
          className="h-1 rounded-full bg-muted overflow-hidden"
          title={
            `seed=${(source.seed_quality ?? 0).toFixed(2)}` +
            (source.img_quality_score != null
              ? ` · measured=${source.img_quality_score.toFixed(2)}`
              : " · unmeasured") +
            ` · final=${(final ?? 0).toFixed(2)}` +
            (m?.format ? ` · ${m.format}` : "") +
            (m?.bpp != null ? ` · ${m.bpp.toFixed(3)} bpp` : "") +
            (m?.is_grayscale != null ? (m.is_grayscale ? " · B&W" : " · color") : "") +
            (m?.decode_quality != null ? ` · decode=${m.decode_quality.toFixed(2)}` : "") +
            (m?.outlier ? ` · outlier=${m.outlier}` : "") +
            (m?.samples_attempted != null
              ? ` · ${m.samples_succeeded ?? 0}/${m.samples_attempted} samples`
              : "") +
            (source.dmca_likely ? " · DMCA-flagged" : "")
          }
        >
          <div
            className={cn("h-full transition-all duration-300", tier.color)}
            style={{ width: `${Math.max(0, Math.min(1, final || 0)) * 100}%` }}
          />
        </div>

        {/* Outlier ⚠ + final score number — right-aligned. The icon flags a
            broken-encoding source (currently only "webp_below_floor") so the
            user can spot a degenerate score at a glance without hovering for
            the tooltip. yellow-500 is the existing warning palette. */}
        <div className="flex items-center justify-end gap-1">
          {m?.outlier && (
            <AlertTriangle
              className="w-3 h-3 text-yellow-500 shrink-0"
              aria-label={`outlier flag: ${m.outlier}`}
            />
          )}
          <span className="font-mono text-[10px] text-muted-foreground tabular-nums">
            {final != null ? final.toFixed(2) : "—"}
          </span>
        </div>

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
