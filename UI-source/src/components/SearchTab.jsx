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
  Checkbox,
} from "@/components/ui/primitives";
import {
  Search,
  X,
  Loader2,
  Sparkles,
  Play,
  RotateCw,
  Trash2,
  Download,
  SlidersHorizontal,
  Gauge,
  AlertTriangle,
  ArrowRight,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { LANGUAGES } from "@/lib/constants";
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

// LANGUAGES moved to @/lib/constants (shared with DownloadTab + SettingsTab).

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
  // ML image-quality rating (CLIP-IQA + NIQE + paired DISTS). Default OFF
  // because torch's Windows import path hits platform.machine() which Python
  // 3.13 routes through WMI — that can hang indefinitely on degraded hosts,
  // bricking --search for the whole session. See search_orchestrator.py's
  // _ML_RATING_ENABLED docstring for the full rationale. The toggle wires
  // through to electron/searcher.js (opts.enableMlRating → --enable-ml-rating)
  // and aio_search_cli sets the orchestrator gate before search_all runs.
  enableMlRating: false,
};

const DOWNLOAD_FORMATS = [
  { value: "pdf", label: "PDF" },
  { value: "epub", label: "EPUB" },
  { value: "cbz", label: "CBZ" },
  { value: "none", label: "None" },
];

// ── Slow/down-site recommendation (2026-07-13) ──
// A tracked site is recommended for disabling once it has ≥2 strikes (a merely
// slow site, seen slow in 2+ searches) OR was down/unreachable in the last run
// (down = +2 strikes in one go — see useDownloader.setSearchSiteHealth). Mirror
// of the ProbeFailureCache threshold: don't nag on a one-off blip.
const RECOMMEND_STRIKE_THRESHOLD = 2;
// Empty-roster guard: block "Disable & re-search" if it would leave fewer than
// this many searchable sites, so a user can't accidentally disable everything.
const MIN_REMAINING_ROSTER = 3;
// Visual full-scale for the per-row latency bar (NOT correctness — just where
// the bar reads as "maxed"). Tuned to the Python soft deadlines so a fan-out
// near the 60 s barrier fills the bar. Cross-file: search_orchestrator.py
// _FANOUT_DEADLINE_S (60) and BaseSiteHandler.PROBE_SOURCE_BUDGET_S (120).
const FANOUT_BUDGET_S = 60;
const PROBE_BUDGET_S = 120;

