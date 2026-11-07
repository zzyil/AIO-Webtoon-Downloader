// ============================================================
// useSearch — the Search tab's cross-site search state.
//
// Owns: searchState (status/query/opts/results/error), the search log feed,
// the rolling in-memory per-site health map, and runSearch / cancelSearch /
// clearSearchLogs. Called by hooks/useDownloader.js, which re-exports every
// name unchanged (App.jsx passes them to SearchTab; Settings reads
// searchSiteHealth). Depends on window.electronAPI.runSearch / cancelSearch /
// onSearchLog (electron/preload.js → main.js "search:run" → searcher.js).
//
// THE RULE THIS HOOK EXISTS FOR: only the most recent runSearch call may write
// state. main's Searcher cancels the previous search process when a new one
// starts, and the cancelled call still resolves ({ok:false, cancelled:true}),
// often AFTER the newer search has finished. Each call takes a sequence number
// and every settle path bails when a newer call has started, so a late answer
// can never replace the current search's results. (Before this, React
// StrictMode's double-run of SearchTab's find effect started two searches and
// the loser's "cancelled" hid the winner's results about 1 run in 3.)
// Log lines are scoped the same way: runSearch passes a request id that main
// uses as the lines' searchId, and lines from any other search are dropped.
//
// Relative imports only: tools/_test_search_hook.js bundles this file with
// esbuild outside Vite.
// ============================================================

import { useState, useEffect, useCallback, useRef } from "react";

const hasAPI = () => typeof window !== "undefined" && !!window.electronAPI;

// The feed holds a few searches' worth: each produces ~50-100 stderr lines.
const SEARCH_LOG_CAP = 500;

// Fold one finished search's per-site health into the rolling map. Pure.
//   result.site_health  → STRIKES (down +2, slow +1, capped 4)
//   result.tested_sites → the DECAY roster (tested-and-healthy this run → −1)
// A site in neither list wasn't measured this run, so it's left untouched.
// status "busy" is the exception inside site_health: THIS machine kept the
// site from answering (mangafire's browser held by another AIO process — grep
// SiteBusyError in sites/base.py). It is a notice SearchTab shows from the
// result itself (grep BusySitesNotice), never a strike, and Python leaves busy
// sites out of tested_sites so they don't decay either.
// Returns `prev` itself when the result carries neither list.
export function foldSiteHealth(prev, result, now = Date.now()) {
  const health = Array.isArray(result?.site_health) ? result.site_health : [];
  const tested = Array.isArray(result?.tested_sites) ? result.tested_sites : [];
  if (!health.length && !tested.length) return prev;
  const next = { ...prev };
  const flagged = new Set();
  // 1) Strike the sites flagged slow/down this run.
  for (const h of health) {
    const key = h?.site;
    if (!key || h.status === "busy") continue;
    flagged.add(key);
    const prevStrikes = next[key]?.strikes || 0;
    const delta = h.status === "down" ? 2 : 1; // down clears the 2-strike bar alone
    next[key] = {
      strikes: Math.min(prevStrikes + delta, 4),
      lastStatus: h.status || "slow",
      lastReason: h.reason || null,
      lastFanoutS: h.fanout_s ?? null,
      lastProbeS: h.probe_s ?? null,
      displayName: h.display_name || key,
      host: h.host || null,
      lastSeen: now,
    };
  }
  // 2) Decay sites that were tested this run but came back healthy (present in
  //    tested_sites, absent from site_health). Fully-decayed entries are
  //    deleted so the map stays bounded to live problems.
  for (const site of tested) {
    if (flagged.has(site)) continue;
    const cur = next[site];
    if (!cur) continue; // never flagged → nothing to decay
    const strikes = Math.max((cur.strikes || 0) - 1, 0);
    if (strikes === 0) {
      delete next[site];
    } else {
      next[site] = { ...cur, strikes, lastStatus: "ok", lastReason: null, lastSeen: now };
    }
  }
  return next;
}

/**
 * @param {object} p
 * @param {{current: object}} p.settingsRef  latest settings (useDownloader's
 *   ref), read at call time so a Settings save never recreates runSearch.
 * @param {number} p.flushInterval  ms between log-feed flushes (Settings →
 *   "Log Update Interval"); a change restarts the timer.
 */
