import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Button, Input, Badge } from "@/components/ui/primitives";
import {
  Search,
  RefreshCw,
  BookOpen,
  FolderOpen,
  Trash2,
  FileText,
  ArrowLeft,
  ArrowUpDown,
  Download,
  ExternalLink,
  Globe,
  Bell,
  Check,
  Loader2,
  AlertCircle,
  Link,
  User,
  Tag,
  PencilLine,
  Save,
  X,
  Filter,
  FilterX,
  Sparkles,
  AlertTriangle,
  Merge,
} from "lucide-react";
import { cn, chaptersToRangeString, getInitials, naturalCompare } from "@/lib/utils";
import { libraryRecipe } from "@/lib/queueRecipe";
import { KOMIKKU_VOLUMES_MESSAGE } from "@/lib/downloadArgs";
import { useSiteCapabilities, volumesSupportedForSite } from "@/lib/siteCapabilities";
import UpdatesCenter from "./UpdatesCenter";
import ChapterChips, { ChapterChipsLegend, selectedChapters } from "./ChapterChips";
import LibraryFilterPanel, {
  FACET_GROUPS,
  FACET_GROUP_BY_KEY,
} from "./LibraryFilterPanel";
// Device Sync's Library surfaces (UI pass U4, design D9). Only drawn while
// sync is on: App passes syncLibrary = null otherwise.
import { SyncChip, SyncLine } from "./sync/LibrarySync";
import { seriesSyncChip, seriesSyncLines } from "./sync/libraryView";

// NOTE on the funnel glyph: this repo pins lucide-react 0.263.1, where the
// funnel icon is still named `Filter` (upstream renamed it to `Funnel` much
// later). `Funnel` is NOT exported here — importing it renders undefined and
// crashes React.

// Convert a Windows file path to a localfile:// URL the renderer can load.
function fileToUrl(filePath) {
  if (!filePath) return null;
  const normalized = filePath.replace(/\\/g, "/");
  return "localfile:///" + encodeURI(normalized);
}

// ── CONFIGURABLE ──
const FORMAT_COLORS = {
  pdf: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  epub: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  cbz: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  // --format none (image-only) series. Badge text is "images" (see
  // getEntryFormats); the per-chapter rows in the detail view reuse this too.
  images: "bg-violet-500/20 text-violet-400 border-violet-500/30",
};

const STATUS_COLORS = {
  Ongoing: "bg-blue-500/20 text-blue-400 border-blue-500/40",
  Releasing: "bg-blue-500/20 text-blue-400 border-blue-500/40",
  Completed: "bg-emerald-500/20 text-emerald-400 border-emerald-500/40",
  Finished: "bg-emerald-500/20 text-emerald-400 border-emerald-500/40",
};

const SORT_OPTIONS = [
  { value: "title", label: "Title A→Z" },
  { value: "title-desc", label: "Title Z→A" },
  { value: "date", label: "Newest first" },
  { value: "date-asc", label: "Oldest first" },
  { value: "size", label: "Largest first" },
  { value: "size-asc", label: "Smallest first" },
];

// ── Facet filtering ──
// The four groups map 1:1 onto .aio_series.json fields, which electron/
// library.js parses whole and hands over untouched as entry.seriesMeta — so no
// backend change was needed to filter on any of this:
//   genres → seriesMeta.genres              string[]
//   tags   → seriesMeta.anilist_tags + .anilist_spoiler_tags
//            ARRAYS OF OBJECTS {name, category, rank, is_media_spoiler,
//            is_general_spoiler} — writer aio-dl.py:_serialize_anilist_tag,
//            grep anilist_tags. A naive .join() renders [object Object], and
//            the keys are absent on anything downloaded before AniList
//            enrichment shipped, so every read is guarded.
//   status → seriesMeta.status              STATUS_COLORS vocabulary
//   sites  → seriesMeta.site                python handler name ("mangafire")
// Keys are lowercased (sites disagree on genre casing); the display label is
// the most common casing observed across the library.
//
// Group order/icons/accents + the singleValued flag live in
// LibraryFilterPanel.jsx:FACET_GROUPS.
const FACET_KEYS = ["genres", "tags", "status", "sites"];

function makeEmptyFilters() {
  return {
    genres: new Set(),
    tags: new Set(),
    status: new Set(),
    sites: new Set(),
    matchMode: "any",
    showSpoilerTags: false,
  };
}

// settings.libraryOpts.filters ⇄ filter state. Sets serialize as arrays.
// Every field is re-validated on read: history.json is hand-editable and an
// older settings dict simply has no `filters` key at all.
function filtersFromJson(raw) {
  const out = makeEmptyFilters();
  if (!raw || typeof raw !== "object") return out;
  for (const k of FACET_KEYS) {
    if (Array.isArray(raw[k])) {
      out[k] = new Set(raw[k].filter((v) => typeof v === "string"));
    }
  }
  if (raw.matchMode === "all" || raw.matchMode === "any") out.matchMode = raw.matchMode;
  out.showSpoilerTags = raw.showSpoilerTags === true;
  return out;
}

function filtersToJson(f) {
  return {
    genres: [...f.genres],
    tags: [...f.tags],
    status: [...f.status],
    sites: [...f.sites],
    matchMode: f.matchMode,
    showSpoilerTags: f.showSpoilerTags,
  };
}

function countActiveFilters(f) {
  return f.genres.size + f.tags.size + f.status.size + f.sites.size;
}

// AND across groups; within a group `matchMode` decides.
// EXCEPT for the single-valued groups (status, source): a series has exactly
// one of each, so "all" with two selected could never match anything. Those
// are always OR — matchMode is effectively a genres/tags control, which is the
// standard faceted-search contract.
function matchesFacets(entryFacets, filters) {
  if (!entryFacets) return false;
  for (const groupKey of FACET_KEYS) {
    const selected = filters[groupKey];
    if (selected.size === 0) continue;
    const have = entryFacets[groupKey];
    if (filters.matchMode === "all" && !FACET_GROUP_BY_KEY[groupKey].singleValued) {
      for (const v of selected) if (!have.has(v)) return false;
    } else {
      let hit = false;
      for (const v of selected) {
        if (have.has(v)) { hit = true; break; }
      }
      if (!hit) return false;
    }
  }
  return true;
}

// Normalize a raw genre/tag/status/site string into its facet key. MUST stay
// identical to the keying in the facetIndex memo below — DetailView's chips
// derive keys from raw metadata strings and would otherwise apply a filter
// that matches nothing.
function facetKey(raw) {
  return String(raw ?? "").trim().toLowerCase();
}

// ── Badge components ──
// Collapse the repeated FORMAT_COLORS / STATUS_COLORS <span> markup (grid card,
// detail header, per-chapter/file rows). Each keeps the color-map lookup +
// fallback in one place; the caller passes size/position/padding via className.
// The color class goes LAST (after className) to match the original ordering
// where the color set followed the sizing set. `fallback` is a prop because
// the two status call sites use different muted fallbacks (bg-muted/80 vs
// bg-muted); `label` lets the image-chapter row render "IMG" while still
// keying the color on fmt="images".
function FormatBadge({ fmt, label, className }) {
  return (
    <span
      className={cn(
        "font-bold uppercase rounded border",
        className,
        FORMAT_COLORS[fmt] || "bg-muted text-muted-foreground border-border"
      )}
    >
      {label ?? fmt}
    </span>
  );
}

function StatusBadge({ status, className, fallback = "bg-muted text-muted-foreground border-border" }) {
  return (
    <span
      className={cn(
        "font-bold uppercase rounded border",
        className,
        STATUS_COLORS[status] || fallback
      )}
    >
      {status}
    </span>
  );
}

// Clickable metadata chip for DetailView's genre + AniList-tag rows. Clicking
// applies the value as a library filter and drops back to the grid, so there's
// deliberately no "selected" state — this chip never renders as on.
// Hand-rolled rather than reusing Badge because Badge (ui/primitives.jsx) does
// NOT spread props and therefore can't take an onClick.
function MetaChip({ label, onClick, title }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title || `Filter library by “${label}”`}
      className={cn(
        "inline-flex items-center rounded-full border border-border/70 bg-secondary/50",
        "px-2 py-0.5 text-[10px] leading-tight text-muted-foreground",
        "hover:border-primary/40 hover:bg-primary/10 hover:text-primary transition-colors"
      )}
    >
      {label}
    </button>
  );
}

// AniList tags in the detail view — the first place in the UI that has ever
// rendered them. Entries are OBJECTS (aio-dl.py:_serialize_anilist_tag), and
// both keys are absent on pre-enrichment downloads, hence the normalize pass.
// Spoiler-flagged tags live in their own array and stay behind a click: the
// is_media_spoiler / is_general_spoiler flags exist precisely so a reader can
// be spoiler-aware.
function AnilistTagChips({ meta, onFilterByFacet }) {
  const [showSpoilers, setShowSpoilers] = useState(false);
  const normalize = (list) =>
    (Array.isArray(list) ? list : []).filter(
      (t) => t && typeof t === "object" && t.name
    );
  const tags = normalize(meta?.anilist_tags);
  const spoilerTags = normalize(meta?.anilist_spoiler_tags);
  if (tags.length === 0 && spoilerTags.length === 0) return null;

  const chip = (t) => (
    <MetaChip
      key={t.name}
      label={t.name}
      title={[t.name, t.category, t.rank ? `rank ${t.rank}` : null]
        .filter(Boolean)
        .join(" · ")}
      onClick={() => onFilterByFacet?.("tags", facetKey(t.name))}
    />
  );

  return (
    <div className="flex items-start gap-1.5">
      <Sparkles className="w-3 h-3 shrink-0 mt-1 text-violet-400" />
      <div className="flex flex-wrap items-center gap-1">
        {tags.map(chip)}
        {spoilerTags.length > 0 &&
          (showSpoilers ? (
            spoilerTags.map(chip)
          ) : (
            <button
              type="button"
              onClick={() => setShowSpoilers(true)}
              className="text-[10px] text-muted-foreground hover:text-foreground underline decoration-dotted underline-offset-2 transition-colors"
            >
              {spoilerTags.length} spoiler tag{spoilerTags.length === 1 ? "" : "s"}
            </button>
          ))}
      </div>
    </div>
  );
}

// ============================================================
// HELPERS
// ============================================================
function formatSize(bytes) {
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
  return (bytes / 1073741824).toFixed(2) + " GB";
}

