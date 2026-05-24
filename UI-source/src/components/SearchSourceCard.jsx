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
//
// Tooltip schema (img_quality_metadata):
//   v8 (2026-05-18) B&W rewrite — bw_screentone_score,
//     bw_line_quality, bw_bg_uniformity, bw_upscaler_score,
//     bw_bilevel, bw_chroma_subsampled. clip_iqa_score replaces
//     pyiqa CLIP path. pairwise_adjustment / pairwise_winrate /
//     pairwise_total_comparisons / pairwise_bucket replace
//     paired_quality_adjustment / paired_anchor_site /
//     paired_perceptual_median / paired_dists_median / etc.
//   v7 and earlier paired_* / fft_hf_ratio fields still rendered
//     as fallbacks when reading pre-v8 cache entries.
// ============================================================

import React, { useState } from "react";
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

export default function SearchSourceCard({
  source,           // SourceEntry from JSON (site, url, cover, scores, etc.)
  officialPublisher, // string | null — non-null if any chapter via this site is is_official=true
  multiSourceUsed,   // bool — adds --multi-source to the queued download by default
  candidate,         // full SeriesCandidate this source belongs to — needed to
                     // bundle sibling sources into the prefetched-alts payload
                     // so aio-dl.py can skip its own cross-site search. Optional
                     // for back-compat (older callers that don't pass it just
                     // skip the prefetch path and trigger a re-search like before).
  onQueue,           // (url, opts) => void — pushes to dl.queueDownload
  index,             // for staggered slide-up animation delay
}) {
  const [imgErr, setImgErr] = useState(false);

  const final = source.img_quality_score != null ? source.img_quality_score : source.seed_quality;
  const tier = qualityTier(final);
  // Phase H aggregate metadata from sites/base.py:_probe_chapter_aggregate.
  // null when un-measured (cache miss + probe failed). Drives the format chip
  // beside the site name, the bpp/B&W/outlier breakdown in the quality-bar
  // tooltip, and the ⚠ icon next to the score when an outlier flag is set.
  const m = source.img_quality_metadata;
  const formatLabel = m?.format ? m.format.toLowerCase() : null;

  const handleDownload = () => {
    // Phase B / Fix B (2026-05-07): when multi-source is on AND we have the
    // parent candidate's other sources, bundle them into prefetchedAlts so
    // aio-dl.py's `--multi-source-prefetched <path>` path skips its own
    // 291-site cross-site search. Without this the user clicks Download from
    // a search result and aio-dl.py re-searches the entire site list to
    // discover the SAME alts the user just looked at.
    const args = {
      // KEY MUST MATCH downloader.js:buildCliArgs's `multiSource` boolMap
      // entry (camelCase). Earlier draft used snake_case `multi_source`
      // which silently dropped the flag — search-driven multi-source
      // downloads behaved identically to single-source.
      multiSource: multiSourceUsed,
    };

    if (multiSourceUsed && candidate?.sources?.length > 1) {
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

    onQueue?.(source.url, args);
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
        {source.cover && !imgErr ? (
          <img
            src={source.cover}
            alt={source.title || source.site}
            loading="lazy"
            onError={() => setImgErr(true)}
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
        {/* Site name + format chip + chapter count */}
        <div className="flex items-center gap-1 min-w-0">
          <span className="font-mono text-[11px] font-medium text-foreground truncate">
            {source.site}
          </span>
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
            plus v6 per-source metadata (T1 components incl. v5.1 content
            type, T2 deep-model scores, T3 paired-ensemble + ghost +
            alignment, watermark, USM, chroma) so the user can audit why
            a source ranked where it did without leaving the card.
            Multi-line via "\n" — browsers generally render \n as line
            breaks in title attributes.

            v6 schema fields come from sites/search_orchestrator.py:
            _compute_t1_score + _detect_watermarks + _compute_usm_overshoot
            + _compute_chroma_penalty + _compute_t2_score + _compute_jpeg_ghost
            + _align_image_pair_v51 + _run_paired_comparison. All fields
            may be null (unmeasured / disabled / not applicable) — the
            conditional chains below handle nulls gracefully. */}
        <div
          className="h-1 rounded-full bg-muted overflow-hidden"
          title={(() => {
            // Defensive IIFE: a single bad metadata field (e.g., a cached
            // m.t1_score that round-tripped as a string "0.42" from an
            // older schema) makes `.toFixed()` throw a TypeError, which
            // would tear down the entire card render. The `!= null` chains
            // below check existence, not type — so they don't catch type
            // drift. Wrapping in try/catch keeps the rest of the card
            // alive on schema-drift edge cases that survived
            // ImageQualityCache.SCHEMA_VERSION invalidation. The catch
            // logs once and falls back to a minimal site-level tooltip
            // so the user can still see WHAT this card is for.
            try {
              return (
            // Headline: seed · measured · final · format · content_type · etc.
            `seed=${(source.seed_quality ?? 0).toFixed(2)}` +
            (source.img_quality_score != null
              ? ` · measured=${source.img_quality_score.toFixed(2)}`
              : " · unmeasured") +
            ` · final=${(final ?? 0).toFixed(2)}` +
            (m?.format ? ` · ${m.format}` : "") +
            // v5.1 Phase 4 content-type chip surfacing.
            (m?.content_type && m.content_type !== "unknown"
              ? ` · ${m.content_type}`
              : "") +
            (m?.bpp != null ? ` · ${m.bpp.toFixed(3)} bpp` : "") +
            (m?.is_grayscale != null ? (m.is_grayscale ? " · B&W" : " · color") : "") +
            (m?.samples_attempted != null
              ? ` · ${m.samples_succeeded ?? 0}/${m.samples_attempted} chapters`
              : "") +
            (m?.cdn_reliability != null
              ? ` · cdn=${m.cdn_reliability.toFixed(2)}`
              : "") +
            (m?.outlier ? ` · ⚠ ${m.outlier}` : "") +
            (source.dmca_likely ? " · DMCA-flagged" : "") +
            // T1 breakdown line — extended in v6/v8 with B&W-specific
            // signals (screentone, line, bg, upscaler, bilevel, chroma)
            // when the source's content_type is bw_manga / bw_manga_with_color_inserts.
            // Legacy (color/webtoon/unknown) sources fall back to fft_hf
            // / tenengrad. Pre-v8 cache entries still show old fields.
            ((m?.t1_score != null || m?.jpeg_qf != null || m?.tenengrad_norm != null)
              ? `\nT1: ` +
                (m.t1_score != null ? `t1=${m.t1_score.toFixed(2)}` : "") +
                (m.res_norm != null ? ` res=${m.res_norm.toFixed(2)}` : "") +
                (m.jpeg_qf != null ? ` qf=${m.jpeg_qf.toFixed(0)}` : "") +
                (m.blockiness != null ? ` block=${m.blockiness.toFixed(2)}` : "") +
                // v8: B&W-specific signals (only present when
                // content_type ∈ bw_manga / bw_manga_with_color_inserts).
                (m.bw_screentone_score != null
                  ? ` screen=${m.bw_screentone_score.toFixed(2)}`
                  : (m.fft_hf_ratio != null ? ` fft=${m.fft_hf_ratio.toFixed(2)}` : "")) +
                (m.bw_line_quality != null
                  ? ` line=${m.bw_line_quality.toFixed(2)}`
                  : "") +
                (m.bw_bg_uniformity != null
                  ? ` bg=${m.bw_bg_uniformity.toFixed(2)}`
                  : "") +
                // v5.1: prefer tenengrad_clean (USM-damped) when available,
                // fall back to raw tenengrad_norm for pre-v6 metadata.
                (m.tenengrad_clean != null
                  ? ` tene=${m.tenengrad_clean.toFixed(2)}`
                  : (m.tenengrad_norm != null ? ` tene=${m.tenengrad_norm.toFixed(2)}` : "")) +
                (m.watermark_score != null && m.watermark_score > 0
                  ? ` wm=${m.watermark_score.toFixed(2)}(${(m.watermark_regions || []).length})`
                  : "") +
                (m.usm_overshoot_score != null && m.usm_overshoot_score > 0
                  ? ` usm=${m.usm_overshoot_score.toFixed(2)}`
                  : "") +
                // v8: AI-upscaler fingerprint (B&W-specific). > 0.7 →
                // outlier=ai_upscale_suspected and a separate ⚠ chip below.
                (m.bw_upscaler_score != null && m.bw_upscaler_score > 0.1
                  ? ` upscale=${m.bw_upscaler_score.toFixed(2)}${m.bw_upscaler_score > 0.7 ? "⚠" : ""}`
                  : "") +
                (m.bw_bilevel ? " bilevel⚠" : "") +
                (m.chroma_subsampling
                  ? ` chroma=${m.chroma_subsampling}${m.chroma_penalty > 0 ? "⚠" : ""}`
                  : (m.bw_chroma_subsampled
                      ? ` chroma=${m.bw_chroma_subsampled}${m.bw_chroma_penalty > 0 ? "⚠" : ""}`
                      : "")) +
                (m.bw_lossless_bonus != null && m.bw_lossless_bonus > 0
                  ? ` +lossless`
                  : "")
              : "") +
            // T2 line — only when t2_available so users see when deep models ran.
            // v8 adds clip_iqa_score from torchmetrics CLIP-IQA+ (manga-
            // tuned prompts). For B&W content NIQE is skipped so clip
            // is the only signal; for color content both run.
            (m?.t2_available
              ? `\nT2: ` +
                (m.t2_score != null ? `t2=${m.t2_score.toFixed(2)}` : "") +
                (m.niqe_score != null ? ` niqe=${m.niqe_score.toFixed(1)}` : "") +
                (m.clip_iqa_score != null ? ` clip=${m.clip_iqa_score.toFixed(2)}` : "") +
                (m.arniqa_score != null ? ` arniqa=${m.arniqa_score.toFixed(2)}` : "")
              : (m?.t2_available === false ? `\nT2: unavailable (T1-only)` : "")) +
            // T3 pairwise line (v8). Anchor-free win-rate aggregation
            // replaces v5.1's DISTS-vs-anchor scheme. Shows the
            // adjustment + winrate + how many component comparisons
            // ran. Pre-v8 cache entries fall back to the legacy
            // paired_* fields below.
            (m?.pairwise_adjustment != null
              ? `\nT3 pairwise: adj=${m.pairwise_adjustment >= 0 ? "+" : ""}${m.pairwise_adjustment.toFixed(2)}` +
                (m.pairwise_winrate != null
                  ? ` winrate=${(m.pairwise_winrate * 100).toFixed(0)}%`
                  : "") +
                (m.pairwise_total_comparisons != null
                  ? ` (${m.pairwise_total_comparisons} comparisons)`
                  : "") +
                (m.pairwise_ghost_score != null
                  ? ` ghost=${m.pairwise_ghost_score.toFixed(2)}`
                  : "") +
                (m.pairwise_bucket
                  ? ` bucket=${m.pairwise_bucket}`
                  : "")
              : // Legacy v5.1 paired_* fields (pre-v8 cache entries).
                m?.paired_quality_adjustment != null
              ? `\nT3 paired (legacy): adj=${m.paired_quality_adjustment >= 0 ? "+" : ""}${m.paired_quality_adjustment.toFixed(2)}` +
                (m.paired_anchor_site
                  ? ` (anchor=${m.paired_anchor_site === source.site ? "self" : m.paired_anchor_site}` +
                    (m.paired_perceptual_median != null
                      ? `, perceptual=${m.paired_perceptual_median.toFixed(3)}`
                      : (m.paired_dists_median != null ? `, DISTS=${m.paired_dists_median.toFixed(3)}` : "")) +
                    (m.paired_pairs_compared != null ? `, ${m.paired_pairs_compared} pair${m.paired_pairs_compared === 1 ? "" : "s"}` : "") +
                    `)`
                  : "")
              : "")
              );
            } catch (err) {
              // One-shot console signal so devs can diagnose; we don't
              // spam every render. The card itself stays alive.
              if (!source.__tooltipErrorLogged) {
                // eslint-disable-next-line no-console
                console.warn(
                  `[SearchSourceCard] tooltip render failed for site=${source.site}; ` +
                  `metadata may be schema-mismatched. Error: ${err && err.message}`,
                );
                source.__tooltipErrorLogged = true;
              }
              return `${source.site || "source"} — metadata unavailable`;
            }
          })()}
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
