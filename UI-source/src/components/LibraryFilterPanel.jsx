// ============================================================
// LIBRARY FILTER PANEL — anchored faceted-filter dropdown
//
// Pure presentation for LibraryTab's facet filters. LibraryTab owns the
// canonical filter state AND the facetIndex memo (built from the scanned
// entries); this file renders chips and calls back. The only state here is
// the local "filter the filters" query and the per-group expand toggle.
//
// There is no Popover primitive in ui/primitives.jsx, so the dropdown is
// hand-rolled: absolutely positioned inside an anchor wrapper that LibraryTab
// owns, dismissed on Escape + mousedown outside that wrapper. The wrapper
// CONTAINS this panel, so one containment test covers both "clicked inside
// the panel" and "clicked the toggle button" — testing the button separately
// would close on mousedown and instantly reopen on the button's own click.
//
// Entrance uses `animate-slide-up` (a keyframe, so it plays on mount).
// UpdatesCenter's rAF two-phase mount is only needed there because it
// animates a CSS *transition*, which needs its from-state committed first.
//
// Cross-file:
//   - LibraryTab.jsx builds `groups` / `filters` and owns persistence
//     (settings.libraryOpts.filters) — grep facetIndex / applyFilters
//   - facet values originate in .aio_series.json (aio-dl.py writes it;
//     electron/library.js passes it through unfiltered as entry.seriesMeta)
// ============================================================

import React, { useEffect, useMemo, useState } from "react";
import { Search, X, Tag, Globe, CircleDot, Sparkles, FilterX } from "lucide-react";
import { Input, Switch } from "@/components/ui/primitives";
import { cn, naturalCompare } from "@/lib/utils";

// ── Facet group → presentational metadata + filter semantics ──
// Single source of truth. EXPORTED because LibraryTab's active-filter chip row
// renders the same icons/accents and its matcher reads `singleValued` — a
// second literal in that file would drift. Same arrangement as
// UpdatesCenter.jsx:SECTION_THEME / LibraryTab's STATUS_COLORS.
//
// `key` is simultaneously the key of the facetIndex `groups` object and of the
// filter-state object, so the whole panel is a map over this array.
//
// `singleValued` = a series carries at most ONE value for this facet, so
// "match all" with two of them selected could never match anything; the
// matcher forces OR there (see LibraryTab:matchesFacets).
//
// Accents reuse colors the app already ships (emerald in STATUS_COLORS, sky in
// SECTION_THEME.active, violet in FORMAT_COLORS.images) — not a new palette.
// Only the section icon+label is tinted; selected chips are uniformly
// `primary` so "selected" reads identically in every group.
export const FACET_GROUPS = [
  { key: "status", label: "Status", icon: CircleDot, accent: "text-emerald-400", singleValued: true },
  { key: "sites", label: "Source", icon: Globe, accent: "text-sky-400", singleValued: true },
  { key: "genres", label: "Genres", icon: Tag, accent: "text-primary", singleValued: false },
  { key: "tags", label: "AniList tags", icon: Sparkles, accent: "text-violet-400", singleValued: false },
];

export const FACET_GROUP_BY_KEY = Object.fromEntries(
  FACET_GROUPS.map((g) => [g.key, g])
);

const MATCH_MODES = [
  { value: "any", label: "Any", hint: "Keep series carrying ANY selected genre/tag" },
  { value: "all", label: "All", hint: "Keep only series carrying EVERY selected genre/tag" },
];

// Chips past this count collapse behind "Show N more" so a 60-genre library
// doesn't turn the dropdown into a wall. Selected chips are always rendered
// regardless of position (see visibleItems) — a chip you just picked must not
// be able to disappear.
const CHIP_COLLAPSE_LIMIT = 24;

function FacetChip({ label, count, selected, onClick, title }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-pressed={selected}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1",
        "text-[11px] leading-none transition-colors",
        selected
          ? "border-primary bg-primary/10 text-primary"
          : "border-border hover:border-primary/40 hover:bg-accent/40"
      )}
    >
      <span className="truncate max-w-[150px]">{label}</span>
      <span
        className={cn(
          "text-[10px] tabular-nums",
          selected ? "text-primary/70" : "text-muted-foreground"
        )}
      >
        {count}
      </span>
    </button>
  );
}