function formatDate(isoString) {
  if (!isoString) return "";
  const d = new Date(isoString);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

// Format badges for a series. Archive series derive them from their file
// extensions; an image-only (--format none) series has no archive files, so
// it gets a single synthetic "images" badge. Returns [] only for the
// degenerate case of no files and not image-only (shouldn't happen — the
// scanner's payload-required gate filters those out).
function getEntryFormats(entry) {
  const fileFormats = [...new Set((entry.files || []).map((f) => f.ext))];
  if (fileFormats.length > 0) return fileFormats;
  if (entry.isImageOnly) return ["images"];
  return [];
}

// chaptersToRangeString ("51","52","53" → "51-53") now lives in
// @/lib/utils (shared with UpdatesCenter). Imported above.

// ============================================================
// PDF COVER THUMBNAIL
// ============================================================
function PdfCover({ entry }) {
  if (entry.thumbPath) {
    return (
      <img
        src={fileToUrl(entry.thumbPath)}
        alt={entry.title}
        className="w-full h-full object-cover rounded"
        loading="lazy"
      />
    );
  }
  // Image-only (--format none) fallback: no PDF to render and maybe no cached
  // web cover yet, so show the first downloaded page straight from disk.
  // Chromium decodes webp/avif/png/jpg/gif natively; object-cover crops the
  // long-strip aspect. If a web cover later downloads, thumbPath wins on the
  // next render (it's checked first). coverImagePath comes from library.js.
  if (entry.coverImagePath) {
    return (
      <img
        src={fileToUrl(entry.coverImagePath)}
        alt={entry.title}
        className="w-full h-full object-cover rounded"
        loading="lazy"
      />
    );
  }
  if (entry.coverPdfPath) {
    return <div className="w-full h-full bg-muted animate-pulse rounded" />;
  }
  const initials = getInitials(entry.title);
  return (
    <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-primary/20 to-primary/5 rounded">
      <span className="text-2xl font-bold text-primary/60">{initials}</span>
    </div>
  );
}

// ============================================================
// MANGA CARD (grid item)
// ============================================================
// ── Duplicate warning badge (grid card overlay) ──
// One series, two folders. A site RENAMING a series used to fork a second
// folder and split its chapters, which made the update check under-report on
// both halves. aio-dl.py's allocator no longer forks, so this badge exists for
// the forks already on disk — and for the weaker "same AniList id from a
// different source" case, which is a hint the user resolves by hand.
//
// Sits bottom-left: top-left is the status badge, top-right the "N new" badge.
// stopPropagation because the card itself opens the detail view and this must
// open the merge dialog instead.
// Cross-file: electron/library.js:findDuplicateSeries populates entry.duplicate.
function DuplicateBadge({ duplicate, onClick }) {
  const exact = duplicate.reason === "same-series";
  const peerNames = duplicate.peers.map((p) => p.title).join(", ");
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title={
        exact
          ? `Split across ${duplicate.peers.length + 1} folders (also: ${peerNames}). Click to merge.`
          : `Possibly the same series as ${peerNames}. Click to review.`
      }
      className={cn(
        "absolute bottom-1.5 left-1.5 flex items-center gap-1 rounded px-1.5 py-0.5",
        "text-[9px] font-bold shadow-sm backdrop-blur-sm transition-colors",
        exact
          ? "bg-amber-500/90 text-white hover:bg-amber-500"
          : "bg-amber-500/60 text-white hover:bg-amber-500/80"
      )}
    >
      <AlertTriangle className="w-2.5 h-2.5" />
      {exact ? "split" : "dupe?"}
    </button>
  );
}

function MangaCard({ entry, newCount, onClick, onShowDuplicate, syncChip }) {
  const formats = getEntryFormats(entry);
  const status = entry.seriesMeta?.status;

  return (
    <button
      onClick={onClick}
      className={cn(
        "group flex flex-col rounded-lg overflow-hidden text-left",
        "bg-card/60 border border-border/50",
        "hover:border-primary/40 hover:bg-card/80",
        "transition-all duration-150 cursor-pointer",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      )}
    >
      {/* Cover with overlay badges */}
      <div className="aspect-[3/4] w-full overflow-hidden bg-muted/30 relative">
        <PdfCover entry={entry} />

        {/* Status badge (top-left) */}
        {status && (
          <StatusBadge
            status={status}
            className="absolute top-1.5 left-1.5 text-[8px] px-1.5 py-0.5 backdrop-blur-sm"
            fallback="bg-muted/80 text-muted-foreground border-border"
          />
        )}

        {/* New chapters badge (top-right) */}
        {newCount > 0 && (
          <span className="absolute top-1.5 right-1.5 text-[9px] font-bold px-1.5 py-0.5 rounded bg-orange-500/90 text-white shadow-sm">
            {newCount} new
          </span>
        )}

        {/* Duplicate-folder warning (bottom-left) */}
        {entry.duplicate && onShowDuplicate && (
          <DuplicateBadge
            duplicate={entry.duplicate}
            onClick={() => onShowDuplicate(entry)}
          />
        )}

        {/* Device Sync (bottom-right): chapters not yet on a target, or a
            check when in sync everywhere it is (sync/libraryView.js). */}
        <SyncChip chip={syncChip} />
      </div>

      {/* Info area */}
      <div className="p-2.5 flex flex-col gap-1 min-w-0">
        <h3 className="text-xs font-semibold leading-tight truncate" title={entry.title}>
          {entry.title}
        </h3>
        <div className="flex gap-1 flex-wrap">
          {formats.map((fmt) => (
            <FormatBadge key={fmt} fmt={fmt} className="text-[8px] px-1.5 py-0.5" />
          ))}
        </div>
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground mt-0.5">
          <span>{formatSize(entry.totalSize)}</span>
          <span>&middot;</span>
          {entry.isImageOnly ? (
            <span>{entry.imageCount} image{entry.imageCount !== 1 ? "s" : ""}</span>
          ) : (
            <span>{entry.files.length} file{entry.files.length !== 1 ? "s" : ""}</span>
          )}
          {entry.chapterCount > 0 && (
            <>
              <span>&middot;</span>
              <span>{entry.chapterCount} ch</span>
            </>
          )}
          {/* Native volumes (electron/library.js volumeCount). */}
          {entry.volumeCount > 0 && (
            <>
              <span>&middot;</span>
              <span>{entry.volumeCount} vol</span>
            </>
          )}
        </div>
      </div>
    </button>
  );
}