export function useSearch({ settingsRef, flushInterval }) {
  // status: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  // results: parsed JSON from `aio-dl.py --search --search-json`, or null.
  const [searchState, setSearchState] = useState({
    status: "idle",
    query: "",
    opts: {},
    results: null,
    error: null,
  });
  // stderr lines of the current search (and, once it ends, of the most recent
  // one), kept apart from download logs so the Search tab only shows search
  // progress.
  const [searchLogs, setSearchLogs] = useState([]);
  const pendingSearchLogsRef = useRef([]);

  // ── Rolling per-site search health (IN-MEMORY — deliberately NOT persisted) ──
  // Keyed by handler name → { strikes, lastStatus, lastReason, lastFanoutS,
  // lastProbeS, displayName, host, lastSeen }; see foldSiteHealth. A merely-SLOW
  // site needs 2+ searches to cross the recommend threshold (+1 per slow run);
  // a DOWN/unreachable site crosses in one (+2).
  //
  // WHY in-memory and not a persisted setting: writing it to `settings` every
  // search would fire SettingsTab's hydration effect mid-edit and clobber the
  // user's unsaved Settings draft (searches run 30–240 s — "start search, edit
  // Settings, search finishes" is reachable). Resetting strikes on app restart
  // is acceptable; only the disabledSites LIST is durable. The SearchTab callout
  // and the Settings "Search Sources" section both read this snapshot.
  const [searchSiteHealth, setSearchSiteHealth] = useState({});

  // Sequence of the most recent runSearch call, and the request id it sent
  // (the searchId its log lines carry). Bumped in runSearch's body, never
  // inside a state updater: StrictMode runs updaters twice.
  const searchSeqRef = useRef(0);
  const currentRequestIdRef = useRef(null);

  // Live stderr from main ("search-log"), buffered until the next flush.
  useEffect(() => {
    if (!hasAPI()) return;
    const unsub = window.electronAPI.onSearchLog?.(({ searchId, line, level }) => {
      if (searchId !== currentRequestIdRef.current) return; // a replaced search
      const timestamp = new Date().toLocaleTimeString("en-GB");
      pendingSearchLogsRef.current.push({ searchId, line, level, timestamp });
    });
    return () => unsub?.();
  }, []);

  // Push buffered lines into state on the app's log cadence. Runs even when
  // idle for the same reason as useDownloader's download flush: a search's
  // last stderr lines can land after it settled.
  useEffect(() => {
    const timer = setInterval(() => {
      const newLines = pendingSearchLogsRef.current;
      if (newLines.length === 0) return;
      pendingSearchLogsRef.current = [];
      setSearchLogs((prev) => {
        const combined = prev.concat(newLines);
        return combined.length > SEARCH_LOG_CAP ? combined.slice(-SEARCH_LOG_CAP) : combined;
      });
    }, flushInterval);
    return () => clearInterval(timer);
  }, [flushInterval]);

  // ── Public: run a cross-site search ──
  // opts: { multiSource, seededOnly, searchLanguage, multiSourceQualityMin,
  //         searchTimeout, searchMinMatch, searchParallelism, disabledSites }
  // Resolves with the parsed JSON, or null on failure / cancel / when a newer
  // search replaced this one. The UI reads searchState for status and error.
  const runSearch = useCallback(async (query, opts = {}) => {
    if (!hasAPI() || !query?.trim()) return null;

    const seq = ++searchSeqRef.current;
    const requestId = `srch_r${seq}_${Date.now()}`;
    currentRequestIdRef.current = requestId;
    const isCurrent = () => seq === searchSeqRef.current;

    // Fresh feed per search: runs are discrete.
    setSearchLogs([]);
    pendingSearchLogsRef.current = [];

    // Global settings every search honors; caller opts spread last and win.
    //   collapseSplits: SearchTab's inline toggle writes settings.collapseSplits
    //     via onSaveSettings. Opt-in since 2026-05-27, so `=== true`;
    //     searcher.js buildSearchArgs emits --collapse-splits only when true.
    //   disabledSites → --disable-sites (searcher.js). The callout's "Disable &
    //     re-search" passes an explicit opts.disabledSites to dodge the
    //     settingsRef update race (saveSettings updates React state async, but
    //     the re-search fires synchronously right after).
    const s = settingsRef.current;
    const finalOpts = {
      collapseSplits: s?.collapseSplits === true,
      ...(Array.isArray(s?.disabledSites) && s.disabledSites.length
        ? { disabledSites: s.disabledSites }
        : {}),
      ...opts,
    };

    setSearchState({
      status: "running",
      query,
      opts: finalOpts,
      results: null,
      error: null,
    });

    try {
      const { ok, result, error, cancelled } =
        await window.electronAPI.runSearch(query, finalOpts, requestId);
      if (!isCurrent()) return null;
      if (!ok) {
        setSearchState((prev) => ({
          ...prev,
          status: cancelled ? "cancelled" : "error",
          error: error || "Search failed",
        }));
        return null;
      }
      setSearchState((prev) => ({
        ...prev,
        status: "done",
        results: result,
        error: null,
      }));
      setSearchSiteHealth((prev) => foldSiteHealth(prev, result));
      return result;
    } catch (err) {
      if (!isCurrent()) return null;
      setSearchState((prev) => ({
        ...prev,
        status: "error",
        error: err?.message || String(err),
      }));
      return null;
    }
  }, [settingsRef]);

  // ── Public: cancel the in-flight search ──
  // The current runSearch then settles as "cancelled" through its own path.
  const cancelSearch = useCallback(async () => {
    if (!hasAPI()) return;
    await window.electronAPI.cancelSearch();
  }, []);

  // ── Public: clear the search log feed ──
  const clearSearchLogs = useCallback(() => {
    setSearchLogs([]);
    pendingSearchLogsRef.current = [];
  }, []);

  return {
    searchState,
    searchLogs,
    searchSiteHealth,
    runSearch,
    cancelSearch,
    clearSearchLogs,
  };
}