// Same anatomy as ui/primitives.jsx:SectionHeader (uppercase tracked label +
// flex-1 hairline rule), tightened for dropdown density and carrying the
// group accent + a vocabulary-size readout.
function FacetSectionHeader({ group, total }) {
  const Icon = group.icon;
  return (
    <div className="flex items-center gap-2 mb-1.5">
      <Icon className={cn("w-3 h-3 shrink-0", group.accent)} />
      <span className={cn("text-[10px] font-semibold uppercase tracking-wider", group.accent)}>
        {group.label}
      </span>
      <div className="flex-1 h-px bg-border" />
      <span className="text-[10px] text-muted-foreground tabular-nums">{total}</span>
    </div>
  );
}

export default function LibraryFilterPanel({
  // The `relative` wrapper this panel is rendered inside; doubles as the
  // click-outside boundary.
  anchorRef,
  onClose,
  // { genres|tags|status|sites: Map<lowercaseKey, {label,count,category,spoiler,search}> }
  groups,
  // { genres:Set, tags:Set, status:Set, sites:Set, matchMode, showSpoilerTags }
  filters,
  activeCount,
  matchedCount,
  totalCount,
  onToggleFacet,
  onSetMatchMode,
  onSetShowSpoilerTags,
  onClearAll,
}) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(() => new Set());

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    const onDown = (e) => {
      if (!anchorRef?.current?.contains(e.target)) onClose();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [anchorRef, onClose]);

  const lowerQuery = query.trim().toLowerCase();

  // Sort is count-desc then label — deliberately INDEPENDENT of selection, so
  // clicking a chip never reorders the row and moves the next chip under the
  // cursor.
  const sections = useMemo(() => {
    const out = [];
    for (const group of FACET_GROUPS) {
      const map = groups?.[group.key];
      if (!map || map.size === 0) continue;
      const selected = filters[group.key];
      const items = [];
      for (const [key, meta] of map) {
        // Spoiler-flagged tags stay hidden unless the switch is on — EXCEPT
        // ones already selected, which must stay deselectable from here.
        if (
          group.key === "tags" &&
          meta.spoiler &&
          !filters.showSpoilerTags &&
          !selected.has(key)
        ) {
          continue;
        }
        if (lowerQuery && !meta.search.includes(lowerQuery)) continue;
        items.push({ key, ...meta, selected: selected.has(key) });
      }
      if (items.length === 0) continue;
      items.sort((a, b) => b.count - a.count || naturalCompare(a.label, b.label));
      out.push({ group, items, total: map.size });
    }
    return out;
  }, [groups, filters, lowerQuery]);

  const visibleItems = (groupKey, items) => {
    if (lowerQuery || expanded.has(groupKey) || items.length <= CHIP_COLLAPSE_LIMIT) {
      return items;
    }
    return items.filter((it, i) => i < CHIP_COLLAPSE_LIMIT || it.selected);
  };

  // AniList tag objects carry a `category` ("Theme", "Demographic", …) —
  // written by aio-dl.py:_serialize_anilist_tag, grep anilist_tags. Sub-group
  // by it so 80 tags read as a taxonomy instead of a blob.
  const byCategory = (items) => {
    const map = new Map();
    for (const it of items) {
      const cat = it.category || "Uncategorized";
      if (!map.has(cat)) map.set(cat, []);
      map.get(cat).push(it);
    }
    return [...map.entries()].sort((a, b) => naturalCompare(a[0], b[0]));
  };

  return (
    <div
      role="dialog"
      aria-label="Library filters"
      className={cn(
        "absolute right-0 top-full mt-1.5 z-40 w-[380px]",
        "flex flex-col max-h-[70vh] overflow-hidden",
        "rounded-lg border border-border bg-card text-card-foreground",
        "shadow-[0_16px_40px_-12px_rgba(0,0,0,0.45)]",
        "animate-slide-up"
      )}
    >
      {/* ── Header: title + match-mode segmented toggle ── */}
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Filters
        </span>
        {activeCount > 0 && (
          <span className="text-[10px] font-mono tabular-nums px-1.5 py-0.5 rounded-full border border-primary/30 bg-primary/10 text-primary leading-none">
            {activeCount}
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Match
          </span>
          <div
            role="group"
            aria-label="Match mode"
            className="inline-flex rounded-md border border-border overflow-hidden"
          >
            {MATCH_MODES.map((m, i) => (
              <button
                key={m.value}
                type="button"
                onClick={() => onSetMatchMode(m.value)}
                title={m.hint}
                aria-pressed={filters.matchMode === m.value}
                className={cn(
                  "px-2 py-1 text-[10px] font-semibold uppercase tracking-wide transition-colors",
                  i > 0 && "border-l border-border",
                  filters.matchMode === m.value
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:bg-accent/40"
                )}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* ── Filter-the-filters ── */}
      <div className="px-3 pb-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter genres, tags, sources…"
            className="pl-8 pr-7 h-8 text-xs"
            autoFocus
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery("")}
              aria-label="Clear"
              className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded-full text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
            >
              <X className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>

      {/* ── Facet groups ── */}
      <div className="flex-1 min-h-0 overflow-y-auto px-3 pb-3 space-y-3.5">
        {sections.length === 0 ? (
          <p className="text-[11px] text-muted-foreground text-center py-8 leading-relaxed">
            {lowerQuery
              ? <>Nothing matching &ldquo;{query}&rdquo;</>
              : "No genres, tags, status or source recorded for this library yet."}
          </p>
        ) : (
          sections.map(({ group, items, total }) => {
            const shown = visibleItems(group.key, items);
            const hidden = items.length - shown.length;
            const chip = (it) => (
              <FacetChip
                key={it.key}
                label={it.label}
                count={it.count}
                selected={it.selected}
                title={it.category ? `${it.label} · ${it.category}` : it.label}
                onClick={() => onToggleFacet(group.key, it.key)}
              />
            );
            return (
              <div key={group.key}>
                <FacetSectionHeader group={group} total={total} />

                {group.key === "tags" && (
                  <div className="flex items-center gap-2 mb-2">
                    <Switch
                      id="lib-filter-spoiler-tags"
                      checked={filters.showSpoilerTags}
                      onCheckedChange={onSetShowSpoilerTags}
                    />
                    <label
                      htmlFor="lib-filter-spoiler-tags"
                      title="AniList flags these as media or general spoilers"
                      className="text-[11px] text-muted-foreground cursor-pointer select-none"
                    >
                      Show spoiler tags
                    </label>
                  </div>
                )}

                {group.key === "tags" ? (
                  <div className="space-y-2">
                    {byCategory(shown).map(([cat, catItems]) => (
                      <div key={cat}>
                        <div className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground/70 mb-1">
                          {cat}
                        </div>
                        <div className="flex flex-wrap gap-1.5">{catItems.map(chip)}</div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-1.5">{shown.map(chip)}</div>
                )}

                {hidden > 0 && (
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded((prev) => new Set(prev).add(group.key))
                    }
                    className="mt-1.5 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
                  >
                    Show {hidden} more
                  </button>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* ── Footer: live match count + clear ── */}
      <div className="flex items-center gap-2 px-3 py-2 border-t border-border bg-muted/30">
        <span className="text-[10px] text-muted-foreground tabular-nums">
          {matchedCount} of {totalCount} series
        </span>
        <button
          type="button"
          onClick={onClearAll}
          disabled={activeCount === 0}
          className={cn(
            "ml-auto inline-flex items-center gap-1.5 text-[11px] transition-colors",
            activeCount === 0
              ? "text-muted-foreground/40 cursor-default"
              : "text-muted-foreground hover:text-foreground"
          )}
        >
          <FilterX className="w-3 h-3" />
          Clear all
        </button>
      </div>
    </div>
  );
}