// ============================================================
// MERGE DUPLICATE FOLDERS DIALOG
//
// Two-step by contract, and the two steps are the whole safety model: opening
// the dialog asks main for a DRY RUN and renders exactly what it reports, and
// only the second click carries it out. Nothing here decides anything —
// electron/series-merge.js owns every rule (containment, identity, never
// overwriting, whether the husk can be removed) and this renders its answer.
//
// Chapters that collide are the number the user should actually read: a
// collision means the target ALREADY has that chapter, so the source's copy
// stays where it is. For a genuine rename fork that count is 0. For two
// different providers of one series it is usually most of the series, which is
// exactly the signal that merging them would only duplicate content.
//
// Cross-file: electron/library.js:findDuplicateSeries flags the entries,
// preload.js:mergeSeriesFolders is the bridge.
// ============================================================
function MergeDuplicatesDialog({ entry, onClose, onMerged }) {
  // [self, ...peers] with a chapter count each. Sorted richest-first so the
  // preselected target is the folder holding the bulk of the series — the same
  // rule main.js and aio-dl.py apply, but here it is only a DEFAULT the user
  // can override, not an authority.
  const members = useMemo(() => {
    const self = {
      folderPath: entry.folderPath,
      title: entry.title,
      site: entry.seriesMeta?.site || null,
      chapters:
        (entry.seriesMeta?.chapters_downloaded || []).length || entry.chapterCount || 0,
    };
    const peers = (entry.duplicate?.peers || []).map((p) => ({
      folderPath: p.folderPath,
      title: p.title,
      site: p.site,
      chapters: p.chapterCount || 0,
    }));
    return [self, ...peers].sort((a, b) => b.chapters - a.chapters);
  }, [entry]);

  const [target, setTarget] = useState(members[0]?.folderPath || "");
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);

  const sources = members.map((m) => m.folderPath).filter((p) => p !== target);
  const exact = entry.duplicate?.reason === "same-series";

  // Re-plan whenever the chosen target changes — the plan is entirely a
  // function of which folder survives.
  useEffect(() => {
    let cancelled = false;
    if (!target || sources.length === 0) return undefined;
    setPlan(null);
    setError(null);
    (async () => {
      try {
        const res = await window.electronAPI.mergeSeriesFolders({
          targetFolder: target,
          sourceFolders: sources,
          dryRun: true,
        });
        if (cancelled) return;
        if (res?.ok) setPlan(res);
        else setError(res?.error || "unknown_error");
      } catch (err) {
        if (!cancelled) setError(err?.message || String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
    // sources is derived from target; depending on its identity would re-run
    // this effect on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  const handleMerge = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await window.electronAPI.mergeSeriesFolders({
        targetFolder: target,
        sourceFolders: sources,
        dryRun: false,
      });
      if (res?.ok) {
        setDone(res);
        onMerged?.();
      } else {
        setError(res?.error || "unknown_error");
      }
    } catch (err) {
      setError(err?.message || String(err));
    }
    setBusy(false);
  };

  const totals = plan
    ? plan.plans.reduce(
        (acc, p) => ({
          moves: acc.moves + p.moves.length,
          collisions: acc.collisions + p.collisions.length,
          leftovers: acc.leftovers + p.leftovers.length,
          removable: acc.removable + (p.willRemoveSource ? 1 : 0),
        }),
        { moves: 0, collisions: 0, leftovers: 0, removable: 0 }
      )
    : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg max-h-[85vh] overflow-y-auto rounded-lg border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
          <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0" />
          <h2 className="text-sm font-semibold flex-1">
            {exact ? "This series is split across folders" : "Possible duplicate series"}
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose} className="h-7 w-7 p-0">
            <X className="w-3.5 h-3.5" />
          </Button>
        </div>

        <div className="px-4 py-3 space-y-4">
          <p className="text-xs text-muted-foreground leading-relaxed">
            {exact ? (
              <>
                These folders hold the <strong>same series from the same source</strong>
                {" "}— the site renamed it, so a second folder was created. Merging moves
                the chapters into one folder and deletes the empty one.
              </>
            ) : (
              <>
                These folders come from <strong>different sources</strong> but matched the
                same AniList entry. They may or may not be the same series, and the two
                sources number chapters independently — check the collision count below
                before merging.
              </>
            )}
          </p>

          {/* Target picker */}
          <div className="space-y-1.5">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              Keep everything in
            </p>
            {members.map((m) => (
              <label
                key={m.folderPath}
                className={cn(
                  "flex items-center gap-2.5 rounded border px-2.5 py-2 cursor-pointer transition-colors",
                  m.folderPath === target
                    ? "border-primary/50 bg-primary/5"
                    : "border-border/50 hover:bg-muted/30"
                )}
              >
                <input
                  type="radio"
                  name="merge-target"
                  className="accent-primary"
                  checked={m.folderPath === target}
                  onChange={() => setTarget(m.folderPath)}
                  disabled={busy || !!done}
                />
                <span className="min-w-0 flex-1">
                  <span className="block text-xs font-medium truncate" title={m.title}>
                    {m.title}
                  </span>
                  <span className="block text-[10px] text-muted-foreground tabular-nums">
                    {m.chapters} chapter{m.chapters === 1 ? "" : "s"}
                    {m.site ? ` · ${m.site}` : ""}
                  </span>
                </span>
              </label>
            ))}
          </div>

          {/* Plan / result */}
          {done ? (
            <div className="rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs">
              <p className="font-medium text-emerald-500">Merged.</p>
              <p className="mt-1 text-muted-foreground">
                {done.moved.length} file{done.moved.length === 1 ? "" : "s"} moved ·{" "}
                {done.chaptersBefore} → {done.chaptersAfter} chapters
                {done.removed.length > 0
                  ? ` · ${done.removed.length} empty folder removed`
                  : ""}
                {done.kept.length > 0
                  ? ` · ${done.kept.length} folder kept (still holds files)`
                  : ""}
              </p>
            </div>
          ) : error ? (
            <div className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs">
              <p className="font-medium text-destructive">{mergeErrorText(error)}</p>
            </div>
          ) : !plan ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              Working out what would move…
            </div>
          ) : (
            <div className="space-y-2 rounded border border-border/60 bg-muted/20 px-3 py-2.5 text-xs">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Chapters after merge</span>
                <span className="tabular-nums font-medium">
                  {plan.chaptersBefore} → {plan.chaptersAfter}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Files moved</span>
                <span className="tabular-nums font-medium">{totals.moves}</span>
              </div>
              {totals.collisions > 0 && (
                <div className="flex justify-between">
                  <span className="text-amber-500">
                    Already in the target — left where they are
                  </span>
                  <span className="tabular-nums font-medium text-amber-500">
                    {totals.collisions}
                  </span>
                </div>
              )}
              {totals.leftovers > 0 && (
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Unrecognized files — untouched</span>
                  <span className="tabular-nums font-medium">{totals.leftovers}</span>
                </div>
              )}
              <div className="flex justify-between border-t border-border/50 pt-2">
                <span className="text-muted-foreground">Emptied folders removed</span>
                <span className="tabular-nums font-medium">
                  {totals.removable} of {plan.plans.length}
                </span>
              </div>
              {totals.collisions > 0 && totals.moves === 0 && (
                <p className="pt-1 leading-relaxed text-amber-500">
                  Every chapter here is already in the target, so merging would move
                  nothing. These are probably two copies of the same run — delete the one
                  you don&apos;t want instead.
                </p>
              )}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-border/60 px-4 py-3">
          <Button variant="ghost" size="sm" onClick={onClose}>
            {done ? "Close" : "Cancel"}
          </Button>
          {!done && (
            <Button
              size="sm"
              onClick={handleMerge}
              disabled={busy || !plan || sources.length === 0}
            >
              {busy ? (
                <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />
              ) : (
                <Merge className="w-3.5 h-3.5 mr-1.5" />
              )}
              {busy ? "Merging…" : "Merge folders"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

// Refusal codes from electron/series-merge.js turned into something a person
// can act on. Anything unrecognized falls through as-is rather than being
// swallowed — an unexplained failure is worse than a raw code.
function mergeErrorText(code) {
  switch (code) {
    case "download_running":
      return "A download is running for this series. Wait for it to finish, then try again.";
    case "identity_mismatch":
      return "These folders don't provably hold the same series, so nothing was merged.";
    case "outside_library":
      return "One of these folders isn't inside your library folder.";
    case "missing_folder":
      return "One of these folders no longer exists — refresh the library.";
    case "unreadable_source":
      return "A folder couldn't be read. Check it isn't open in another program.";
    default:
      return `Merge failed: ${code}`;
  }
}

// ============================================================
// UPDATE CHECKER SECTION (inside detail view)
// ============================================================

// The VOLUME half of a series' update check (MangaFire native volumes). Shown
// when the check result carries volumes: the series tracks them, or the user
// pressed "Get volumes" (`browse`). Own tick state, own cross-outs
// (onSetChaptersIgnored(..., "volume") → volumes.ignored), own download
// button (libraryRecipe kind "volume"). While Komikku is on the download is
// refused, because the engine would turn the volume run into chapters
// (lib/downloadArgs.js KOMIKKU_VOLUMES_MESSAGE).
//
// browse: every listed volume starts UNTICKED, because a series that never had
// volumes reports all of them as missing (117 for One Piece) and an all-ticked
// default would queue the whole run with one click. Browse also offers NO
// cross-outs: writing volumes.ignored creates the volume block, and a block
// alone makes the series track volumes, so every later check would list the
// whole run as new. Cross-outs appear once a volume download made it tracked.
function VolumeUpdateBlock({
  entry, meta, result, browse, komikku, onStartDownload, onSwitchTab,
  onSetChaptersIgnored, onResultChange, onError,
}) {
  const newVolumes = result?.newVolumes || [];
  const ignoredVolumes = result?.ignoredVolumes || [];
  const [deselected, setDeselected] = useState(() => new Set(browse ? newVolumes : []));
  const [busy, setBusy] = useState(false);
  const selected = selectedChapters(newVolumes, deselected);

  const toggle = (v) =>
    setDeselected((prev) => {
      const next = new Set(prev);
      if (next.has(v)) next.delete(v);
      else next.add(v);
      return next;
    });

  const setIgnored = async (vols, ignored) => {
    setBusy(true);
    try {
      const res = await onSetChaptersIgnored?.(entry.folderPath, vols, ignored, "volume");
      if (res && res.ok === false) {
        onError(res.error || "Could not save the change");
        return;
      }
      const moved = new Set(vols);
      const byNumber = (a, b) => parseFloat(a) - parseFloat(b);
      onResultChange((prev) => {
        if (!prev) return prev;
        const wasNew = prev.newVolumes || [];
        const wasIgnored = prev.ignoredVolumes || [];
        return {
          ...prev,
          newVolumes: ignored
            ? wasNew.filter((c) => !moved.has(c))
            : [...wasNew, ...wasIgnored.filter((c) => moved.has(c))].sort(byNumber),
          ignoredVolumes: ignored
            ? [...wasIgnored, ...wasNew.filter((c) => moved.has(c))].sort(byNumber)
            : wasIgnored.filter((c) => !moved.has(c)),
        };
      });
    } finally {
      setBusy(false);
    }
  };

  const download = () => {
    if (!selected.length || !meta?.url || komikku) return;
    const keep = new Set(selected);
    const excluding = [...newVolumes.filter((c) => !keep.has(c)), ...ignoredVolumes];
    const rangeStr = chaptersToRangeString(selected, { excluding });
    onStartDownload(meta.url, {
      recipe: libraryRecipe(meta, rangeStr, entry.folderPath, "volume"),
    });
    onSwitchTab("queue");
  };

  if (result?.volumesError) {
    return (
      <div className="flex items-center gap-2 text-xs text-destructive">
        <AlertCircle className="w-3.5 h-3.5 shrink-0" />
        <span>Volumes: {result.volumesError}</span>
      </div>
    );
  }

  if (newVolumes.length === 0) {
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-xs text-emerald-400">
          <span>&#10003;</span>
          <span>
            {result?.volumesTotal
              ? `Volumes up to date (${result.volumesTotal} on site, ${result.volumesDownloaded ?? 0} on device)`
              : "This series has no volumes on the site in this language"}
          </span>
        </div>
        {ignoredVolumes.length > 0 && (
          <div className="rounded-md border border-border/50 bg-muted/10 p-3 space-y-2">
            <p className="text-[10px] leading-snug text-muted-foreground">
              <span className="text-foreground/80">
                {ignoredVolumes.length} volume{ignoredVolumes.length === 1 ? "" : "s"} crossed out
              </span>{" "}
              — saved to this series. Restore to make {ignoredVolumes.length === 1 ? "it" : "them"} downloadable again.
            </p>
            <ChapterChips
              chapters={[]}
              ignored={ignoredVolumes}
              deselected={deselected}
              onToggle={toggle}
              onSetIgnored={setIgnored}
              busy={busy}
              noun="volume"
            />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-md border border-sky-500/30 bg-sky-500/10 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-sky-400">
          {browse
            ? `${newVolumes.length} volume${newVolumes.length !== 1 ? "s" : ""} on the site`
            : `${newVolumes.length} new volume${newVolumes.length !== 1 ? "s" : ""} available`}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {result.volumesDownloaded ?? 0} / {result.volumesTotal ?? 0} volumes
        </span>
      </div>
      <ChapterChipsLegend
        noun="volume"
        crossOut={!browse}
        actions={
          newVolumes.length > 1 && (
            <span className="flex items-center gap-1.5 shrink-0">
              <button type="button" onClick={() => setDeselected(new Set())}
                className="hover:text-foreground transition-colors"
                title="Include every volume in this download">all</button>
              <span className="text-muted-foreground/40">|</span>
              <button type="button" onClick={() => setDeselected(new Set(newVolumes))}
                className="hover:text-foreground transition-colors"
                title="Skip every volume in this download (nothing is crossed out)">none</button>
            </span>
          )
        }
      />
      <ChapterChips
        chapters={newVolumes}
        ignored={ignoredVolumes}
        deselected={deselected}
        onToggle={toggle}
        onSetIgnored={browse ? undefined : setIgnored}
        busy={busy}
        noun="volume"
      />
      {komikku && (
        <p className="text-[10px] text-yellow-500 dark:text-yellow-400 leading-snug">
          {KOMIKKU_VOLUMES_MESSAGE} Turn it off in Settings → Output to download volumes.
        </p>
      )}
      <Button
        size="sm"
        onClick={download}
        disabled={selected.length === 0 || komikku}
        className="text-xs gap-1.5 w-full"
      >
        <Download className="w-3 h-3" />
        {selected.length === 0
          ? "Tick volumes to download"
          : selected.length === newVolumes.length
          ? `Download ${browse ? "All" : "Missing"} Volumes`
          : `Download ${selected.length} Selected Volume${selected.length === 1 ? "" : "s"}`}
      </Button>
    </div>
  );
}

// `onSetChaptersIgnored` is useUpdateCheck's setRowIgnored, threaded down from
// the tab. Going through the hook rather than calling the IPC directly is what
// keeps the Updates Center panel and the library entry in step when a chapter
// is crossed out from here instead of from there.
function UpdateSection({ entry, onStartDownload, onSwitchTab, settings, onSetChaptersIgnored }) {
  const meta = entry.seriesMeta;
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [manualUrl, setManualUrl] = useState("");
  const [saving, setSaving] = useState(false);
  // Unticked chapters — this-download-only, exactly as in the panel. Held as
  // deselections so chapters a later check adds arrive ticked.
  const [deselected, setDeselected] = useState(() => new Set());
  const [ignoreBusy, setIgnoreBusy] = useState(false);
  // "Get volumes" was used for this result: the series doesn't track volumes
  // yet, so the volume list is a browse, not a diff (VolumeUpdateBlock).
  const [volumeBrowse, setVolumeBrowse] = useState(false);
  // Bumped on every check so VolumeUpdateBlock remounts and re-seeds its ticks.
  const [checkSeq, setCheckSeq] = useState(0);
  const sites = useSiteCapabilities();
  const komikku = settings?.defaults?.komikku === true;
  const seriesTracksVolumes = (entry.trackedKinds || []).includes("volume");
  const canGetVolumes =
    !seriesTracksVolumes && volumesSupportedForSite(meta?.site, sites);
  // A volume-only series has no chapter half to show (its result's
  // trackedKinds omits "chapter"); results from before kinds existed do.
  const showChapters = !result?.trackedKinds || result.trackedKinds.includes("chapter");
  const showVolumes = !!result && (
    (result.trackedKinds || []).includes("volume") || "newVolumes" in result || !!result.volumesError
  );

  const newChapters = result?.newChapters || [];
  const ignoredChapters = result?.ignoredChapters || [];
  const selected = selectedChapters(newChapters, deselected);

  // opts.includeVolumes: the "Get volumes" button (list volumes for a series
  // that doesn't track them yet; main.js _checkSeriesUpdates).
  const handleCheck = async (opts = {}) => {
    setChecking(true);
    setError(null);
    setResult(null);
    setVolumeBrowse(!!opts.includeVolumes);
    setCheckSeq((n) => n + 1);
    // A fresh check re-offers everything; carrying ticks across it would mean
    // silently dropping chapters the user unticked before a different result.
    setDeselected(new Set());
    try {
      const res = await window.electronAPI.checkForUpdates(entry.folderPath, opts);
      if (res.error) {
        setError(res.message || res.error);
      } else {
        setResult(res);
      }
    } catch (err) {
      setError(err.message || "Check failed");
    }
    setChecking(false);
  };

  const toggleChapter = (chapter) => {
    setDeselected((prev) => {
      const next = new Set(prev);
      if (next.has(chapter)) next.delete(chapter);
      else next.add(chapter);
      return next;
    });
  };

  // Cross out / restore. Persists through the hook (which writes
  // .aio_series.json and updates the panel row), then moves the chapters in
  // this section's own `result` — that copy came from a one-shot IPC and has
  // nothing else to refresh it.
  const handleSetIgnored = async (chapters, ignored) => {
    setIgnoreBusy(true);
    try {
      const res = await onSetChaptersIgnored?.(entry.folderPath, chapters, ignored);
      if (res && res.ok === false) {
        setError(res.error || "Could not save the change");
        return;
      }
      const moved = new Set(chapters);
      const byNumber = (a, b) => parseFloat(a) - parseFloat(b);
      setResult((prev) => {
        if (!prev) return prev;
        const wasNew = prev.newChapters || [];
        const wasIgnored = prev.ignoredChapters || [];
        return {
          ...prev,
          newChapters: ignored
            ? wasNew.filter((c) => !moved.has(c))
            : [...wasNew, ...wasIgnored.filter((c) => moved.has(c))].sort(byNumber),
          ignoredChapters: ignored
            ? [...wasIgnored, ...wasNew.filter((c) => moved.has(c))].sort(byNumber)
            : wasIgnored.filter((c) => !moved.has(c)),
        };
      });
      if (ignored) {
        setDeselected((prev) => {
          const next = new Set(prev);
          for (const c of chapters) next.delete(c);
          return next;
        });
      }
    } finally {
      setIgnoreBusy(false);
    }
  };

  const handleDownloadNew = () => {
    if (!selected.length || !meta?.url) return;
    // Unticked + crossed-out chapters must not be swept up by a range that
    // spans them — see the excluding note on chaptersToRangeString.
    const keep = new Set(selected);
    const excluding = [...newChapters.filter((c) => !keep.has(c)), ...ignoredChapters];
    const rangeStr = chaptersToRangeString(selected, { excluding });

    // Queue a "library" recipe: when the download STARTS, the saved Settings
    // defaults are applied, format/language/site come from the series
    // metadata, and the chapter range is only the missing ones. Shared with
    // the Updates Center per-row queue (buildDownloadArgsForRow). The builder
    // (lib/downloadArgs.js:buildLibraryDownloadArgs) forces lazy multi-source
    // for both update-check paths: a 1-2 chapter delta shouldn't pay the eager
    // cross-site discovery. See that builder's header note.
    onStartDownload(meta.url, { recipe: libraryRecipe(meta, rangeStr, entry.folderPath) });
    onSwitchTab("queue");
  };

  const handleSaveUrl = async () => {
    if (!manualUrl.trim()) return;
    setSaving(true);
    try {
      const res = await window.electronAPI.saveSeriesMeta(entry.folderPath, {
        url: manualUrl.trim(),
        title: entry.title,
      });
      if (res.ok) {
        // Mutate the entry's seriesMeta so the UI refreshes immediately
        entry.seriesMeta = res.meta;
        setManualUrl("");
      } else {
        setError(res.error || "Failed to save");
      }
    } catch (err) {
      setError(err.message);
    }
    setSaving(false);
  };

  // ── No metadata: show manual URL entry ──
  if (!meta?.url) {
    return (
      <div className="rounded-lg border border-border/50 bg-card/30 p-4 space-y-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" />
          <span>No source URL saved. Enter it to enable update checking.</span>
        </div>
        <div className="flex gap-2">
          <Input
            value={manualUrl}
            onChange={(e) => setManualUrl(e.target.value)}
            placeholder="https://mangafire.to/title/..."
            className="h-8 text-xs flex-1"
          />
          <Button
            size="sm"
            onClick={handleSaveUrl}
            disabled={saving || !manualUrl.trim()}
            className="text-xs gap-1.5 shrink-0"
          >
            {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Link className="w-3 h-3" />}
            Save
          </Button>
        </div>
        {error && <p className="text-[10px] text-destructive">{error}</p>}
      </div>
    );
  }

  // ── Has metadata: show update check UI ──
  return (
    <div className="rounded-lg border border-border/50 bg-card/30 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Globe className="w-3.5 h-3.5" />
          <span className="truncate max-w-[280px]" title={meta.url}>
            {meta.site || "unknown"}
          </span>
          {meta.chapters_downloaded?.length > 0 && (
            <>
              <span>&middot;</span>
              <span>{meta.chapters_downloaded.length} ch downloaded</span>
            </>
          )}
          {entry.volumeCount > 0 && (
            <>
              <span>&middot;</span>
              <span>{entry.volumeCount} vol downloaded</span>
            </>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {/* MangaFire series that has only chapters so far: list its volumes. */}
          {canGetVolumes && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => handleCheck({ includeVolumes: true })}
              disabled={checking}
              className="text-xs gap-1.5"
              title="List this series' volumes on the site and pick some to download"
            >
              <BookOpen className="w-3 h-3" />
              Get volumes
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => handleCheck()}
            disabled={checking}
            className="text-xs gap-1.5"
          >
            {checking
              ? <Loader2 className="w-3 h-3 animate-spin" />
              : <RefreshCw className="w-3 h-3" />}
            {checking ? "Checking…" : "Check for Updates"}
          </Button>
        </div>
      </div>

      {error && (
        <div className="flex items-center gap-2 text-xs text-destructive">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {result && showVolumes && (
        <VolumeUpdateBlock
          key={checkSeq}
          entry={entry}
          meta={meta}
          result={result}
          browse={volumeBrowse}
          komikku={komikku}
          onStartDownload={onStartDownload}
          onSwitchTab={onSwitchTab}
          onSetChaptersIgnored={onSetChaptersIgnored}
          onResultChange={setResult}
          onError={setError}
        />
      )}

      {result && showChapters && (
        <div className="space-y-2">
          {newChapters.length > 0 ? (
            <div className="rounded-md border border-orange-500/30 bg-orange-500/10 p-3 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-orange-400">
                  {newChapters.length}
                  {result.checkMode === "files"
                    ? ` chapter${newChapters.length !== 1 ? "s" : ""} missing from device`
                    : ` new chapter${newChapters.length !== 1 ? "s" : ""} available`}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  {result.downloaded} / {result.total} total
                </span>
              </div>
              {/* The plain "Chapters: 51-53" line became these chips: same
                  information, plus the two per-chapter gestures. Crossed-out
                  chapters are interleaved in numeric order by ChapterChips, and
                  the wording is shared with the Updates Center panel so the two
                  surfaces can't explain the same buttons differently. */}
              <ChapterChipsLegend
                actions={
                  newChapters.length > 1 && (
                    <span className="flex items-center gap-1.5 shrink-0">
                      <button
                        type="button"
                        onClick={() => setDeselected(new Set())}
                        className="hover:text-foreground transition-colors"
                        title="Include every chapter in this download"
                      >
                        all
                      </button>
                      <span className="text-muted-foreground/40">|</span>
                      <button
                        type="button"
                        onClick={() => setDeselected(new Set(newChapters))}
                        className="hover:text-foreground transition-colors"
                        title="Skip every chapter in this download (nothing is crossed out)"
                      >
                        none
                      </button>
                    </span>
                  )
                }
              />
              <ChapterChips
                chapters={newChapters}
                ignored={ignoredChapters}
                deselected={deselected}
                onToggle={toggleChapter}
                onSetIgnored={handleSetIgnored}
                busy={ignoreBusy}
              />
              <Button
                size="sm"
                onClick={handleDownloadNew}
                disabled={selected.length === 0}
                className="text-xs gap-1.5 w-full"
              >
                <Download className="w-3 h-3" />
                {selected.length === newChapters.length
                  ? "Download Missing Chapters"
                  : `Download ${selected.length} Selected Chapter${selected.length === 1 ? "" : "s"}`}
              </Button>
              {/* Mode indicator */}
              <p className="text-[9px] text-muted-foreground/60 text-right">
                Checked via {result.checkMode === "files" ? "file scan" : "download history"}
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-xs text-emerald-400">
                <span>&#10003;</span>
                <span>
                  Up to date ({result.total} on site, {result.downloaded} on device)
                </span>
                <span className="text-[9px] text-muted-foreground/60 ml-auto">
                  via {result.checkMode === "files" ? "file scan" : "history"}
                </span>
              </div>
              {/* A series can read up to date purely because every missing
                  chapter is crossed out. Showing them here is the only way
                  back — without it the cross-out would be one-way. */}
              {ignoredChapters.length > 0 && (
                <div className="rounded-md border border-border/50 bg-muted/10 p-3 space-y-2">
                  {/* No legend here: with nothing left to tick, the tick half
                      would describe a control that isn't on screen. This says
                      the part that matters instead — the cross-out is SAVED, so
                      it is why the series reads up to date. */}
                  <p className="text-[10px] leading-snug text-muted-foreground">
                    <span className="text-foreground/80">
                      {ignoredChapters.length} chapter{ignoredChapters.length === 1 ? "" : "s"}{" "}
                      crossed out
                    </span>{" "}
                    — saved to this series, so every check leaves{" "}
                    {ignoredChapters.length === 1 ? "it" : "them"} out. Restore to make{" "}
                    {ignoredChapters.length === 1 ? "it" : "them"} downloadable again.
                  </p>
                  <ChapterChips
                    chapters={[]}
                    ignored={ignoredChapters}
                    deselected={deselected}
                    onToggle={toggleChapter}
                    onSetIgnored={handleSetIgnored}
                    busy={ignoreBusy}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================
// DETAIL VIEW
// ============================================================
function MetadataEditorPanel({ entry, onClose, onSaved }) {
  const editableFiles = entry.files.filter((f) => ["cbz", "epub", "pdf"].includes(f.ext));
  const primaryFile = editableFiles[0];
  const [form, setForm] = useState({
    title: entry.title || "",
    writers: "",
    pencillers: "",
    genres: "",
    publisher: "",
    synopsis: "",
  });
  const [coverPath, setCoverPath] = useState("");
  const [applyAll, setApplyAll] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!primaryFile || !window.electronAPI?.readMetadata) return;
      setBusy(true);
      setError(null);
      try {
        const metadata = await window.electronAPI.readMetadata(primaryFile.path);
        if (!cancelled && metadata) {
          setForm((prev) => ({
            ...prev,
            ...metadata,
            writers: Array.isArray(metadata.writers) ? metadata.writers.join(", ") : (metadata.writers || ""),
            pencillers: Array.isArray(metadata.pencillers) ? metadata.pencillers.join(", ") : (metadata.pencillers || ""),
            genres: Array.isArray(metadata.genres) ? metadata.genres.join(", ") : (metadata.genres || ""),
          }));
        }
      } catch (err) {
        if (!cancelled) setError(err.message || "Could not read metadata");
      } finally {
        if (!cancelled) setBusy(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [primaryFile?.path]);

  const updateField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const handlePickCover = async () => {
    const picked = await window.electronAPI?.pickFile?.([
      { name: "Images", extensions: ["jpg", "jpeg", "png", "webp"] },
    ]);
    if (picked) setCoverPath(picked);
  };

  const handleSave = async () => {
    if (!primaryFile || !window.electronAPI?.updateMetadata) return;
    setBusy(true);
    setError(null);
    const payload = {
      ...form,
      writers: form.writers.split(",").map((s) => s.trim()).filter(Boolean),
      pencillers: form.pencillers.split(",").map((s) => s.trim()).filter(Boolean),
      genres: form.genres.split(",").map((s) => s.trim()).filter(Boolean),
    };
    try {
      const targets = applyAll ? editableFiles : [primaryFile];
      for (const file of targets) {
        await window.electronAPI.updateMetadata(file.path, payload, coverPath || null);
      }
      onSaved?.();
      onClose();
    } catch (err) {
      setError(err.message || "Could not update metadata");
    } finally {
      setBusy(false);
    }
  };

  if (!primaryFile) return null;

  return (
    <div className="mt-4 border border-border/40 bg-card/30 rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          Embedded Metadata
        </h3>
        <Button variant="ghost" size="sm" onClick={onClose} className="h-7 w-7 p-0">
          <X className="w-3.5 h-3.5" />
        </Button>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Input value={form.title} onChange={(e) => updateField("title", e.target.value)} placeholder="Title" />
        <Input value={form.publisher} onChange={(e) => updateField("publisher", e.target.value)} placeholder="Publisher" />
        <Input value={form.writers} onChange={(e) => updateField("writers", e.target.value)} placeholder="Writers" />
        <Input value={form.pencillers} onChange={(e) => updateField("pencillers", e.target.value)} placeholder="Pencillers" />
        <Input className="col-span-2" value={form.genres} onChange={(e) => updateField("genres", e.target.value)} placeholder="Genres" />
      </div>
      <textarea
        className="w-full min-h-20 rounded-md border border-input bg-background px-3 py-2 text-xs"
        value={form.synopsis}
        onChange={(e) => updateField("synopsis", e.target.value)}
        placeholder="Synopsis"
      />
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={handlePickCover} className="text-xs">
          Cover
        </Button>
        <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground">
          {coverPath || "No cover selected"}
        </span>
        <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <input type="checkbox" checked={applyAll} onChange={(e) => setApplyAll(e.target.checked)} />
          Apply to all
        </label>
        <Button size="sm" onClick={handleSave} disabled={busy} className="text-xs gap-1.5">
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
          Save
        </Button>
      </div>
      {error && <p className="text-[10px] text-destructive">{error}</p>}
    </div>
  );
}

function DetailView({
  entry, onBack, onRefresh, onStartDownload, onSwitchTab, settings,
  // (groupKey, facetKey) → add that value to the grid's facet filter and
  // navigate back. Supplied by LibraryTab; grep addFacet.
  onFilterByFacet,
  // Pass-through for UpdateSection's per-chapter cross-out (useUpdateCheck's
  // setRowIgnored). Routed through the hook so a cross-out made here also
  // lands on the Updates Center row and the library entry.
  onSetChaptersIgnored,
  // Device Sync (U4): this series' libraryStatus rows + the configured
  // targets, or null while sync is off; onOpenInSync(folderPath).
  syncLibrary, onOpenInSync,
}) {
  const [deleting, setDeleting] = useState(false);
  // Two-step delete confirmation (avoids window.confirm which breaks
  // Electron's renderer focus/input handling)
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState(null);
  const [showMetadataEditor, setShowMetadataEditor] = useState(false);

  const handleOpenFile = async (filePath) => {
    if (window.electronAPI?.openFile) {
      await window.electronAPI.openFile(filePath);
    }
  };

  const handleOpenFolder = () => {
    if (window.electronAPI?.openFolder) {
      window.electronAPI.openFolder(entry.folderPath);
    }
  };

  // Open a single chapter's image folder in the OS file explorer. Image-only
  // (--format none) series have no archive files to "open", so the per-chapter
  // rows in the Files section call this instead. openFolder → shell.openPath
  // accepts any path (see main.js "open-folder" handler).
  const handleOpenChapter = (chapterPath) => {
    if (window.electronAPI?.openFolder) {
      window.electronAPI.openFolder(chapterPath);
    }
  };

  const handleDelete = async () => {
    if (!confirmDelete) {
      // First click — show confirmation
      setConfirmDelete(true);
      // Auto-cancel after 4 seconds
      setTimeout(() => setConfirmDelete(false), 4000);
      return;
    }
    // Second click — actually delete
    setDeleting(true);
    setConfirmDelete(false);
    if (window.electronAPI?.deleteSeries) {
      const result = await window.electronAPI.deleteSeries(entry.folderPath);
      if (result.ok) {
        onRefresh();
        onBack();
      } else {
        setDeleteError("Failed to delete: " + result.error);
        setDeleting(false);
      }
    }
  };

  const formats = getEntryFormats(entry);
  const meta = entry.seriesMeta;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-3 border-b bg-card/30">
        <Button variant="ghost" size="sm" onClick={onBack} className="gap-1.5">
          <ArrowLeft className="w-3.5 h-3.5" />
          Back
        </Button>
        <h2 className="text-sm font-semibold truncate">{entry.title}</h2>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        <div className="flex gap-5">
          {/* Cover */}
          <div className="w-44 shrink-0 aspect-[3/4] rounded-lg overflow-hidden border border-border/50 bg-muted/30 relative">
            <PdfCover entry={entry} />
            {syncLibrary && <SyncChip chip={seriesSyncChip(syncLibrary.bySeries[entry.folderPath], syncLibrary.targets)} />}
          </div>

          {/* Info */}
          <div className="flex-1 min-w-0 space-y-3">
            <h2 className="text-lg font-bold leading-tight">{entry.title}</h2>

            {/* Series metadata from .aio_series.json */}
            {meta && (
              <div className="space-y-1.5">
                {meta.status && (
                  <StatusBadge status={meta.status} className="inline-block text-[10px] px-2 py-0.5" />
                )}
                {meta.authors?.length > 0 && (
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <User className="w-3 h-3 shrink-0" />
                    <span>{meta.authors.join(", ")}</span>
                  </div>
                )}
                {meta.genres?.length > 0 && (
                  <div className="flex items-start gap-1.5">
                    <Tag className="w-3 h-3 shrink-0 mt-1 text-muted-foreground" />
                    <div className="flex flex-wrap gap-1">
                      {meta.genres.map((g) => (
                        <MetaChip
                          key={g}
                          label={g}
                          onClick={() => onFilterByFacet?.("genres", facetKey(g))}
                        />
                      ))}
                    </div>
                  </div>
                )}
                <AnilistTagChips meta={meta} onFilterByFacet={onFilterByFacet} />
              </div>
            )}

            {/* Stats row */}
            <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
              <div className="flex items-center gap-1">
                <FileText className="w-3.5 h-3.5" />
                {entry.isImageOnly
                  ? `${entry.imageCount} image${entry.imageCount !== 1 ? "s" : ""}`
                  : `${entry.files.length} file${entry.files.length !== 1 ? "s" : ""}`}
              </div>
              <div>{formatSize(entry.totalSize)}</div>
              {meta?.chapters_downloaded?.length > 0 && (
                <div>{meta.chapters_downloaded.length} chapters</div>
              )}
              {entry.volumeCount > 0 && (
                <div>{entry.volumeCount} volume{entry.volumeCount === 1 ? "" : "s"}</div>
              )}
              {entry.lastModified && <div>Modified {formatDate(entry.lastModified)}</div>}
            </div>

            {/* Device Sync facts for this series (design D9), with Open in Sync. */}
            {syncLibrary && (
              <SyncLine
                lines={seriesSyncLines(syncLibrary.bySeries[entry.folderPath], syncLibrary.targets)}
                onOpen={onOpenInSync ? () => onOpenInSync(entry.folderPath) : undefined}
              />
            )}

            {/* Format badges */}
            <div className="flex gap-1.5">
              {formats.map((fmt) => (
                <FormatBadge key={fmt} fmt={fmt} className="text-[10px] px-2 py-1" />
              ))}
            </div>

            {/* Action buttons */}
            <div className="flex gap-2 pt-1">
              <Button variant="outline" size="sm" onClick={handleOpenFolder} className="gap-1.5 text-xs">
                <FolderOpen className="w-3.5 h-3.5" />
                Open Folder
              </Button>
              {meta?.url && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => window.open(meta.url, "_blank")}
                  className="gap-1.5 text-xs"
                >
                  <Globe className="w-3.5 h-3.5" />
                  Source
                </Button>
              )}
              {/* Embedded-metadata editing writes into a ComicInfo.xml inside
                  the archive; image-only (--format none) series have no
                  archive, so the editor has nothing to act on. Hide it there
                  (MetadataEditorPanel also self-guards by returning null). */}
              {entry.files.length > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setShowMetadataEditor((value) => !value)}
                  className="gap-1.5 text-xs"
                >
                  <PencilLine className="w-3.5 h-3.5" />
                  Metadata
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={handleDelete}
                onBlur={() => setConfirmDelete(false)}
                disabled={deleting}
                className={cn(
                  "gap-1.5 text-xs",
                  confirmDelete
                    ? "text-destructive bg-destructive/10 border-destructive/50 hover:bg-destructive/20 hover:text-destructive hover:border-destructive/50"
                    : "text-destructive hover:text-destructive hover:border-destructive/50"
                )}
              >
                <Trash2 className="w-3.5 h-3.5" />
                {deleting ? "Deleting…" : confirmDelete ? "Are you sure?" : "Delete"}
              </Button>
            </div>
            {deleteError && (
              <p className="text-[10px] text-destructive mt-1">{deleteError}</p>
            )}
            {showMetadataEditor && (
              <MetadataEditorPanel
                entry={entry}
                onClose={() => setShowMetadataEditor(false)}
                onSaved={onRefresh}
              />
            )}
          </div>
        </div>

        {/* Update checking section */}
        <div className="mt-5">
          <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
            Updates
          </h3>
          <UpdateSection
            entry={entry}
            onStartDownload={onStartDownload}
            onSwitchTab={onSwitchTab}
            settings={settings}
            onSetChaptersIgnored={onSetChaptersIgnored}
          />
        </div>

        {/* File list (archives) — or per-chapter image folders for image-only
            (--format none) series, which have no archive to open. Each chapter
            row opens its images/Chapter_<n>/ folder in the OS file explorer
            (handleOpenChapter). imageChapters comes from library.js. */}
        <div className="mt-5">
          <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
            {entry.isImageOnly ? "Chapters" : "Files"}
          </h3>
          <div className="space-y-1">
            {entry.isImageOnly
              ? (entry.imageChapters || []).map((chap) => (
                  <button
                    key={chap.path}
                    onClick={() => handleOpenChapter(chap.path)}
                    className={cn(
                      "w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left",
                      "bg-card/40 border border-border/30",
                      "hover:bg-card/80 hover:border-primary/30",
                      "transition-all duration-100 group"
                    )}
                  >
                    <FormatBadge
                      fmt="images"
                      label="IMG"
                      className="text-[9px] px-1.5 py-0.5 shrink-0"
                    />
                    <span className="text-xs font-medium truncate flex-1">
                      {chap.name.replace(/_/g, " ")}
                    </span>
                    <span className="text-[10px] text-muted-foreground shrink-0">
                      {chap.imageCount} img &middot; {formatSize(chap.size)}
                    </span>
                    <FolderOpen className="w-3.5 h-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />
                  </button>
                ))
              : entry.files.map((file) => (
                  <button
                    key={file.path}
                    onClick={() => handleOpenFile(file.path)}
                    className={cn(
                      "w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left",
                      "bg-card/40 border border-border/30",
                      "hover:bg-card/80 hover:border-primary/30",
                      "transition-all duration-100 group"
                    )}
                  >
                    <FormatBadge fmt={file.ext} className="text-[9px] px-1.5 py-0.5 shrink-0" />
                    <span className="text-xs font-medium truncate flex-1">{file.name}</span>
                    <span className="text-[10px] text-muted-foreground shrink-0">
                      {formatSize(file.size)}
                    </span>
                    <ExternalLink className="w-3.5 h-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />
                  </button>
                ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================
// EMPTY STATE
// ============================================================
function EmptyState() {
  return (
    <div className="flex-1 flex flex-col items-center justify-center text-center py-20 px-8">
      <div className="w-16 h-16 rounded-full bg-muted/50 flex items-center justify-center mb-4">
        <BookOpen className="w-7 h-7 text-muted-foreground/50" />
      </div>
      <h3 className="text-sm font-semibold mb-1">No manga yet</h3>
      <p className="text-xs text-muted-foreground max-w-xs">
        Downloaded manga will appear here. Go to the{" "}
        <span className="inline-flex items-center gap-0.5 text-primary">
          <Download className="w-3 h-3" /> New
        </span>{" "}
        tab to start downloading.
      </p>
    </div>
  );
}

// Third empty state, distinct from EmptyState (nothing downloaded yet) and the
// no-search-results line: the library HAS content and the user's own facet
// selection is what's hiding it, so the fix is one click away.
function FilteredEmptyState({ searchQuery, onClearFilters }) {
  return (
    <div className="flex-1 flex flex-col items-center justify-center text-center py-20 px-8 gap-3">
      <div className="w-16 h-16 rounded-full bg-muted/50 flex items-center justify-center">
        <Filter className="w-7 h-7 text-muted-foreground/50" />
      </div>
      <div>
        <h3 className="text-sm font-semibold mb-1">No matches</h3>
        <p className="text-xs text-muted-foreground max-w-xs">
          {searchQuery
            ? <>Nothing matches these filters and &ldquo;{searchQuery}&rdquo;.</>
            : "Nothing in your library matches every active filter."}
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onClearFilters} className="gap-1.5 text-xs">
        <FilterX className="w-3.5 h-3.5" />
        Clear filters
      </Button>
    </div>
  );
}

// ============================================================
// LIBRARY TAB (main export)
// ============================================================
export default function LibraryTab({
  onStartDownload, onSwitchTab, settings, onSaveSettings,
  // Lifted state from useDownloader. libraryEntries is null until first
  // load completes (so we know whether to trigger an initial fetch on
  // mount); loadLibrary forces a fresh scan. There is deliberately no
  // setLibraryEntries here any more — the one writer was the update-sweep's
  // updatedMeta splice, which moved into hooks/useUpdateCheck.js along with
  // the rest of the scan.
  libraryEntries, libraryLoading, loadLibrary,
  // The "Check All" sweep, owned by useDownloader (hooks/useUpdateCheck.js)
  // because the sweep runs in the main process and outlives this component —
  // App.jsx unmounts the whole tab on every switch. Everything scan-shaped
  // that used to be useState in here now arrives through this prop; the only
  // scan state LibraryTab still owns is whether the panel is open.
  updateCheck,
  // Device Sync (UI pass U4): {bySeries, targets} from useDeviceSync's
  // libraryStatus + config, or null while sync is off (no chip, no line).
  // onOpenInSync(folderPath) is App's deep link into the Sync tab.
  syncLibrary = null, onOpenInSync,
}) {
  const entries = libraryEntries || [];
  const loading = libraryLoading || libraryEntries === null;
  const [searchQuery, setSearchQuery] = useState("");
  // Lazy-init from persisted settings.libraryOpts.sortBy. Falls back to "title"
  // for first run / older settings dicts. Sync below via useEffect when the
  // settings prop hydrates asynchronously from disk on app launch.
  const [sortBy, setSortBy] = useState(() => settings?.libraryOpts?.sortBy ?? "title");

  // Sync once when settings.libraryOpts.sortBy arrives from disk (history.json
  // load is async). Same shape as SearchTab's settings.searchOpts hydration.
  useEffect(() => {
    const persisted = settings?.libraryOpts?.sortBy;
    if (persisted && persisted !== sortBy) {
      setSortBy(persisted);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings?.libraryOpts?.sortBy]);

  // ── Facet filters ──
  const [filters, setFilters] = useState(() =>
    filtersFromJson(settings?.libraryOpts?.filters)
  );
  const [filterPanelOpen, setFilterPanelOpen] = useState(false);
  const filterAnchorRef = useRef(null);

  // Hydration is ONE-SHOT, unlike sortBy's above: the persisted value is an
  // object, so an equality guard would need a deep compare and a plain
  // identity dep would re-fire on every settings save and clobber the live
  // selection. Also latched by applyFilters, so a slow disk load can't
  // overwrite a selection the user already made.
  const filtersHydratedRef = useRef(false);
  useEffect(() => {
    if (filtersHydratedRef.current) return;
    const persisted = settings?.libraryOpts;
    if (!persisted) return;
    filtersHydratedRef.current = true;
    setFilters(filtersFromJson(persisted.filters));
  }, [settings?.libraryOpts]);

  // Single writer for settings.libraryOpts, so the sort write and the filter
  // write can't drop each other's key. Same spread-merge as the original
  // updateSort, but merging onto a ref instead of the `settings` prop: the
  // prop only refreshes after saveSettings round-trips through IPC, so two
  // writes in quick succession would both merge onto the same stale base.
  const libraryOptsRef = useRef(settings?.libraryOpts || {});
  useEffect(() => {
    if (settings?.libraryOpts) libraryOptsRef.current = settings.libraryOpts;
  }, [settings?.libraryOpts]);

  const persistLibraryOpts = useCallback((patch) => {
    const next = { ...libraryOptsRef.current, ...patch };
    libraryOptsRef.current = next;
    onSaveSettings?.({ libraryOpts: next });
  }, [onSaveSettings]);

  const updateSort = (value) => {
    setSortBy(value);
    persistLibraryOpts({ sortBy: value });
  };

  // Every filter mutation funnels through here so persistence can't be
  // forgotten. Reads `filters` from the closure rather than using a functional
  // updater: the persist call is a side effect and React StrictMode invokes
  // updaters twice. Chip clicks are user-paced, so there's no batching hazard.
  const applyFilters = useCallback((next) => {
    filtersHydratedRef.current = true;
    setFilters(next);
    persistLibraryOpts({ filters: filtersToJson(next) });
  }, [persistLibraryOpts]);

  const toggleFacet = useCallback((groupKey, valueKey) => {
    const nextSet = new Set(filters[groupKey]);
    if (nextSet.has(valueKey)) nextSet.delete(valueKey);
    else nextSet.add(valueKey);
    applyFilters({ ...filters, [groupKey]: nextSet });
  }, [filters, applyFilters]);

  // Additive-only variant for DetailView's chips — clicking a genre there
  // means "show me more of this", never "turn it off".
  const addFacet = useCallback((groupKey, valueKey) => {
    if (!valueKey || filters[groupKey].has(valueKey)) return;
    applyFilters({ ...filters, [groupKey]: new Set(filters[groupKey]).add(valueKey) });
  }, [filters, applyFilters]);

  const clearFilters = useCallback(
    () => applyFilters(makeEmptyFilters()),
    [applyFilters]
  );
  const setMatchMode = useCallback(
    (mode) => applyFilters({ ...filters, matchMode: mode }),
    [filters, applyFilters]
  );
  const setShowSpoilerTags = useCallback(
    (value) => applyFilters({ ...filters, showSpoilerTags: value }),
    [filters, applyFilters]
  );
  const closeFilterPanel = useCallback(() => setFilterPanelOpen(false), []);

  const [selectedEntry, setSelectedEntry] = useState(null);
  // The entry whose duplicate badge was clicked, or null. Holds the ENTRY (not
  // just the paths) because the dialog reads its peers, titles and chapter
  // counts straight off entry.duplicate — see electron/library.js's
  // findDuplicateSeries for what that carries.
  const [duplicateEntry, setDuplicateEntry] = useState(null);

  // ── Updates Center state (read-only here) ──
  // seriesStates is Map<folderPath, row>; row shape is documented on
  // electron/preload.js:onUpdateCheckProgress and built in electron/main.js.
  // newChapterCounts (folderPath → count) drives the orange "+N new" badge
  // on each MangaCard.
  const {
    rows: seriesStates,
    scanState,          // "idle" | "running" | "done"
    scanStats,          // { completed, total, durationMs, aborted }
    newChapterCounts,
    foundCount: updatesFoundCount,
    start: startUpdateCheck,
    cancel: cancelUpdateCheck,
    resolveRows: resolveUpdateRows,
    setRowIgnored,
  } = updateCheck;

  // The panel's open/closed flag is the one piece of scan-adjacent state that
  // is genuinely per-mount: the panel is a full-screen overlay whose backdrop
  // swallows the click that would switch tabs, so it can never be open across
  // a switch anyway.
  const [updatesPanelOpen, setUpdatesPanelOpen] = useState(false);

  // ── Load library on first mount only ──
  // libraryEntries is the null sentinel until the first scan completes.
  // Subsequent tab switches see an array and skip the fetch — the entries
  // and pending thumbnail-ready events are managed at the hook level.
  useEffect(() => {
    if (libraryEntries === null && !libraryLoading) {
      loadLibrary();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Updates Center entry points ──
  // The progress subscription used to live here, which is precisely why the
  // sweep looked like it died on a tab switch — this component unmounts and
  // took the listener (and every row it had collected) with it. It lives in
  // hooks/useUpdateCheck.js now; the three callbacks below are all that is
  // left, and they only decide WHEN a scan is allowed to start.
  const handleOpenPanel = useCallback(() => setUpdatesPanelOpen(true), []);

  // Starts a sweep only when there is nothing to show yet — see the toolbar
  // button's comment for the full state table. A no-op against a live sweep
  // (the hook and main both refuse), so a stray click can't restart a scan
  // that is 20 series deep.
  const handleCheckAll = useCallback(() => {
    setUpdatesPanelOpen(true);
    startUpdateCheck();
  }, [startUpdateCheck]);

  // The explicit "I want fresh results" gesture (panel header). The ONLY
  // caller that passes force, i.e. the only one that may preempt a running
  // sweep.
  const handleRescan = useCallback(() => {
    setUpdatesPanelOpen(true);
    startUpdateCheck({ force: true });
  }, [startUpdateCheck]);

  // ── Per-row queue (called from the panel) ──
  // Builds the same download args as DetailView's "Download Missing
  // Chapters" path so the user gets identical behavior whether they queue
  // from the panel or the detail view. Pulls defaults from settings.
  //
  // Both paths force lazy multi-source via buildLibraryDownloadArgs: an
  // update-check download is a 1-2 chapter delta, so when --multi-source is
  // on in settings the ~30-80 s eager cross-site discovery would dwarf the
  // actual download. The builder sets multiSourceLazy:true (overriding a
  // global lazy opt-out); downloader.js emits --multi-source-lazy whenever
  // multiSource is on and multiSourceLazy !== false. See the builder header.
  // `chapters` is the subset the panel says to queue — the user's ticked
  // chapters, never simply row.newChapters. Everything the row offered that is
  // NOT in it (unticked chapters, plus the crossed-out ones main split off into
  // ignoredChapters) is handed to chaptersToRangeString as `excluding`, and
  // that is load-bearing rather than cosmetic: aio-dl.py reads "10-11" as a
  // closed interval, so without it a range spanning an excluded 10.5 would
  // download the very chapter the user just crossed out. See the excluding
  // note on chaptersToRangeString.
  // An array passed for `chapters` is AUTHORITATIVE, empty included — falling
  // back to row.newChapters when it happens to be empty would turn "queue
  // nothing" into "queue everything", which is the worst possible reading of a
  // user who just unticked the lot. Callers guard against empty before getting
  // here; omitting the argument entirely still means "the whole row".
  //
  // kind "volume": the same, over the row's newVolumes / ignoredVolumes, and a
  // volume recipe (the series' volume format/language; lib/queueRecipe.js).
  const buildDownloadArgsForRow = useCallback((row, entry, chapters, kind = "chapter") => {
    const meta = entry?.seriesMeta || {};
    const isVolume = kind === "volume";
    const offered = (isVolume ? row.newVolumes : row.newChapters) || [];
    const crossed = (isVolume ? row.ignoredVolumes : row.ignoredChapters) || [];
    const wanted = Array.isArray(chapters) ? chapters : offered;
    const keep = new Set(wanted);
    const excluding = [
      ...offered.filter((c) => !keep.has(c)),
      ...crossed,
    ];
    const rangeStr = chaptersToRangeString(wanted, { excluding });
    // Same "library" recipe as the detail-view "Download Missing Chapters"
    // path; its args are built when the download starts, from the Settings
    // saved then (lib/queueRecipe.js).
    const recipe = libraryRecipe(
      meta,
      rangeStr,
      // The row's folder, which for a FORKED series is the member holding the
      // bulk of it (main.js picks it; grep seriesIdentityKey). Passing it as
      // --series-dir is what stops the delta landing in the husk instead.
      row.folderPath || entry?.folderPath,
      kind,
    );
    return { url: meta.url, args: { recipe } };
  }, []);

  // Volume downloads are refused under Komikku (lib/downloadArgs.js
  // KOMIKKU_VOLUMES_MESSAGE); the panel shows why and never hands volumes
  // over then, and this guards the same rule for the queue paths below.
  const komikkuOn = settings?.defaults?.komikku === true;

  // `volumes` (optional): the row's ticked VOLUMES, queued as a separate
  // volume download after the chapter one. A row can carry both kinds.
  const handleQueueRow = useCallback((row, chapters, volumes) => {
    const entry = entries.find((e) => e.folderPath === row.folderPath);
    if (!entry?.seriesMeta?.url) return;
    const wanted = Array.isArray(chapters) ? chapters : row.newChapters || [];
    const wantedVolumes = komikkuOn || !Array.isArray(volumes) ? [] : volumes;
    if (wanted.length === 0 && wantedVolumes.length === 0) return;
    if (wanted.length > 0) {
      const { url, args } = buildDownloadArgsForRow(row, entry, wanted);
      onStartDownload(url, args);
    }
    if (wantedVolumes.length > 0) {
      const { url, args } = buildDownloadArgsForRow(row, entry, wantedVolumes, "volume");
      onStartDownload(url, args);
    }
    // The user committed, so drop the badge and downgrade the row to
    // "uptodate" — it stays visible in the panel as feedback rather than
    // vanishing. If a later scan finds more, the count repopulates.
    resolveUpdateRows([row.folderPath]);
  }, [entries, buildDownloadArgsForRow, onStartDownload, resolveUpdateRows, komikkuOn]);

  // `plan` is [{ row, chapters, volumes? }] built by the panel, which owns the ticks —
  // this no longer re-derives the found set from seriesStates, because that
  // would silently queue every new chapter of a row the user had partly
  // unticked.
  const handleQueueAll = useCallback((plan) => {
    const volsOf = (item) => (komikkuOn ? [] : item.volumes || []);
    const items = (Array.isArray(plan) ? plan : []).filter(
      (item) => item?.row && ((item.chapters?.length || 0) + volsOf(item).length) > 0
    );
    for (const item of items) {
      const { row, chapters } = item;
      const entry = entries.find((e) => e.folderPath === row.folderPath);
      if (!entry?.seriesMeta?.url) continue;
      if (chapters?.length > 0) {
        const { url, args } = buildDownloadArgsForRow(row, entry, chapters);
        onStartDownload(url, args);
      }
      if (volsOf(item).length > 0) {
        const { url, args } = buildDownloadArgsForRow(row, entry, volsOf(item), "volume");
        onStartDownload(url, args);
      }
    }
    // Bulk-clear all queued badges + downgrade rows to up-to-date.
    resolveUpdateRows(items.map(({ row }) => row.folderPath));
    onSwitchTab("queue");
    setUpdatesPanelOpen(false);
  }, [entries, buildDownloadArgsForRow, onStartDownload, onSwitchTab, resolveUpdateRows, komikkuOn]);

  // Clear badges for the given folderPaths without queueing anything.
  // Used by the "Dismiss" buttons (per-row + bulk). The row itself stays in
  // the panel as "uptodate" so the user can see the dismiss took effect.
  const handleDismiss = resolveUpdateRows;

  const handleRefresh = useCallback(() => {
    setSelectedEntry(null);
    loadLibrary();
  }, [loadLibrary]);

  // ── Filter + Sort ──
  // Pre-compute lowercased titles ONCE per entries-list change. Without
  // this, every keystroke recomputes `e.title.toLowerCase()` for every
  // entry — at 200 entries × 8 keystrokes/sec that's 1600 case
  // conversions/sec just to filter on a substring search.
  const entriesIndexed = useMemo(
    () => entries.map((e) => ({ entry: e, lowerTitle: (e.title || "").toLowerCase() })),
    [entries]
  );

  // Per-entry facet Sets + the library-wide facet vocabulary. Same reasoning as
  // entriesIndexed, one level up: without it every filter evaluation would
  // re-walk each series' genre/tag arrays and re-lowercase every string.
  //
  // `perEntry` is INDEX-PARALLEL to entriesIndexed — both are a plain
  // `entries.map`, and `filtered` below zips them by index. Keep BOTH memos
  // keyed on [entries] alone or that invariant breaks.
  //
  // Counts are library-wide, NOT contextual (they don't shrink as you select
  // other facets). Contextual counts would mean rebuilding the whole index on
  // every click and every number jumping as you go — worse to use, and it
  // would tie this memo to `filters`.
  const facetIndex = useMemo(() => {
    const groups = {
      genres: new Map(),
      tags: new Map(),
      status: new Map(),
      sites: new Map(),
    };

    // `seen` is the entry's own key Set: a series listing "Action" twice must
    // count once. Casing tallies ride the meta so the label we display is the
    // library's most common spelling, not whichever series scanned first.
    const bump = (groupKey, rawValue, seen, extra) => {
      const raw = String(rawValue ?? "").trim();
      if (!raw) return;
      const key = raw.toLowerCase();
      const map = groups[groupKey];
      let meta = map.get(key);
      if (!meta) {
        meta = { label: raw, count: 0, category: "", spoiler: false, casings: new Map() };
        map.set(key, meta);
      }
      if (extra) {
        // First non-empty category wins; spoiler is sticky — a tag flagged on
        // any one series stays behind the spoiler switch everywhere.
        if (extra.category && !meta.category) meta.category = extra.category;
        if (extra.spoiler) meta.spoiler = true;
      }
      meta.casings.set(raw, (meta.casings.get(raw) || 0) + 1);
      if (seen.has(key)) return;
      seen.add(key);
      meta.count += 1;
    };

    const perEntry = entries.map((e) => {
      const meta = e.seriesMeta || {};
      const facets = {
        genres: new Set(),
        tags: new Set(),
        status: new Set(),
        sites: new Set(),
      };
      for (const g of meta.genres || []) bump("genres", g, facets.genres);
      // anilist_tags / anilist_spoiler_tags are arrays of OBJECTS, and the
      // split is already spoiler-vs-not on the Python side
      // (external_metadata.py:_split_tags) — the per-tag flags are re-checked
      // anyway so a hand-edited or older file can't leak a spoiler.
      const readTags = (list, fromSpoilerBucket) => {
        for (const t of Array.isArray(list) ? list : []) {
          if (!t || typeof t !== "object") continue;
          bump("tags", t.name, facets.tags, {
            category: String(t.category || "").trim(),
            spoiler:
              fromSpoilerBucket ||
              t.is_media_spoiler === true ||
              t.is_general_spoiler === true,
          });
        }
      };
      readTags(meta.anilist_tags, false);
      readTags(meta.anilist_spoiler_tags, true);
      bump("status", meta.status, facets.status);
      bump("sites", meta.site, facets.sites);
      return facets;
    });

    // Resolve display labels + the precomputed lowercase haystack the panel's
    // filter-the-filters input tests against (so a keystroke doesn't
    // re-lowercase every facet label in the library).
    for (const map of Object.values(groups)) {
      for (const meta of map.values()) {
        let best = meta.label;
        let bestN = -1;
        for (const [text, n] of meta.casings) {
          if (n > bestN || (n === bestN && text < best)) {
            best = text;
            bestN = n;
          }
        }
        meta.label = best;
        delete meta.casings;
        meta.search = (meta.category ? `${best} ${meta.category}` : best).toLowerCase();
      }
    }

    return { perEntry, groups };
  }, [entries]);

  const activeFilterCount = countActiveFilters(filters);

  const lowerQuery = useMemo(() => searchQuery.toLowerCase(), [searchQuery]);
  const filtered = useMemo(
    () => {
      if (!lowerQuery && activeFilterCount === 0) return entries;
      const out = [];
      for (let i = 0; i < entriesIndexed.length; i++) {
        if (lowerQuery && !entriesIndexed[i].lowerTitle.includes(lowerQuery)) continue;
        if (activeFilterCount > 0 && !matchesFacets(facetIndex.perEntry[i], filters)) continue;
        out.push(entriesIndexed[i].entry);
      }
      return out;
    },
    [entriesIndexed, facetIndex, filters, activeFilterCount, lowerQuery, entries]
  );

  const sorted = useMemo(() => {
    const copy = [...filtered];
    copy.sort((a, b) => {
      switch (sortBy) {
        // naturalCompare (@/lib/utils) is numeric-aware: "Vol 2" before
        // "Vol 10", not after "Vol 100".
        case "title": return naturalCompare(a.title, b.title);
        case "title-desc": return naturalCompare(b.title, a.title);
        // DELIBERATELY plain localeCompare: these are ISO-8601 strings, which
        // already sort correctly lexicographically. numeric:true would parse
        // the digit groups across the '-' and ':' separators and break them.
        case "date": return (b.lastModified || "").localeCompare(a.lastModified || "");
        case "date-asc": return (a.lastModified || "").localeCompare(b.lastModified || "");
        case "size": return b.totalSize - a.totalSize;
        case "size-asc": return a.totalSize - b.totalSize;
        default: return 0;
      }
    });
    return copy;
  }, [filtered, sortBy]);

  // Flatten the active selection into removable chips, in FACET_GROUPS order.
  // A key that no longer exists in the library (series deleted, or a filter
  // persisted from a previous library) still gets a chip — falling back to the
  // raw key — so it stays removable instead of silently matching nothing.
  const activeFilterChips = useMemo(() => {
    const chips = [];
    for (const group of FACET_GROUPS) {
      const map = facetIndex.groups[group.key];
      for (const key of filters[group.key]) {
        chips.push({ group, key, label: map?.get(key)?.label || key });
      }
    }
    return chips;
  }, [filters, facetIndex]);

  // Count how many series are eligible for "Check All".
  // Must mirror main.js's checkable filter — see the comment there. When
  // settings.checkAllIncludeCompleted is on (default), Completed/Finished
  // series count too because aggregators (notably mangafire) lie about
  // status. If the user has opted out via Settings, restore the legacy
  // ongoing-only filter.
  //
  // Counted per SERIES, not per folder: main.js runs one check per identity
  // group, so a library holding a forked series (two folders, one series;
  // grep seriesIdentityKey) would otherwise advertise a bigger number than the
  // sweep ever reports back. seriesKey rides on the entry from
  // electron/library.js's scanLibrary; folderPath is the fallback for a folder
  // with no identity, which is exactly how main.js buckets those too.
  const includeCompletedInCheck = settings?.checkAllIncludeCompleted !== false;
  const ongoingCount = useMemo(() => {
    const keys = new Set();
    for (const e of entries) {
      if (!e.seriesMeta?.url) continue;
      if (!includeCompletedInCheck) {
        const s = e.seriesMeta.status;
        if (!(!s || s === "Ongoing" || s === "Releasing")) continue;
      }
      keys.add(e.seriesKey || e.folderPath);
    }
    return keys.size;
  }, [entries, includeCompletedInCheck]);

  // ── Toolbar button mode ──
  //   "scanning" — a sweep is live. It runs in the MAIN process, so this
  //                mode is reached on a remount too: leaving the Library tab
  //                never stopped the scan, it only used to hide it.
  //   "results"  — a finished sweep worth reopening (including a clean one:
  //                "everything was up to date" is a result).
  //   "fresh"    — nothing to show; the click starts the first scan.
  // Cancelled-with-nothing-found falls back to "fresh" because there is no
  // result to preserve — anything else would make the user open the panel
  // just to reach Rescan. Every other post-scan click OPENS rather than
  // re-scans; Rescan inside the panel is the one way to spend the work again.
  const updatesButtonMode =
    scanState === "running"
      ? "scanning"
      : scanState === "done" && (updatesFoundCount > 0 || !scanStats.aborted)
      ? "results"
      : "fresh";

  // ── Detail view ──
  if (selectedEntry) {
    const current = entries.find((e) => e.folderPath === selectedEntry.folderPath) || selectedEntry;
    return (
      <DetailView
        entry={current}
        onBack={() => setSelectedEntry(null)}
        onRefresh={handleRefresh}
        onStartDownload={onStartDownload}
        onSwitchTab={onSwitchTab}
        settings={settings}
        onFilterByFacet={(groupKey, valueKey) => {
          addFacet(groupKey, valueKey);
          setSelectedEntry(null);
        }}
        onSetChaptersIgnored={setRowIgnored}
        syncLibrary={syncLibrary}
        onOpenInSync={onOpenInSync}
      />
    );
  }

  // ── GRID VIEW ──
  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b bg-card/20">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <Input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search manga…"
            className="pl-8 h-8 text-xs"
          />
        </div>

        <div className="flex items-center gap-1.5">
          <ArrowUpDown className="w-3.5 h-3.5 text-muted-foreground" />
          <select
            value={sortBy}
            onChange={(e) => updateSort(e.target.value)}
            className={cn(
              "text-xs bg-transparent border border-border rounded-md px-2 py-1.5",
              "text-foreground cursor-pointer",
              "focus:outline-none focus:ring-1 focus:ring-primary"
            )}
          >
            {SORT_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>

        {/* Faceted filter dropdown. This wrapper is `relative` so the panel
            positions under the button, AND it is the click-outside boundary —
            LibraryFilterPanel tests containment against this same ref, which is
            why the panel must stay a child of it (see that file's header). */}
        <div className="relative" ref={filterAnchorRef}>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setFilterPanelOpen((v) => !v)}
            disabled={entries.length === 0}
            aria-expanded={filterPanelOpen}
            className={cn(
              "gap-1.5 text-xs",
              activeFilterCount > 0 && [
                "border-primary/50 bg-primary/10 text-primary",
                "hover:bg-primary/15 hover:text-primary hover:border-primary/60",
              ]
            )}
            title={
              activeFilterCount > 0
                ? `${activeFilterCount} filter${activeFilterCount !== 1 ? "s" : ""} active`
                : "Filter by genre, AniList tag, status or source"
            }
          >
            <Filter className="w-3.5 h-3.5" />
            Filter
            {activeFilterCount > 0 && (
              <span className="text-[10px] font-mono tabular-nums px-1 rounded-sm bg-primary/20">
                {activeFilterCount}
              </span>
            )}
          </Button>

          {filterPanelOpen && (
            <LibraryFilterPanel
              anchorRef={filterAnchorRef}
              onClose={closeFilterPanel}
              groups={facetIndex.groups}
              filters={filters}
              activeCount={activeFilterCount}
              matchedCount={sorted.length}
              totalCount={entries.length}
              onToggleFacet={toggleFacet}
              onSetMatchMode={setMatchMode}
              onSetShowSpoilerTags={setShowSpoilerTags}
              onClearAll={clearFilters}
            />
          )}
        </div>

        {/* Updates Center button — see updatesButtonMode above for the
            state table. Four labels:
              - Scanning N/M  (sweep live, here or started in a hidden tab)
              - Updates ●N    (finished with N found, orange accent)
              - Up to date    (finished clean)
              - Check All     (nothing to show — the only mode that scans)
            Only "fresh" starts a sweep. That used to be the ONLY behavior
            available after a tab switch, because the tab remounted with an
            empty scanState while the sweep was still running — clicking then
            aborted the live sweep and restarted it from zero.
            Cross-file: UpdatesCenter.jsx is the panel; hooks/useUpdateCheck.js
            owns the state; electron/main.js owns the sweep. */}
        {ongoingCount > 0 && (
          <Button
            variant="outline"
            size="sm"
            onClick={updatesButtonMode === "fresh" ? handleCheckAll : handleOpenPanel}
            disabled={loading}
            className={cn(
              "gap-1.5 text-xs relative",
              updatesButtonMode === "results" && updatesFoundCount > 0 && [
                "border-orange-500/50 text-orange-300",
                "hover:bg-orange-500/10 hover:text-orange-200 hover:border-orange-500/60",
              ]
            )}
            title={
              updatesButtonMode === "scanning"
                ? `Scanning ${scanStats.completed} of ${scanStats.total}… (runs in the background — click to watch)`
                : updatesButtonMode === "fresh"
                ? `Check ${ongoingCount} ongoing series for new chapters`
                : updatesFoundCount > 0
                ? `${updatesFoundCount} series have new chapters — click to view`
                : "Last check found nothing new — click to view, then Rescan for a fresh check"
            }
          >
            {updatesButtonMode === "scanning" ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : updatesButtonMode === "results" && updatesFoundCount === 0 ? (
              <Check className="w-3.5 h-3.5" />
            ) : (
              <Bell className="w-3.5 h-3.5" />
            )}
            {updatesButtonMode === "scanning"
              ? `Scanning ${scanStats.completed}/${scanStats.total}`
              : updatesButtonMode === "fresh"
              ? "Check All"
              : updatesFoundCount > 0
              ? `Updates ${updatesFoundCount}`
              : "Up to date"}
            {updatesButtonMode === "results" && updatesFoundCount > 0 && (
              <span
                aria-hidden
                className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-orange-400 shadow-[0_0_6px_rgba(249,115,22,0.8)]"
              />
            )}
          </Button>
        )}

        <Button
          variant="ghost"
          size="sm"
          onClick={handleRefresh}
          disabled={loading}
          className="gap-1.5 text-xs"
          title="Refresh library"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
        </Button>

        {/* Reflects the FILTERED count; hints at the library total whenever a
            filter or search is narrowing it. */}
        <Badge variant="secondary" className="text-[10px] ml-auto tabular-nums">
          {loading
            ? "…"
            : sorted.length === entries.length
            ? `${sorted.length} manga${sorted.length !== 1 ? "s" : ""}`
            : `${sorted.length} of ${entries.length}`}
        </Badge>
      </div>

      {/* ── Active filter chips ──
          Keeps the current filter legible without opening the panel. Chip
          markup follows SettingsTab's disabled-search-sources list; the group
          icon carries the accent so the neutral chip body doesn't turn the row
          into a wall of blue. Staggered reveal matches SearchTab's rows. */}
      {activeFilterChips.length > 0 && (
        <div className="flex items-center flex-wrap gap-1.5 px-4 py-2 border-b bg-card/10">
          {activeFilterChips.map((chip, i) => {
            const Icon = chip.group.icon;
            return (
              <span
                key={`${chip.group.key}:${chip.key}`}
                className="inline-flex items-center gap-1.5 rounded-full border border-border bg-secondary/60 pl-2 pr-1 py-0.5 animate-slide-up"
                style={{ animationDelay: `${Math.min(i * 40, 240)}ms` }}
              >
                <Icon className={cn("w-3 h-3 shrink-0", chip.group.accent)} />
                <span className="text-[11px] leading-none">{chip.label}</span>
                <button
                  type="button"
                  onClick={() => toggleFacet(chip.group.key, chip.key)}
                  aria-label={`Remove ${chip.group.label} filter ${chip.label}`}
                  title="Remove filter"
                  className="ml-0.5 p-0.5 rounded-full text-muted-foreground hover:text-foreground hover:bg-background transition-colors"
                >
                  <X className="w-3 h-3" />
                </button>
              </span>
            );
          })}
          {/* "All" changes what the selection MEANS, and it's otherwise only
              visible inside the panel. */}
          {filters.matchMode === "all" && (
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
              match all
            </span>
          )}
          <button
            type="button"
            onClick={clearFilters}
            className="ml-1 inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
          >
            <FilterX className="w-3 h-3" />
            Clear
          </button>
        </div>
      )}

      {/* Grid / Empty */}
      <div className="flex-1 overflow-y-auto">
        {!loading && sorted.length === 0 ? (
          // Order matters: an empty library must never offer "clear filters"
          // (a filter can outlive the series it was applied to, since it's
          // persisted in settings.libraryOpts).
          entries.length === 0 ? (
            <EmptyState />
          ) : activeFilterCount > 0 ? (
            <FilteredEmptyState searchQuery={searchQuery} onClearFilters={clearFilters} />
          ) : searchQuery ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <Search className="w-8 h-8 text-muted-foreground/30 mb-3" />
              <p className="text-xs text-muted-foreground">
                No manga matching &ldquo;{searchQuery}&rdquo;
              </p>
            </div>
          ) : (
            <EmptyState />
          )
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3 p-4">
            {sorted.map((entry) => (
              <MangaCard
                key={entry.folderPath}
                entry={entry}
                newCount={newChapterCounts[entry.folderPath] || 0}
                onClick={() => setSelectedEntry(entry)}
                onShowDuplicate={setDuplicateEntry}
                syncChip={syncLibrary ? seriesSyncChip(syncLibrary.bySeries[entry.folderPath], syncLibrary.targets) : null}
              />
            ))}

            {loading && entries.length === 0 &&
              Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="rounded-lg overflow-hidden border border-border/30">
                  <div className="aspect-[3/4] bg-muted/30 animate-pulse" />
                  <div className="p-2.5 space-y-2">
                    <div className="h-3 bg-muted/40 rounded animate-pulse w-3/4" />
                    <div className="h-2 bg-muted/30 rounded animate-pulse w-1/2" />
                  </div>
                </div>
              ))
            }
          </div>
        )}
      </div>

      {/* ── Updates Center side-sheet ──
          Rendered last so it overlays the grid + toolbar. */}
      <UpdatesCenter
        open={updatesPanelOpen}
        onClose={() => setUpdatesPanelOpen(false)}
        seriesStates={seriesStates}
        scanState={scanState}
        scanStats={scanStats}
        onRescan={handleRescan}
        onCancel={cancelUpdateCheck}
        onQueueRow={handleQueueRow}
        onQueueAll={handleQueueAll}
        onDismiss={handleDismiss}
        onSetChaptersIgnored={setRowIgnored}
        hasCheckableSeries={ongoingCount > 0}
        komikku={komikkuOn}
      />

      {/* ── Merge duplicate folders ──
          Opened from a card's amber badge. Rescans on success because a merge
          moves files between folders and removes one — every entry the grid
          holds for this series is stale afterwards. */}
      {duplicateEntry && (
        <MergeDuplicatesDialog
          entry={duplicateEntry}
          onClose={() => setDuplicateEntry(null)}
          onMerged={loadLibrary}
        />
      )}
    </div>
  );
}