// Map one rolling-health entry to its callout row presentation: a status label,
// a human latency/reason detail, and the latency-bar fill %. A `down` site maxes
// the bar in red (worse than any slow site); a `slow` site fills proportionally
// to whichever phase dragged. Kept module-level (pure) so it's not rebuilt per render.
//
// Reason vocabulary since the 2026-07-13 reachability refinement (Python side:
// search_orchestrator.py _refine_with_reachability): `unreachable` is the one
// true DOWN bucket (the emit-time liveness GET couldn't reach any host);
// `search_error` is a SLOW verdict — the host is reachable but its search
// endpoint flaked this run (mangakatana's class: fine for downloads), so it
// only crosses the recommend bar after 2 such runs, never instantly.
function healthRowView(h) {
  const down = h.lastStatus === "down";
  const reason = h.lastReason;
  if (down) {
    let detail;
    if (reason === "unreachable") detail = "unreachable";
    else if (reason === "blocked") detail = "blocked · repeated failures";
    else if (reason === "probe_stuck") detail = "probe hung";
    else if (reason === "late_fanout") detail = "timed out";
    else detail = h.lastFanoutS != null ? `error · ${h.lastFanoutS.toFixed(1)}s` : "unreachable";
    return { down, statusLabel: "down", detail, barPct: 100 };
  }
  let detail, barPct;
  if (reason === "search_error") {
    // Reachable host, its search endpoint flaked — not slow, not dead. Show the
    // fail latency (how long it churned before erroring) and size the bar by it.
    const s = h.lastFanoutS != null ? h.lastFanoutS : 0;
    detail = h.lastFanoutS != null ? `search error · ${s.toFixed(1)}s` : "search error";
    barPct = Math.min(100, Math.max(8, (s / FANOUT_BUDGET_S) * 100));
  } else if (reason === "slow_probe" && h.lastProbeS != null) {
    detail = `probe ${h.lastProbeS.toFixed(1)}s`;
    barPct = Math.min(100, Math.max(8, (h.lastProbeS / PROBE_BUDGET_S) * 100));
  } else {
    const s = h.lastFanoutS != null ? h.lastFanoutS : 0;
    detail = `fan-out ${s.toFixed(1)}s`;
    barPct = Math.min(100, Math.max(8, (s / FANOUT_BUDGET_S) * 100));
  }
  return { down, statusLabel: "slow", detail, barPct };
}

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
  searchSiteHealth = {},
  onManageSources,
}) {
  const [query, setQuery] = useState("");
  const [downloadDialog, setDownloadDialog] = useState(null);
  // Session-only "don't recommend these again" set for the SlowSitesCallout.
  // Persists nothing — a chronically-bad site re-surfaces next session, which
  // is the intended nudge. Cleared implicitly on app restart (fresh state).
  const [dismissedSites, setDismissedSites] = useState(() => new Set());
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

  const openDownloadDialog = (url, args = {}, context = {}) => {
    setDownloadDialog({
      url,
      args,
      source: context.source,
      candidate: context.candidate,
    });
  };

  const closeDownloadDialog = () => setDownloadDialog(null);

  const confirmSearchDownload = (overrides) => {
    if (!downloadDialog) return;
    const finalArgs = {
      ...(downloadDialog.args || {}),
      ...overrides,
    };
    if (!finalArgs.multiSource) {
      delete finalArgs.prefetchedAlts;
      delete finalArgs.multiSourcePrefetched;
    }
    onStartDownload?.(downloadDialog.url, finalArgs);
    setDownloadDialog(null);
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

  // Set of currently-disabled handler names (durable setting). A disabled site
  // is already excluded from the fan-out, so it must never be re-recommended.
  const disabledSet = useMemo(
    () => new Set((Array.isArray(settings?.disabledSites) ? settings.disabledSites : [])
      .map((s) => String(s).toLowerCase())),
    [settings?.disabledSites],
  );

  // The recommendation set for the callout: tracked sites that crossed the
  // strike threshold (or went down last run), minus anything already disabled
  // or dismissed this session. Sorted down-first, then by strikes, then by the
  // worst measured latency — the most-worth-disabling site leads.
  const recommendedSites = useMemo(() => {
    const out = [];
    for (const [site, h] of Object.entries(searchSiteHealth || {})) {
      if (!h) continue;
      const recommend = (h.strikes || 0) >= RECOMMEND_STRIKE_THRESHOLD || h.lastStatus === "down";
      if (!recommend) continue;
      if (disabledSet.has(site.toLowerCase())) continue;
      if (dismissedSites.has(site)) continue;
      out.push({ site, ...h });
    }
    out.sort((a, b) => {
      const dr = (a.lastStatus === "down" ? 0 : 1) - (b.lastStatus === "down" ? 0 : 1);
      if (dr) return dr;
      if ((b.strikes || 0) !== (a.strikes || 0)) return (b.strikes || 0) - (a.strikes || 0);
      const la = a.lastFanoutS ?? a.lastProbeS ?? 0;
      const lb = b.lastFanoutS ?? b.lastProbeS ?? 0;
      return lb - la;
    });
    return out;
  }, [searchSiteHealth, disabledSet, dismissedSites]);

  // Add the chosen sites to the durable disabled list, then re-run the SAME
  // search — now faster, since the fan-out skips them. The merged list is
  // passed explicitly in opts (not just saved) so the re-search doesn't race
  // the async settings write (see useDownloader.runSearch's disabledSites note).
  const handleDisableAndReSearch = (siteNames) => {
    if (!siteNames?.length) return;
    const cur = Array.isArray(settings?.disabledSites) ? settings.disabledSites : [];
    const merged = Array.from(new Set([...cur, ...siteNames]));
    onSaveSettings?.({ disabledSites: merged });
    const q = (searchState?.query || query || "").trim();
    if (q) runSearch(q, { ...opts, disabledSites: merged });
  };

  // Dismiss = stop recommending the CURRENTLY-listed sites this session. A
  // different site going bad later still surfaces a fresh callout.
  const handleDismissCallout = () => {
    setDismissedSites((prev) => {
      const next = new Set(prev);
      for (const s of recommendedSites) next.add(s.site);
      return next;
    });
  };

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
                  // Name + cached count for the queued resume card.
                  title: matchedResumable.title || matchedResumable.params?.title,
                  cachedChapters: matchedResumable.cachedChapters,
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
              gated on multi-source) since downloads are affected too.
              2026-05-27: flipped to OPT-IN — checked = strictly true.
              `=== true` (not `!== false`) so undefined/null defaults to OFF;
              old users who set it true stay opted in. */}
          <div className="flex items-center justify-between gap-3">
            <div>
              <Label htmlFor="opt-collapse-splits" className="text-xs font-medium block">
                Collapse split chapters
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Merge X.1/X.2/X.3 splits and drop source-only .1/.2/.3/.4
                fragment uploads (multi-source only). Default off — opt in if
                aggregators inflate your chapter counts.
              </p>
            </div>
            <Switch
              id="opt-collapse-splits"
              checked={settings?.collapseSplits === true}
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
            {/* ML image-quality rating — opt-in. Forwards to --enable-ml-rating
                via electron/searcher.js (opts.enableMlRating). Off by default;
                see DEFAULT_OPTS above for the WMI-hang rationale. */}
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label htmlFor="opt-ml-rating" className="text-xs font-medium">
                  ML image rating
                </Label>
                <p className="text-[10px] text-muted-foreground">
                  Torch-backed CLIP-IQA + NIQE + DISTS for sharper ranking
                  on borderline matches. Adds ~5 s startup and ~150 MB of
                  model weights on first use. Default off.
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

        {/* ── Slow/down-site advisory — slides in above the results. Gated on
            having results so it never competes with the live search feed, and
            on a non-empty recommendation set (strikes ≥ threshold / down, minus
            disabled + dismissed). `key` remounts it — resetting the per-site
            checkboxes to all-checked — whenever the recommended set changes. */}
        {hasResults && recommendedSites.length > 0 && (
          <SlowSitesCallout
            key={recommendedSites.map((s) => s.site).join(",")}
            sites={recommendedSites}
            eligibleCount={searchState?.results?.eligible_count ?? null}
            onDisable={handleDisableAndReSearch}
            onDismiss={handleDismissCallout}
            onManageSources={onManageSources}
          />
        )}

        {/* ── Results ── */}
        {hasResults && (
          <SearchResults
            results={searchState.results}
            opts={searchState.opts}
            officialBySite={officialBySite}
            onQueue={openDownloadDialog}
          />
        )}
      </div>
      {downloadDialog && (
        <SearchDownloadOptionsDialog
          pending={downloadDialog}
          settings={settings}
          onClose={closeDownloadDialog}
          onConfirm={confirmSearchDownload}
        />
      )}
    </div>
  );
}

