// ============================================================
// SEARCH TAB
//
// Cross-site search over the seeded handler set. Wraps:
//   - search bar + "go" button
//   - inline filters (multi-source, seeded-only, language)
//   - advanced collapsible (timeout, parallelism, min-match)
//   - live stderr feed during search
//   - results: candidate list, each with source cards
//   - chapter coverage map (multi-source only)
//
// Result cards push downloads into the existing queue via the
// onStartDownload callback — same path the New tab uses, so the
// Queue tab handles them transparently.
// ============================================================

import React, { useState, useMemo, useEffect, useRef } from "react";
import {
  Button, Input, Label, Switch, Slider, Select, SectionHeader, Collapsible, Badge,
} from "@/components/ui/primitives";
import { Search, X, Loader2, Sparkles, Play, RotateCw, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import SearchSourceCard from "./SearchSourceCard";
import SearchChapterMap from "./SearchChapterMap";

// Detect HTTP/S URLs in the search query — when matched, we cross-reference
// against the resumable list (tmp_* folders) so the user can pick up a
// partial download from where they left off without running a full search.
// Matches anything starting with http(s)://, no further validation; the CLI
// will give a clear error if the URL doesn't resolve to a known handler.
const URL_RE = /^https?:\/\//i;

// Normalize URLs for matching: strip trailing slash, lowercase scheme+host,
// drop query string + fragment. Helps when the user pastes a URL with
// slightly different formatting than what was saved in run_meta.json
// (trailing slash being the most common drift).
function normalizeUrl(u) {
  if (!u) return "";
  try {
    const p = new URL(u);
    return `${p.protocol}//${p.host.toLowerCase()}${p.pathname.replace(/\/+$/, "")}`;
  } catch {
    return u.replace(/\/+$/, "").toLowerCase();
  }
}

// Reuse the language list shape from DownloadTab so the user sees the same
// labels (changing one place would normally drift; the cost of duplicating
// is one row when a new language is added — kept inline because it's tiny).
const LANGUAGES = [
  { value: "en", label: "English" },
  { value: "ja", label: "Japanese" },
  { value: "ko", label: "Korean" },
  { value: "zh", label: "Chinese" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "pt-br", label: "Portuguese (BR)" },
  { value: "de", label: "German" },
  { value: "it", label: "Italian" },
  { value: "ru", label: "Russian" },
  { value: "ar", label: "Arabic" },
  { value: "tr", label: "Turkish" },
];

// Defaults match aio-dl.py argparse defaults.
//
// `collapseSplits` lived here historically (Phase 3) but moved to top-level
// settings.collapseSplits in 2026-05-08 because the same toggle now affects
// download behavior, not just search-display diagnostics. SearchTab's inline
// toggle below reads/writes settings.collapseSplits directly via
// onSaveSettings, and useDownloader.runSearch injects it into opts before IPC.
const DEFAULT_OPTS = {
  searchLanguage: "en",
  seededOnly: true,         // user-facing default ON: faster, cleaner results
  multiSource: false,       // user opts in for the chapter-fallback feature
  multiSourceQualityMin: 0.65,
  searchTimeout: 20,
  searchMinMatch: 0.55,
  searchParallelism: 6,
  // Off by default — torch + pyiqa + torchmetrics import on Windows can
  // stall on WMI queries (Python 3.13 platform.machine() path), which
  // historically hung search forever with no output. T2/T3 ML scoring
  // adds ~3-8% ranking accuracy on borderline matches; users who want
  // the boost can opt in via the Advanced toggle. See
  // sites/search_orchestrator.py:_ML_RATING_ENABLED for the full
  // rationale. Surfaces as --enable-ml-rating in searcher.js.
  enableMlRating: false,
};

export default function SearchTab({
  searchState,
  searchLogs,
  runSearch,
  cancelSearch,
  clearSearchLogs,
  onStartDownload,
  settings,
  onSaveSettings,
  resumable = [],
  onResumeDownload,
}) {
  const [query, setQuery] = useState("");
  // Lazy-initialize from persisted settings.searchOpts (saved by previous
  // sessions). Falls back to DEFAULT_OPTS for first run + any partial state.
  // Spread merge means we pick up new fields gracefully if DEFAULT_OPTS is
  // extended later without breaking older saved state.
  const [opts, setOpts] = useState(() => ({
    ...DEFAULT_OPTS,
    ...(settings?.searchOpts || {}),
  }));
  const inputRef = useRef(null);
  const logFeedRef = useRef(null);

  // When the parent's settings prop loads asynchronously from disk, sync
  // the form state once so user toggles persisted from the previous session
  // appear correctly. Only runs when settings.searchOpts changes (which is
  // typically once on app startup, after history.json reads).
  useEffect(() => {
    if (settings?.searchOpts) {
      setOpts((prev) => ({ ...DEFAULT_OPTS, ...prev, ...settings.searchOpts }));
    }
  }, [settings?.searchOpts]);

  // Wrap setOpts to also persist. Settings.json writes go through Electron
  // IPC and a temp+rename on disk — cheap individually, but rapid changes
  // (text inputs in advanced options, slider drags) used to hit one write
  // per character. Debounce so the user-perceived behavior is unchanged
  // (state updates immediately) while disk I/O coalesces to a single
  // write per ~350ms idle window. Switches and sliders trigger persist
  // through the same path; their input rate is naturally bounded so the
  // debounce doesn't materially delay them.
  //
  // pendingOptsRef holds the most recent opts pending a write so the
  // unmount cleanup can flush it synchronously — without this, a tab
  // switch mid-typing would drop the user's last few characters from
  // settings.json (debounced timer cleared but never fired).
  const persistTimerRef = useRef(null);
  const pendingOptsRef = useRef(null);
  const set = (key, value) =>
    setOpts((prev) => {
      const next = { ...prev, [key]: value };
      pendingOptsRef.current = next;
      if (persistTimerRef.current) clearTimeout(persistTimerRef.current);
      persistTimerRef.current = setTimeout(() => {
        onSaveSettings?.({ searchOpts: next });
        persistTimerRef.current = null;
        pendingOptsRef.current = null;
      }, 350);
      return next;
    });

  // Flush pending opts on unmount so a tab switch mid-typing doesn't
  // drop the user's last few characters from settings.json.
  useEffect(() => {
    return () => {
      if (persistTimerRef.current) {
        clearTimeout(persistTimerRef.current);
        persistTimerRef.current = null;
        if (pendingOptsRef.current) {
          onSaveSettings?.({ searchOpts: pendingOptsRef.current });
          pendingOptsRef.current = null;
        }
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const isRunning = searchState?.status === "running";
  const hasResults = searchState?.status === "done" && searchState.results;

  // Auto-focus on mount so users can type immediately when they switch tabs.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Auto-scroll the live log feed during search to keep the latest line
  // visible. Only scrolls while running — once results land, the log
  // moves out of focus and we don't fight the user.
  useEffect(() => {
    if (isRunning && logFeedRef.current) {
      logFeedRef.current.scrollTop = logFeedRef.current.scrollHeight;
    }
  }, [searchLogs.length, isRunning]);

  const handleSubmit = (e) => {
    e?.preventDefault();
    const q = query.trim();
    if (!q || isRunning) return;
    runSearch(q, opts);
  };

  const handleClear = () => {
    setQuery("");
    inputRef.current?.focus();
  };

  // Resume detection: if the user's query is a URL, check whether the
  // local tmp_<hid>/ folder cache has a partial download for that URL.
  // Surfaces a banner above the results (or instead of running search)
  // so the user can pick up where they left off — answers user feedback
  // 2026-05-07 "passed a link from MF into search, didn't pick up from
  // where it left off". Matches by normalized URL (strip trailing slash,
  // lowercase host) so tiny formatting drift doesn't miss the cache.
  const matchedResumable = useMemo(() => {
    const q = (query || "").trim();
    if (!q || !URL_RE.test(q) || !Array.isArray(resumable) || resumable.length === 0) {
      return null;
    }
    const target = normalizeUrl(q);
    return (
      resumable.find((r) => r.url && normalizeUrl(r.url) === target) || null
    );
  }, [query, resumable]);

  // For each source in each candidate, pre-compute whether THIS site has
  // any official-tagged chapters in the winner_chapter_map. Done once
  // per result render so the source card just receives a string|null.
  // Without this lookup, every card would walk the full chapter map
  // (O(chapters * sources) per render) — way too much work for 1200ch
  // series like One Piece.
  const officialBySite = useMemo(() => {
    const map = {};
    const cm = searchState?.results?.winner_chapter_map;
    if (!cm?.chapters) return map;
    for (const entry of cm.chapters) {
      for (const s of entry.sources || []) {
        if (s.is_official && !map[s.site]) {
          map[s.site] = s.publisher || s.group_name || "Official";
        }
      }
    }
    return map;
  }, [searchState?.results]);

  return (
    <div className="flex flex-col h-full">
      {/* Scrollable content area */}
      <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-4">
        {/* ── Search bar ── */}
        <form onSubmit={handleSubmit}>
          <Label htmlFor="search-query" className="text-sm font-semibold">
            Find a manga across {opts.seededOnly ? "30" : "280+"} sites
          </Label>
          <p className="text-xs text-muted-foreground mt-0.5 mb-2">
            Cross-site search ranks results by title match, measured chapter
            quality, and DMCA detection.
          </p>
          <div className="relative flex gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
              <Input
                ref={inputRef}
                id="search-query"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") handleClear();
                }}
                placeholder='e.g. "Frieren", "Witch Hat Atelier", "One Piece"'
                disabled={isRunning}
                className="pl-9 pr-9 h-10 text-sm"
              />
              {query && !isRunning && (
                <button
                  type="button"
                  onClick={handleClear}
                  aria-label="Clear search"
                  className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-muted-foreground hover:text-foreground rounded transition-colors"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
            {isRunning ? (
              <Button
                type="button"
                variant="destructive"
                onClick={cancelSearch}
                className="h-10 px-5 gap-1.5"
              >
                <X className="w-4 h-4" />
                Cancel
              </Button>
            ) : (
              <Button
                type="submit"
                disabled={!query.trim()}
                className="h-10 px-5 gap-1.5"
              >
                <Search className="w-4 h-4" />
                Search
              </Button>
            )}
          </div>
        </form>

        {/* ── Resume banner — only when query is a URL with cached progress ── */}
        {matchedResumable && (
          <div className="flex items-center justify-between gap-4 rounded-lg border border-primary/40 bg-primary/5 px-4 py-3 animate-slide-up">
            <div className="flex items-start gap-3 min-w-0">
              <RotateCw className="w-4 h-4 mt-0.5 text-primary shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold truncate">
                  Resume partial download
                </div>
                <p className="text-xs text-muted-foreground mt-0.5 truncate">
                  {matchedResumable.title || matchedResumable.hid} —{" "}
                  <span className="font-mono tabular-nums">
                    {matchedResumable.cachedChapters}
                  </span>{" "}
                  {matchedResumable.cachedChapters === 1 ? "chapter" : "chapters"}{" "}
                  already cached. Skip the search and pick up where you left off.
                </p>
              </div>
            </div>
            <Button
              size="sm"
              variant="default"
              className="gap-1.5 shrink-0"
              onClick={() => {
                // Pass format + epubLayout from the matched resumable so the
                // resumed run uses the original output format. Without these,
                // useDownloader.resumeDownload would forward `format: undefined`
                // through IPC, and downloader.js's resume() fallback would
                // default to PDF (since run_params.json deliberately omits
                // format — see aio-dl.py:get_behavior_params). matchedResumable
                // populates `.format` from run_meta.json which DOES carry the
                // original --format value.
                onResumeDownload?.({
                  url: matchedResumable.url,
                  tmpDir: matchedResumable.tmpDir,
                  format: matchedResumable.format,
                  epubLayout: matchedResumable.params?.epubLayout,
                });
              }}
            >
              <Play className="w-3.5 h-3.5" />
              Resume
            </Button>
          </div>
        )}

        {/* ── Inline filters ── */}
        <div className="grid grid-cols-2 gap-3 rounded-lg border bg-card/50 p-3">
          <div className="flex items-center justify-between gap-3">
            <Label htmlFor="opt-language" className="text-xs font-medium">
              Language
            </Label>
            <Select
              id="opt-language"
              value={opts.searchLanguage}
              onChange={(e) => set("searchLanguage", e.target.value)}
              disabled={isRunning}
              className="w-32 h-8 text-xs"
            >
              {LANGUAGES.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </Select>
          </div>

          <div className="flex items-center justify-between gap-3">
            <div>
              <Label htmlFor="opt-seeded" className="text-xs font-medium block">
                Curated sites only
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                ~3× faster, skips long-tail aggregators
              </p>
            </div>
            <Switch
              id="opt-seeded"
              checked={opts.seededOnly}
              onCheckedChange={(v) => set("seededOnly", v)}
              disabled={isRunning}
            />
          </div>

          <div className="flex items-center justify-between gap-3">
            <div>
              <Label htmlFor="opt-multi" className="text-xs font-medium block">
                Multi-source
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Per-chapter fallback when primary fails
              </p>
            </div>
            <Switch
              id="opt-multi"
              checked={opts.multiSource}
              onCheckedChange={(v) => set("multiSource", v)}
              disabled={isRunning}
            />
          </div>

          {/* Collapse split chapters — global setting (settings.collapseSplits),
              moved out of opts in 2026-05-08 because it now affects download
              behavior too. SettingsTab → "Default Chapter Behavior" mirrors
              this same toggle; the global state stays in sync regardless of
              which surface the user changes it from. Always visible (not
              gated on multi-source) since downloads are affected too. */}
          <div className="flex items-center justify-between gap-3">
            <div>
              <Label htmlFor="opt-collapse-splits" className="text-xs font-medium block">
                Collapse split chapters
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Merge X.1/X.2/X.3 splits; drop duplicate uploads. Affects downloads.
              </p>
            </div>
            <Switch
              id="opt-collapse-splits"
              checked={settings?.collapseSplits !== false}
              onCheckedChange={(v) => onSaveSettings?.({ collapseSplits: v })}
              disabled={isRunning}
            />
          </div>

          {/* Quality-min slider — only meaningful when multi-source is on,
              since it gates which sources qualify as per-chapter fallbacks. */}
          {opts.multiSource && (
            <div className="col-span-2 animate-slide-up">
              <div className="flex items-center justify-between gap-3 mb-1.5">
                <Label htmlFor="opt-quality-min" className="text-xs font-medium">
                  Alternative quality floor
                </Label>
                <span className="font-mono text-xs tabular-nums text-muted-foreground">
                  {opts.multiSourceQualityMin.toFixed(2)}
                </span>
              </div>
              <Slider
                id="opt-quality-min"
                min={0.3}
                max={0.95}
                step={0.05}
                value={opts.multiSourceQualityMin}
                onValueChange={(v) => set("multiSourceQualityMin", v)}
                disabled={isRunning}
              />
              <p className="text-[10px] text-muted-foreground mt-1">
                Sources below this seed/measured quality won't be used as fallbacks.
                Default 0.65 keeps unknown-language Madara extras out.
              </p>
            </div>
          )}
        </div>

        {/* ── Advanced (collapsed) ── */}
        <Collapsible title="Advanced search options">
          <div className="space-y-3 pt-2">
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="opt-timeout" className="text-xs font-medium">
                  Per-site timeout
                </Label>
                <p className="text-[10px] text-muted-foreground">
                  Slow sites self-select out after this. Default 20s.
                </p>
              </div>
              <Input
                id="opt-timeout"
                type="number"
                min={5}
                max={60}
                step={1}
                value={opts.searchTimeout}
                onChange={(e) => set("searchTimeout", parseInt(e.target.value, 10) || 20)}
                disabled={isRunning}
                className="w-20 h-8 text-xs"
              />
            </div>
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="opt-minmatch" className="text-xs font-medium">
                  Min title-match
                </Label>
                <p className="text-[10px] text-muted-foreground">
                  Drop hits below this similarity. Default 0.55.
                </p>
              </div>
              <Input
                id="opt-minmatch"
                type="number"
                min={0.3}
                max={1.0}
                step={0.05}
                value={opts.searchMinMatch}
                onChange={(e) => set("searchMinMatch", parseFloat(e.target.value) || 0.55)}
                disabled={isRunning}
                className="w-20 h-8 text-xs font-mono"
              />
            </div>
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="opt-parallel" className="text-xs font-medium">
                  Parallel sites
                </Label>
                <p className="text-[10px] text-muted-foreground">
                  How many sites to query at once. Default 6.
                </p>
              </div>
              <Input
                id="opt-parallel"
                type="number"
                min={1}
                max={16}
                step={1}
                value={opts.searchParallelism}
                onChange={(e) => set("searchParallelism", parseInt(e.target.value, 10) || 6)}
                disabled={isRunning}
                className="w-20 h-8 text-xs"
              />
            </div>

            {/* ML quality rating toggle. Off by default because torch's
                Windows import path can stall on degraded WMI services
                (Python 3.13 platform.machine() → WMI query) — historically
                hung search with no output. When on, T2 (CLIP-IQA, NIQE)
                and T3 (paired DISTS) ML scoring run on top of T1's
                pixel-level quality metrics for ~3-8% more accurate
                rankings on borderline matches. Cost: ~150 MB model
                weights on first download, ~2-5 s per source probe. */}
            <div className="flex items-start justify-between gap-3 pt-1 border-t border-border/40">
              <div className="flex-1 min-w-0">
                <Label htmlFor="opt-ml-rating" className="text-xs font-medium block">
                  ML quality rating
                </Label>
                <p className="text-[10px] text-muted-foreground mt-0.5 leading-relaxed">
                  GPU/CPU torch models (CLIP-IQA, NIQE, paired DISTS) refine
                  rankings on borderline matches. <strong>Off by default</strong> —
                  enable only if you've installed torch and don't see search
                  hangs. Downloads ~150 MB of model weights on first use.
                </p>
              </div>
              <Switch
                id="opt-ml-rating"
                checked={opts.enableMlRating}
                onCheckedChange={(v) => set("enableMlRating", v)}
                disabled={isRunning}
              />
            </div>
          </div>
        </Collapsible>

        {/* ── Live progress feed during search ── */}
        {(isRunning || (searchLogs.length > 0 && !hasResults)) && (
          <div className="rounded-lg border bg-card/50 overflow-hidden animate-slide-up">
            <div className="flex items-center justify-between px-3 py-1.5 border-b bg-muted/30">
              <div className="flex items-center gap-2">
                {isRunning && <Loader2 className="w-3.5 h-3.5 animate-spin text-primary" />}
                <span className="text-xs font-medium">
                  {isRunning ? "Searching…" : "Search log"}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-muted-foreground tabular-nums">
                  {searchLogs.length} lines
                </span>
                {/* Clear log feed. Disabled while a search is running so
                    the user doesn't accidentally wipe progress mid-flight
                    — the runSearch effect clears these on its own when a
                    new query starts, so this button is for explicitly
                    discarding the prior search's tail. */}
                {!isRunning && searchLogs.length > 0 && clearSearchLogs && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={clearSearchLogs}
                    className="h-5 px-1.5 text-[10px] gap-1"
                    title="Clear search log"
                  >
                    <Trash2 className="w-3 h-3" />
                    Clear
                  </Button>
                )}
              </div>
            </div>
            <div
              ref={logFeedRef}
              className="font-mono text-[10px] leading-relaxed px-3 py-1.5 max-h-32 overflow-y-auto bg-background"
            >
              {searchLogs
                .filter((entry) =>
                  // Verbose toggle (settings.verboseAlways) hides dimmed verbose
                  // lines from the panel — same rule as LogPanel.jsx so the user
                  // gets a consistent experience across both log surfaces.
                  settings?.verboseAlways !== false || entry.level !== "verbose"
                )
                .map((entry, i) => (
                  <div
                    key={i}
                    className={cn(
                      "whitespace-pre-wrap break-all",
                      entry.level === "error" && "text-red-500",
                      entry.level === "warning" && "text-yellow-500",
                      entry.level === "success" && "text-green-500",
                      entry.level === "info" && "text-muted-foreground",
                    )}
                  >
                    {entry.line}
                  </div>
                ))}
            </div>
          </div>
        )}

        {/* ── Error state ── */}
        {searchState?.status === "error" && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 animate-slide-up">
            <div className="text-sm font-medium text-destructive">Search failed</div>
            <p className="text-xs text-muted-foreground mt-0.5">{searchState.error}</p>
          </div>
        )}

        {/* ── Results ── */}
        {hasResults && (
          <SearchResults
            results={searchState.results}
            opts={searchState.opts}
            officialBySite={officialBySite}
            onQueue={onStartDownload}
          />
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────
// Results renderer — the JSON payload from `aio-dl.py --search`.
// Split out so SearchTab stays readable.
// ────────────────────────────────────────────────────────────
function SearchResults({ results, opts, officialBySite, onQueue }) {
  const candidates = results?.candidates || [];

  if (candidates.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center animate-slide-up">
        <Sparkles className="w-12 h-12 text-muted-foreground/40 mb-3" />
        <p className="text-sm font-medium">No results for "{results.query}"</p>
        <p className="text-xs text-muted-foreground mt-1 max-w-md">
          Try a shorter query, switch language, or turn off "Curated sites only"
          to widen the search to the long-tail aggregators.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {/* Header — count + chapter map (multi-source only) */}
      <div className="flex items-center justify-between">
        <div>
          <span className="text-sm font-semibold">
            {candidates.length} result{candidates.length !== 1 && "s"}
          </span>
          <span className="text-xs text-muted-foreground ml-2">
            for "{results.query}"
          </span>
        </div>
      </div>

      {results.winner_chapter_map && (
        <SearchChapterMap chapterMap={results.winner_chapter_map} />
      )}

      {/* Candidate list */}
      <div className="space-y-6">
        {candidates.map((candidate, ci) => (
          <div
            key={`${candidate.canonical_title}-${ci}`}
            className="space-y-2 animate-slide-up"
            style={{ animationDelay: `${Math.min(ci * 50, 400)}ms` }}
          >
            {/* Candidate header */}
            <div className="flex items-baseline gap-2">
              <h3 className="text-sm font-semibold truncate">
                {candidate.canonical_title}
              </h3>
              {candidate.canonical_year && (
                <span className="text-xs text-muted-foreground tabular-nums">
                  · {candidate.canonical_year}
                </span>
              )}
              <span className="text-[10px] text-muted-foreground">
                · {candidate.sources.length} source{candidate.sources.length !== 1 && "s"}
              </span>
            </div>

            {/* Sources — horizontal scroll-snap row for compact density.
                The cards are 160px wide; on a 1280px window we fit ~7
                without scrolling, ~5 on a 960px window. */}
            <div className="flex gap-2.5 overflow-x-auto pb-2 -mx-1 px-1 snap-x">
              {candidate.sources.map((source, si) => (
                <div key={`${source.site}-${si}`} className="snap-start">
                  <SearchSourceCard
                    source={source}
                    officialPublisher={officialBySite[source.site] || null}
                    multiSourceUsed={!!opts?.multiSource}
                    /* Fix B (2026-05-07): pass the full candidate so the card
                       can build a prefetched-alts payload. Without this each
                       card only knows its own source and would still trigger
                       a 291-site re-search inside aio-dl.py. */
                    candidate={candidate}
                    onQueue={onQueue}
                    index={si}
                  />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
