// ============================================================
// useDownloader Hook — Central State Manager
//
// This connects to the Electron main process via window.electronAPI
// (exposed by preload.js) and manages:
//   - Active downloads and their progress
//   - A queue system (one download at a time, auto-advances)
//   - Log lines from each download (flat array)
//   - Resumable downloads found on disk (tmp_* folders)
//   - App settings
//
// STATE SHAPES (what each component expects):
//   activeDownloads: { [downloadId]: { url, args, status, progress, logs } }
//   logs: [ { downloadId, line, level, timestamp }, ... ]
//   queue: [ { url, args, queuedAt }, ... ]
//   resumable: [ { hid, tmpDir, params, cachedChapters }, ... ]
//   settings: { pythonCmd, scriptPath, workingDir, defaults, verboseAlways }
// ============================================================

import { useState, useEffect, useCallback, useRef } from "react";
import { formatDuration } from "@/lib/utils";

// ── Helper: check if Electron IPC is available ──
// When running with just `npm run dev` (Vite only, no Electron),
// window.electronAPI won't exist. We return mock data so the UI
// still renders without crashing.
const hasAPI = () => typeof window !== "undefined" && !!window.electronAPI;

export function useDownloader() {
  // ── State ──
  const [activeDownloads, setActiveDownloads] = useState({});
  const [logs, setLogs] = useState([]);
  const [resumable, setResumable] = useState([]);
  const [settings, setSettings] = useState({
    // Sensible defaults so components don't crash before settings load.
    // Empty path placeholders intentionally — the real per-machine values
    // arrive from main.js's get-settings IPC (DEV_SCRIPT_PATH derived from
    // __dirname, or the bundled Python path in packaged mode). Used to
    // hardcode an absolute path to the original developer's OneDrive
    // folder, which mkdirSync would silently create on any other machine.
    pythonCmd: "python",
    scriptPath: "",
    workingDir: "",
    defaults: {},
    verboseAlways: true,
    // Global toggle: collapse split-cluster chapters at download time AND in
    // search-display diagnostic counts. Default ON. The Python side reads
    // args.collapse_splits; the UI emits --no-collapse-splits when this is
    // explicitly false. See sites/chapter_merger.py:group_chapters_for_download
    // for the cluster rule.
    collapseSplits: true,
    // Inter-chapter image prefetch worker count (Phase G7, 2026-05-08).
    // While chapter N is encoding/processing on the main thread, a
    // background thread downloads chapter N+1's images using this many
    // parallel workers. -1 = match the main download pool's image_workers.
    // 0 = disable prefetch entirely. Drop to 4 (or 0) when the upstream
    // CDN is rate-limiting and the extra concurrent burst from N+1's
    // downloads compounds throttling. See aio-dl.py:_start_image_prefetch.
    prefetchImageWorkers: -1,
  });
  const [queue, setQueue] = useState([]);
  const [currentDownloadId, setCurrentDownloadId] = useState(null);

  // ── Cross-site search state ──
  // status: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  // results: parsed JSON from `aio-dl.py --search --search-json` or null.
  // searchLogs: stderr lines from the in-flight search (and the most recent
  //   completed search) — separate buffer from download logs so the search
  //   feed only shows search-relevant progress instead of the global firehose.
  const [searchState, setSearchState] = useState({
    status: "idle",
    query: "",
    opts: {},
    results: null,
    error: null,
  });
  const [searchLogs, setSearchLogs] = useState([]);
  const pendingSearchLogsRef = useRef([]);

  // ── Library state ──
  // Lifted from LibraryTab.jsx so the entries survive tab switches without
  // re-running the mangas/ folder walk + cover-cache lookup on every mount.
  // null = not yet loaded; [] = loaded but empty (no series). LibraryTab
  // checks for null on mount and only calls loadLibrary when uninitialized.
  // The setter is exposed so LibraryTab.handleCheckAll can splice updated
  // metadata back into entries without round-tripping through the IPC scan.
  const [libraryEntries, setLibraryEntries] = useState(null);
  const [libraryLoading, setLibraryLoading] = useState(false);

  // Refs so callbacks always see the latest state without re-subscribing
  const queueRef = useRef(queue);
  queueRef.current = queue;
  const currentIdRef = useRef(currentDownloadId);
  currentIdRef.current = currentDownloadId;
  // activeDownloadsRef gives the IPC complete-handler synchronous read access
  // to the just-completed download's title / displayUrl without resorting to
  // setActiveDownloads-callback side effects (which run inside React's
  // batching window and aren't a clean place for log-buffer mutation).
  const activeDownloadsRef = useRef(activeDownloads);
  activeDownloadsRef.current = activeDownloads;
  // settingsRef keeps queueDownload / runSearch closures pointed at the latest
  // settings without forcing those callbacks to be recreated (and forcing
  // every consumer's effect deps to invalidate) on every settings save.
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  // startingRef is a synchronous "spawn-in-flight" flag. queueDownload checks
  // it BEFORE the await electronAPI.startDownload() so a second concurrent
  // call (double-click on Download, or rapid queue-from-search + queue-from-
  // library) sees the slot reserved and falls through to the queue path
  // instead of also spawning. Without this, both calls observe
  // currentIdRef.current === null (it's only updated AFTER the await
  // resolves) and both spawn — the second clobbers currentDownloadId and the
  // first process becomes orphaned. Reset in the spawn's finally so a failed
  // start (rare, e.g. main-process IPC error) doesn't lock the queue.
  const startingRef = useRef(false);

  // ── Load settings + resumable list on mount ──
  useEffect(() => {
    if (!hasAPI()) return;

    window.electronAPI.getSettings().then((s) => {
      if (s) setSettings(s);
    });

    window.electronAPI.scanResumable().then((r) => {
      if (Array.isArray(r)) setResumable(r);
    });
  }, []);

  // ── Configurable: how often to flush buffered logs/progress to the UI ──
  // Lower = more responsive but more CPU. Default: 100ms (10 updates/sec).
  // Can be changed in Settings → "Log Update Interval"
  const flushInterval = settings?.logUpdateInterval ?? 100;

  // Refs that accumulate events between flushes (no re-renders until flush)
  const pendingLogsRef = useRef([]);
  const pendingProgressRef = useRef({});   // { downloadId: latestProgress }
  const pendingCompletionsRef = useRef([]); // completion events (flushed immediately)

  // ── Subscribe to live Electron IPC events ──
  useEffect(() => {
    if (!hasAPI()) return;

    // 1) Log lines — push into buffer, don't trigger React yet
    const unsubLog = window.electronAPI.onDownloadLog(({ downloadId, line, level }) => {
      const timestamp = new Date().toLocaleTimeString("en-GB");
      pendingLogsRef.current.push({ downloadId, line, level, timestamp });
    });

    // 2) Progress updates — keep only the latest per download
    const unsubProgress = window.electronAPI.onDownloadProgress(({ downloadId, progress }) => {
      pendingProgressRef.current[downloadId] = progress;
    });

    // 3) Download completed — handle immediately (don't wait for next flush)
    const unsubComplete = window.electronAPI.onDownloadComplete(({ downloadId, result }) => {
      setActiveDownloads((prev) => {
        const dl = prev[downloadId];
        if (!dl) return prev;
        return {
          ...prev,
          [downloadId]: {
            ...dl,
            status: result.status || "completed",
            result,
          },
        };
      });

      // Inject a synthetic "divider" entry into the global log buffer so the
      // LogPanel can render a horizontal-rule punctuation between successive
      // runs. Without this, multi-job sessions blur into a single wall of
      // text. activeDownloadsRef gives us the title/url before the run-end
      // state replaces it.
      const dl = activeDownloadsRef.current[downloadId];
      if (dl) {
        const title =
          dl.progress?.title ||
          dl.displayUrl ||
          (Array.isArray(dl.url) ? dl.url[0] : dl.url) ||
          "Download";
        const status = result.status || "completed";
        const duration = result.duration ? formatDuration(result.duration) : "";
        const dividerEntry = {
          downloadId,
          level: "divider",
          status,
          title,
          duration,
          line: `${title} · ${status}${duration ? ` · ${duration}` : ""}`,
          timestamp: new Date().toLocaleTimeString("en-GB"),
        };
        setLogs((prev) => {
          // concat avoids the spread's intermediate iterator allocation;
          // V8 can fast-path concat on dense arrays of known size.
          const combined = prev.concat([dividerEntry]);
          return combined.length > 5000 ? combined.slice(-5000) : combined;
        });
      }

      if (currentIdRef.current === downloadId) {
        setCurrentDownloadId(null);
        setTimeout(() => _startNextInQueue(), 500);
      }

      window.electronAPI.scanResumable().then((r) => {
        if (Array.isArray(r)) setResumable(r);
      });
    });

    // Search stderr stream — buffered same way as download logs so the
    // 100ms flush timer batches them. Each entry has a searchId for
    // correlation; the UI only shows the latest search's lines.
    const unsubSearchLog = window.electronAPI.onSearchLog?.(({ searchId, line, level }) => {
      const timestamp = new Date().toLocaleTimeString("en-GB");
      pendingSearchLogsRef.current.push({ searchId, line, level, timestamp });
    });

    // Library thumbnail-ready stream. Lifted from LibraryTab so the
    // subscription doesn't churn on every tab switch — and so a
    // tab-unmounted thumb completion still updates the central entries
    // list. setLibraryEntries(null) on initial state is filtered out so
    // we don't try to map() a null.
    const unsubThumb = window.electronAPI.onThumbnailReady?.(({ folderPath, thumbPath }) => {
      setLibraryEntries((prev) => {
        if (!Array.isArray(prev)) return prev;
        return prev.map((e) =>
          e.folderPath === folderPath ? { ...e, thumbPath } : e
        );
      });
    });

    return () => {
      unsubLog();
      unsubProgress();
      unsubComplete();
      if (unsubSearchLog) unsubSearchLog();
      if (unsubThumb) unsubThumb();
    };
  }, []); // Empty deps — subscribe once on mount

  // ── Flush timer: pushes buffered logs & progress into React state ──
  // This is where the "configurable update speed" lives.
  // At 100ms (default), the UI updates ~10 times per second.
  // At 1000ms, it updates once per second (lower CPU, less responsive).
  //
  // Why we DON'T deactivate when idle: the IPC handlers push directly to
  // refs (pendingLogsRef, pendingProgressRef) which don't trigger React
  // re-renders. A download's tail-end stderr lines often land AFTER
  // currentDownloadId/queue have flipped to "idle" (Python process is
  // shutting down stdout/stderr). If we deactivated the interval at that
  // boundary, those final lines would sit in the buffer forever. The
  // cost when idle is three boolean checks per fire — genuinely cheap.
  useEffect(() => {
    const timer = setInterval(() => {
      // --- Flush pending log lines ---
      const newLogs = pendingLogsRef.current;
      if (newLogs.length > 0) {
        pendingLogsRef.current = []; // clear buffer

        // Update the global flat log array. concat is faster than the
        // double-spread pattern on long arrays — V8 can pre-size the
        // result and skip iterator overhead. At the default 100ms flush
        // interval and a 5000-line cap, this fires up to 10x/sec during
        // a busy download, so the savings add up.
        setLogs((prev) => {
          const combined = prev.concat(newLogs);
          return combined.length > 5000 ? combined.slice(-5000) : combined;
        });

        // Update per-download logs
        setActiveDownloads((prev) => {
          const next = { ...prev };
          let changed = false;
          for (const entry of newLogs) {
            const dl = next[entry.downloadId];
            if (!dl) continue;
            changed = true;
            const dlLogs = [...(dl.logs || []), entry];
            next[entry.downloadId] = {
              ...dl,
              logs: dlLogs.length > 5000 ? dlLogs.slice(-5000) : dlLogs,
            };
          }
          return changed ? next : prev;
        });
      }

      // --- Flush pending search log lines ---
      const newSearchLogs = pendingSearchLogsRef.current;
      if (newSearchLogs.length > 0) {
        pendingSearchLogsRef.current = [];
        setSearchLogs((prev) => {
          const combined = prev.concat(newSearchLogs);
          // Cap at 500 — each search produces ~50-100 stderr lines, so
          // this comfortably holds 5+ recent searches.
          return combined.length > 500 ? combined.slice(-500) : combined;
        });
      }

      // --- Flush pending progress updates ---
      const progUpdates = pendingProgressRef.current;
      const progIds = Object.keys(progUpdates);
      if (progIds.length > 0) {
        pendingProgressRef.current = {}; // clear buffer

        setActiveDownloads((prev) => {
          const next = { ...prev };
          let changed = false;
          for (const id of progIds) {
            const dl = next[id];
            if (!dl) continue;
            changed = true;
            next[id] = {
              ...dl,
              progress: { ...(dl.progress || {}), ...progUpdates[id] },
            };
          }
          return changed ? next : prev;
        });
      }
    }, flushInterval);

    return () => clearInterval(timer);
  }, [flushInterval]);

  // ── Internal: start the next item in the queue ──
  const _startNextInQueue = useCallback(async () => {
    const q = queueRef.current;
    if (q.length === 0 || currentIdRef.current || startingRef.current) return;

    const next = q[0];
    // Remove it from the queue
    setQueue((prev) => prev.slice(1));

    // Spawn the Python process. Reserve the slot synchronously before the
    // await so a concurrent queueDownload (e.g. user clicks Download again
    // before we resolve) can't also spawn.
    if (!hasAPI()) return;
    startingRef.current = true;
    try {
      const { downloadId } = await window.electronAPI.startDownload({
        url: next.url,
        args: next.args,
      });

      setActiveDownloads((prev) => ({
        ...prev,
        [downloadId]: {
          url: next.url,
          args: next.args,
          status: "running",
          progress: { phase: "starting" },
          logs: [],
          queuedAt: next.queuedAt,
          startedAt: Date.now(),
        },
      }));

      setCurrentDownloadId(downloadId);
    } catch (err) {
      console.error("Failed to start queued download:", err);
      // Try the next one if this fails
      setTimeout(() => _startNextInQueue(), 1000);
    } finally {
      startingRef.current = false;
    }
  }, []);

  // ── Public: add a download (starts immediately or queues) ──
  // url can be a string (single) or string[] (multi-URL batch with --jobs)
  //
  // Injects global defaults (verbose, collapseSplits) before passing args to
  // the spawn so search/library/queue callsites don't have to re-implement
  // those in every callsite. Caller-provided args win on conflict (the spread
  // is positioned after the defaults), so DownloadTab's explicit verbose
  // setting still takes priority over the global default.
  const queueDownload = useCallback(
    async (url, args) => {
      const s = settingsRef.current;
      const finalArgs = {
        verbose: s?.verboseAlways !== false,
        collapseSplits: s?.collapseSplits !== false,
        // Curated-sites toggle. Persisted under settings.searchOpts.seededOnly
        // because SettingsTab + SearchTab both write that namespace; we mirror
        // it here so download paths see the same flag. Only takes effect when
        // --multi-source is on — aio-dl.py reads seeded_only inside
        // find_alternatives_for_direct_url to skip the long-tail Madara
        // extras during the cross-site fan-out (otherwise: 282 handlers
        // searched instead of ~30 in sites/quality_seed.json).
        // searcher.js:70 uses the identical opts.seededOnly check; this
        // closes the asymmetry where Search honored the toggle but
        // multi-source-direct-URL downloads ignored it.
        ...(s?.searchOpts?.seededOnly ? { seededOnly: true } : {}),
        // -1 sentinel = "match --image-workers"; the Python side resolves it.
        // Skip injecting if the value is explicitly -1 (the default) so the
        // CLI default also kicks in without a redundant flag in the spawn.
        ...(s?.prefetchImageWorkers != null && s.prefetchImageWorkers !== -1
          ? { prefetchImageWorkers: s.prefetchImageWorkers }
          : {}),
        // ── Fast-download knobs (added 2026-05-09; generalized 2026-05-13) ──
        // Same "skip if at default" pattern as prefetchImageWorkers above:
        // when the setting matches the Python-side default, leave it out of
        // the spawn so older saved settings dicts that don't have the field
        // still produce identical CLI invocations. Python defaults:
        //   imageConcurrency=8, imagePrefetchDepth=2, imagePrefetchParallel=2.
        // MangaFire VRF capture knobs (--mangafire-vrf-prefetch-depth,
        // --mangafire-vrf-parallel) were dropped from the UI on 2026-05-13
        // — argparse defaults serve everyone now; advanced users pass the
        // CLI flags directly. Migration note: settings dicts persisted
        // before 2026-05-13 carry `mangafireImageConcurrency` instead of
        // `imageConcurrency`. The SettingsTab loader migrates them at read
        // time, so by the time we get here `s.imageConcurrency` is live.
        ...(s?.imageConcurrency != null && s.imageConcurrency !== 8
          ? { imageConcurrency: s.imageConcurrency }
          : {}),
        ...(s?.imagePrefetchDepth != null && s.imagePrefetchDepth !== 2
          ? { imagePrefetchDepth: s.imagePrefetchDepth }
          : {}),
        ...(s?.imagePrefetchParallel != null && s.imagePrefetchParallel !== 2
          ? { imagePrefetchParallel: s.imagePrefetchParallel }
          : {}),
        ...(s?.noFastDownload === true ? { noFastDownload: true } : {}),
        ...args,
      };

      // Display label: show first URL + count for batches
      const displayUrl = Array.isArray(url)
        ? `${url[0]} (+${url.length - 1} more)`
        : url;

      // If nothing is running AND no spawn is currently in flight, start
      // immediately. The startingRef check + synchronous reservation closes
      // the double-click race: two concurrent calls between the check and
      // the await would otherwise both observe currentIdRef.current === null
      // (which is only updated AFTER the IPC resolves) and both spawn.
      if (!currentIdRef.current && !startingRef.current && hasAPI()) {
        startingRef.current = true;
        try {
          const { downloadId } = await window.electronAPI.startDownload({ url, args: finalArgs });

          setActiveDownloads((prev) => ({
            ...prev,
            [downloadId]: {
              url,
              displayUrl,
              args: finalArgs,
              status: "running",
              progress: { phase: "starting" },
              logs: [],
              startedAt: Date.now(),
            },
          }));

          setCurrentDownloadId(downloadId);
          return;
        } catch (err) {
          console.error("Failed to start download:", err);
          // Fall through to enqueue so the user's intent is preserved.
        } finally {
          startingRef.current = false;
        }
      }

      // Otherwise add to queue
      setQueue((prev) => [...prev, { url, displayUrl, args: finalArgs, queuedAt: Date.now() }]);
    },
    []
  );

  // ── Public: cancel a running download ──
  const cancelDownload = useCallback(async (downloadId) => {
    if (!hasAPI()) return;
    await window.electronAPI.cancelDownload(downloadId);

    setActiveDownloads((prev) => {
      const dl = prev[downloadId];
      if (!dl) return prev;
      return { ...prev, [downloadId]: { ...dl, status: "cancelled" } };
    });

    // If this was the active one, advance the queue
    if (currentIdRef.current === downloadId) {
      setCurrentDownloadId(null);
      setTimeout(() => _startNextInQueue(), 500);
    }
  }, [_startNextInQueue]);

  // ── Public: resume from a tmp folder ──
  // item: { url, tmpDir, format?, epubLayout?, params? }
  const resumeDownload = useCallback(async (item) => {
    if (!hasAPI() || !item.url) return;

    try {
      const { downloadId } = await window.electronAPI.resumeDownload({
        url: item.url,
        tmpDir: item.tmpDir,
        format: item.format,         // optional format override from dropdown
        epubLayout: item.epubLayout,  // optional epub layout when format is epub
      });

      setActiveDownloads((prev) => ({
        ...prev,
        [downloadId]: {
          url: item.url,
          status: "running",
          progress: { phase: "resuming", title: item.params?.title || "" },
          logs: [],
          startedAt: Date.now(),
        },
      }));

      setCurrentDownloadId(downloadId);
    } catch (err) {
      console.error("Failed to resume download:", err);
    }
  }, []);

  // ── Public: delete a tmp folder ──
  const deleteTemp = useCallback(async (tmpDir) => {
    if (!hasAPI()) return;
    await window.electronAPI.deleteTemp(tmpDir);
    const r = await window.electronAPI.scanResumable();
    if (Array.isArray(r)) setResumable(r);
  }, []);

  // ── Public: remove a queued (not yet started) download ──
  const removeFromQueue = useCallback((index) => {
    setQueue((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // ── Public: clear completed/failed/cancelled from the active list ──
  const clearCompleted = useCallback(() => {
    setActiveDownloads((prev) => {
      const next = {};
      for (const [id, dl] of Object.entries(prev)) {
        if (dl.status === "running") next[id] = dl;
      }
      return next;
    });
  }, []);

  // ── Public: clear all logs ──
  const clearLogs = useCallback(() => {
    setLogs([]);
  }, []);

  // ── Public: save settings ──
  const saveSettings = useCallback(async (newSettings) => {
    if (hasAPI()) {
      await window.electronAPI.saveSettings(newSettings);
    }
    setSettings((prev) => ({ ...prev, ...newSettings }));
  }, []);

  // ── Public: refresh resumable list ──
  const refreshResumable = useCallback(async () => {
    if (!hasAPI()) return;
    const r = await window.electronAPI.scanResumable();
    if (Array.isArray(r)) setResumable(r);
  }, []);

  // ── Public: load (or refresh) the library entries ──
  // Always force-fetches from disk via scan-library IPC. LibraryTab calls
  // this on first mount (when libraryEntries is null) and from the manual
  // refresh button. Held at the hook level so tab-switch unmounts don't
  // discard the result and force a re-scan.
  const loadLibrary = useCallback(async () => {
    if (!hasAPI()) return;
    setLibraryLoading(true);
    try {
      const data = await window.electronAPI.scanLibrary();
      setLibraryEntries(Array.isArray(data) ? data : []);
    } catch (err) {
      console.error("Failed to scan library:", err);
      // Keep prior entries on failure — surfacing nothing would mask the
      // existing list. The next manual refresh retries.
    } finally {
      setLibraryLoading(false);
    }
  }, []);

  // ── Public: run a cross-site search ──
  // opts: { multiSource, seededOnly, searchLanguage, multiSourceQualityMin,
  //         searchTimeout, searchMinMatch, searchParallelism }
  // Returns the parsed JSON (or null on failure) — the UI also reads
  // searchState directly for status and recent error.
  const runSearch = useCallback(async (query, opts = {}) => {
    if (!hasAPI() || !query?.trim()) return null;

    // Reset log feed for the new search so the user only sees current-run
    // stderr (rather than appending forever — search runs are discrete).
    setSearchLogs([]);
    pendingSearchLogsRef.current = [];

    // Merge the global collapseSplits setting into opts so SearchTab's inline
    // toggle (which writes settings.collapseSplits via onSaveSettings) is
    // honored even when the caller passes opts that omit the field. The
    // searcher.js buildSearchArgs only emits --no-collapse-splits when this
    // is explicitly false. Caller-passed opts win on conflict (...opts at end).
    const s = settingsRef.current;
    const finalOpts = {
      collapseSplits: s?.collapseSplits !== false,
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
      const { ok, result, error, cancelled } = await window.electronAPI.runSearch(query, finalOpts);
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
      return result;
    } catch (err) {
      setSearchState((prev) => ({
        ...prev,
        status: "error",
        error: err?.message || String(err),
      }));
      return null;
    }
  }, []);

  // ── Public: cancel an in-flight search ──
  const cancelSearch = useCallback(async () => {
    if (!hasAPI()) return;
    await window.electronAPI.cancelSearch();
  }, []);

  // ── Public: clear search log feed ──
  const clearSearchLogs = useCallback(() => {
    setSearchLogs([]);
    pendingSearchLogsRef.current = [];
  }, []);

  return {
    // State
    activeDownloads,
    logs,
    resumable,
    settings,
    queue,
    currentDownloadId,
    searchState,
    searchLogs,
    libraryEntries,
    libraryLoading,

    // Actions
    queueDownload,
    cancelDownload,
    resumeDownload,
    deleteTemp,
    removeFromQueue,
    clearCompleted,
    clearLogs,
    saveSettings,
    refreshResumable,
    runSearch,
    cancelSearch,
    clearSearchLogs,
    loadLibrary,
    setLibraryEntries,
  };
}