// ────────────────────────────────────────────────────────────
// SLOW-SITES CALLOUT ("pop-up")
//
// Non-blocking inline advisory that slides in above the results when sites were
// slow or unreachable this search. Instrument-panel styling WITHIN the app's
// language: the amber "governed / heads-up" accent (same convention as
// SettingsTab's ManagedBanner), a 3px left accent bar (echoing the App rail +
// Settings nav active indicators), per-site checkboxes, a status pill, and the
// memorable detail — a diagnostic latency bar that turns raw seconds into an
// at-a-glance "this one's dragging" read (full red for down, proportional amber
// for slow). Recommend-only: nothing changes until the user acts.
//
// Props: sites (recommended health entries, pre-sorted), eligibleCount (the
// empty-roster guard denominator), onDisable(names[]), onDismiss(), onManageSources().
// SearchTab remounts this via `key` when the recommended set changes, so the
// per-site checkboxes always start all-checked for a fresh callout.
// ────────────────────────────────────────────────────────────
function SlowSitesCallout({ sites, eligibleCount, onDisable, onDismiss, onManageSources }) {
  const [checked, setChecked] = useState(() => new Set(sites.map((s) => s.site)));
  // Fill the latency bars from 0 → target on mount for a "gauge settling" read.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setMounted(true), 30);
    return () => clearTimeout(t);
  }, []);

  const toggle = (site) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(site)) next.delete(site);
      else next.add(site);
      return next;
    });

  const checkedNames = sites.map((s) => s.site).filter((s) => checked.has(s));
  const downCount = sites.filter((s) => s.lastStatus === "down").length;

  // Empty-roster guard: disabling the checked sites must leave a workable set.
  // Skipped when eligibleCount is unknown (payload from an older spawn).
  const wouldRemain = eligibleCount != null ? eligibleCount - checkedNames.length : null;
  const rosterTooSmall = wouldRemain != null && wouldRemain < MIN_REMAINING_ROSTER;
  const canDisable = checkedNames.length > 0 && !rosterTooSmall;

  const headline =
    sites.length === 1
      ? "1 site is slowing your searches"
      : `${sites.length} sites are slowing your searches`;

  return (
    <div className="relative overflow-hidden rounded-lg border border-amber-500/30 bg-amber-500/[0.07] animate-slide-up">
      {/* 3px left accent bar — the one distinctive structural touch. */}
      <span aria-hidden="true" className="absolute left-0 top-0 bottom-0 w-[3px] bg-amber-500" />

      <div className="pl-4 pr-3 py-3">
        {/* Header */}
        <div className="flex items-start gap-2.5">
          <span className="mt-0.5 flex items-center justify-center w-7 h-7 rounded-md bg-amber-500/15 text-amber-600 dark:text-amber-400 shrink-0">
            <Gauge className="w-4 h-4" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-amber-800 dark:text-amber-200">
              {headline}
            </div>
            <p className="text-[11px] text-amber-700/80 dark:text-amber-300/70 mt-0.5 leading-snug">
              {downCount > 0
                ? "They were unreachable or dragged the probe. Disable them to search faster."
                : "They dragged the search probe. Disable them to search faster."}
            </p>
          </div>
          <button
            type="button"
            onClick={onDismiss}
            aria-label="Dismiss"
            className="shrink-0 p-1 rounded text-amber-700/60 hover:text-amber-800 hover:bg-amber-500/10 dark:text-amber-300/50 dark:hover:text-amber-200 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Per-site rows — staggered reveal, matching the results stagger. */}
        <div className="mt-2.5 space-y-0.5">
          {sites.map((h, i) => {
            const view = healthRowView(h);
            const isChecked = checked.has(h.site);
            return (
              <div
                key={h.site}
                className="flex items-center gap-2.5 rounded-md px-1.5 py-1.5 hover:bg-amber-500/[0.06] transition-colors animate-slide-up"
                style={{ animationDelay: `${Math.min(i * 40, 240)}ms` }}
              >
                <Checkbox
                  checked={isChecked}
                  onCheckedChange={() => toggle(h.site)}
                  className={cn(
                    "border-amber-500/60",
                    isChecked && "bg-amber-500 border-amber-500 text-white",
                  )}
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs font-medium truncate">
                      {h.displayName || h.site}
                    </span>
                    <Badge
                      variant={view.down ? "destructive" : "warning"}
                      className="text-[9px] px-1.5 py-0 leading-tight shrink-0"
                    >
                      {view.statusLabel}
                    </Badge>
                    <span className="ml-auto shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
                      {view.detail}
                    </span>
                  </div>
                  {/* Diagnostic latency bar. */}
                  <div className="mt-1 h-1 rounded-full bg-muted/60 overflow-hidden">
                    <div
                      className={cn(
                        "h-full rounded-full transition-all duration-500 ease-out",
                        view.down ? "bg-red-500" : "bg-amber-500",
                      )}
                      style={{ width: mounted ? `${view.barPct}%` : "0%" }}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Empty-roster warning */}
        {rosterTooSmall && (
          <div className="mt-2 flex items-center gap-1.5 text-[10px] text-amber-700 dark:text-amber-300">
            <AlertTriangle className="w-3 h-3 shrink-0" />
            <span>That would leave too few sites to search — uncheck a few.</span>
          </div>
        )}

        {/* Footer actions */}
        <div className="mt-3 flex items-center gap-2">
          <Button
            size="sm"
            onClick={() => onDisable?.(checkedNames)}
            disabled={!canDisable}
            className="gap-1.5 bg-amber-600 text-white hover:bg-amber-600/90"
          >
            <RotateCw className="w-3.5 h-3.5" />
            Disable &amp; re-search
          </Button>
          <Button size="sm" variant="ghost" onClick={onDismiss} className="text-muted-foreground">
            Dismiss
          </Button>
          {onManageSources && (
            <button
              type="button"
              onClick={onManageSources}
              className="ml-auto inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
            >
              Manage in Settings
              <ArrowRight className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function SearchDownloadOptionsDialog({ pending, settings, onClose, onConfirm }) {
  const initial = useMemo(() => ({
    format: settings?.defaults?.format || "pdf",
    epubLayout: settings?.defaults?.epubLayout || "vertical",
    chapters: "all",
    group: settings?.defaults?.group || "",
    language: settings?.defaults?.language || settings?.searchOpts?.searchLanguage || "en",
    quality: settings?.defaults?.quality ?? 100,
    keepChapters: !!settings?.defaults?.keepChapters,
    keepImages: !!settings?.defaults?.keepImages,
    noFinalFile: !!settings?.defaults?.noFinalFile,
    multiSource: !!pending?.args?.multiSource,
  }), [pending?.args?.multiSource, settings?.defaults, settings?.searchOpts?.searchLanguage]);
  const [form, setForm] = useState(initial);

  useEffect(() => {
    setForm(initial);
  }, [initial]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const set = (key, value) => {
    setForm((prev) => ({
      ...prev,
      [key]: value,
      ...(key === "format" && value === "none" ? { keepImages: true } : {}),
      ...(key === "keepChapters" && !value ? { noFinalFile: false } : {}),
    }));
  };

  const submit = (event) => {
    event?.preventDefault();
    const chapters = (form.chapters || "").trim();
    const group = (form.group || "").trim();
    const args = {
      format: form.format,
      quality: form.quality,
      language: form.language,
      keepChapters: form.keepChapters,
      keepImages: form.keepImages,
      noFinalFile: form.noFinalFile,
      multiSource: form.multiSource,
    };
    if (form.format === "epub") args.epubLayout = form.epubLayout;
    if (chapters && chapters.toLowerCase() !== "all") args.chapters = chapters;
    if (group) args.group = group;
    onConfirm?.(args);
  };

  const title =
    pending?.source?.title ||
    pending?.candidate?.canonical_title ||
    pending?.source?.site ||
    "Search result";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-lg rounded-lg border bg-card shadow-xl animate-slide-up"
      >
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
          <div className="flex items-center gap-2 min-w-0">
            <SlidersHorizontal className="w-4 h-4 text-primary shrink-0" />
            <div className="min-w-0">
              <div className="text-sm font-semibold truncate">Download settings</div>
              <div className="text-[11px] text-muted-foreground truncate">
                {pending?.source?.site || "source"} · {title}
              </div>
            </div>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="h-8 w-8 shrink-0"
            title="Close"
          >
            <X className="w-4 h-4" />
          </Button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto px-4 py-3 space-y-4">
          <div>
            <Label className="text-xs">Format</Label>
            <div className="grid grid-cols-4 gap-2 mt-1">
              {DOWNLOAD_FORMATS.map((format) => (
                <button
                  key={format.value}
                  type="button"
                  onClick={() => set("format", format.value)}
                  className={cn(
                    "h-8 rounded-md border text-xs font-semibold transition-colors",
                    form.format === format.value
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border hover:border-primary/40 hover:bg-accent/40"
                  )}
                >
                  {format.label}
                </button>
              ))}
            </div>
          </div>

          {form.format === "epub" && (
            <div className="grid grid-cols-2 gap-2 animate-slide-up">
              {[
                ["vertical", "Vertical"],
                ["page", "Page"],
              ].map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => set("epubLayout", value)}
                  className={cn(
                    "h-8 rounded-md border text-xs font-medium transition-colors",
                    form.epubLayout === value
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border hover:border-primary/40 hover:bg-accent/40"
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="search-download-chapters" className="text-xs">
                Chapters
              </Label>
              <Input
                id="search-download-chapters"
                value={form.chapters}
                onChange={(e) => set("chapters", e.target.value)}
                onFocus={(e) => {
                  if (e.target.value === "all") e.target.select();
                }}
                placeholder="all, 1-20, 44"
                className="mt-1 h-8 text-xs font-mono"
              />
            </div>
            <div>
              <Label htmlFor="search-download-language" className="text-xs">
                Language
              </Label>
              <Select
                id="search-download-language"
                value={form.language}
                onChange={(e) => set("language", e.target.value)}
                className="mt-1 h-8 text-xs"
              >
                {LANGUAGES.map((language) => (
                  <option key={language.value} value={language.value}>
                    {language.label}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="search-download-group" className="text-xs">
                Group
              </Label>
              <Input
                id="search-download-group"
                value={form.group}
                onChange={(e) => set("group", e.target.value)}
                placeholder="optional"
                className="mt-1 h-8 text-xs"
              />
            </div>
            <div>
              <div className="flex items-center justify-between mb-1">
                <Label htmlFor="search-download-quality" className="text-xs">
                  Quality
                </Label>
                <Badge variant="secondary" className="font-mono text-[10px]">
                  {form.quality}
                </Badge>
              </div>
              <Slider
                id="search-download-quality"
                value={form.quality}
                onValueChange={(value) => set("quality", value)}
                min={1}
                max={100}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex items-center gap-2">
              <Checkbox
                id="search-keep-chapters"
                checked={form.keepChapters}
                onCheckedChange={(value) => set("keepChapters", value)}
              />
              <Label htmlFor="search-keep-chapters" className="text-xs cursor-pointer">
                Keep chapters
              </Label>
            </div>
            <div className="flex items-center gap-2">
              <Checkbox
                id="search-keep-images"
                checked={form.keepImages}
                onCheckedChange={(value) => set("keepImages", value)}
              />
              <Label htmlFor="search-keep-images" className="text-xs cursor-pointer">
                Keep images
              </Label>
            </div>
            <div className="flex items-center gap-2">
              <Checkbox
                id="search-no-final-file"
                checked={form.noFinalFile}
                onCheckedChange={(value) => set("noFinalFile", value)}
                disabled={!form.keepChapters}
              />
              <Label
                htmlFor="search-no-final-file"
                className={cn("text-xs cursor-pointer", !form.keepChapters && "opacity-40")}
              >
                No final file
              </Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch
                id="search-multi-source-download"
                checked={form.multiSource}
                onCheckedChange={(value) => set("multiSource", value)}
              />
              <Label
                htmlFor="search-multi-source-download"
                className="text-xs cursor-pointer"
              >
                Multi-source
              </Label>
            </div>
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-4 py-3">
          <Button type="button" variant="outline" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" size="sm" className="gap-1.5">
            <Download className="w-3.5 h-3.5" />
            Queue
          </Button>
        </div>
      </form>
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
                    fallbackCover={
                      candidate.sources.find(
                        (s) =>
                          s.url !== source.url &&
                          s.cover &&
                          String(s.cover).startsWith("localfile://")
                      )?.cover ||
                      candidate.sources.find((s) => s.url !== source.url && s.cover)?.cover ||
                      null
                    }
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
