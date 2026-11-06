// ============================================================
// useDownloader Hook — Central State Manager
//
// This connects to the Electron main process via window.electronAPI
// (exposed by preload.js) and manages:
//   - Active downloads and their progress
//   - A queue system: auto-advances ONE at a time (currentDownloadId = the
//     serial "anchor"), but a queued item can be launched in PARALLEL with the
//     anchor via startQueuedNow ("start alongside")
//   - Persisting that queue across app close and replaying it on the next
//     launch (grep QUEUE_PERSIST_LIMIT / queueHydratedRef)
//   - Log lines from each download (flat array)
//   - Resumable downloads found on disk (tmp_* folders)
//   - App settings
//
// STATE SHAPES (what each component expects):
//   activeDownloads: { [downloadId]: { url, args, status, progress, logs, tmpDir? } }
//   logs: [ { downloadId, line, level, timestamp }, ... ]
//   queue: [ { id, type:"download"|"resume", url, displayUrl, queuedAt, restored?,
//             recipe? | args? (download) | tmpDir?/format?/epubLayout?/title?/cachedChapters? (resume) }, ... ]
//     A download item stores a `recipe` (lib/queueRecipe.js); its CLI args are
//     built when it STARTS, from the settings saved at that moment. Only items
//     queued before recipes existed (an old download_queue.json) carry frozen
//     `args`. The `queue` this hook RETURNS adds a display-only `args` to
//     recipe items (grep displayQueue); the persisted queue never has it.
//   resumable: [ { hid, tmpDir, params, cachedChapters }, ... ]
//   settings: { pythonCmd, scriptPath, workingDir, defaults, verboseAlways }
//
// `progress` also carries the main-process ETA fields (etaMs / chapterMsEma /
// etaSamples) — see electron/downloader.js:applyChapterEta.
// ============================================================

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { formatDuration } from "@/lib/utils";
import { useUpdateCheck } from "@/hooks/useUpdateCheck";
import { useSearch } from "@/hooks/useSearch";
import { finalArgsFor, globalArgs } from "@/lib/queueRecipe";

// DEFAULT_DOWNLOAD_DEFAULTS / mergeSettings / normalizeDownloadArgs were
// removed 2026-05-13: SettingsTab.jsx now owns the defaults dict. The
// architectural triad (main.js #3 + this file + SettingsTab.jsx #5) must
// agree — if you re-introduce defaults here, also update the other two.

// ── Helper: check if Electron IPC is available ──
// When running with just `npm run dev` (Vite only, no Electron),
// window.electronAPI won't exist. We return mock data so the UI
// still renders without crashing.
const hasAPI = () => typeof window !== "undefined" && !!window.electronAPI;

// ── Queue-item spawn/adopt helpers ──
// Module-level (NOT useCallback) on purpose: they read only their arguments +
// window.electronAPI + Date.now, so the useCallback closures below can reference
// them WITHOUT dep-array churn. A queue item is either a fresh download
// ({type:"download", url, displayUrl, recipe | args}) or a resume job
// ({type:"resume", url, tmpDir, format, epubLayout, title}). Both drain through
// the SAME path (_startNextInQueue / startQueuedNow) via these helpers, so
// resume and download queue and replay identically.

// The CLI args a download item starts with under settings `s`: its recipe
// resolved NOW (lib/queueRecipe.js), or the frozen args of a legacy item.
// undefined for resume items — they replay run_params.json, and main.js applies
// the current CPU/network limits to them itself (resumeThrottleFlags).
// THROWS on a malformed recipe (a restored download_queue.json can hold
// anything). Every caller resolves BEFORE spawning and drops the item on a
// throw, so one bad entry can't wedge the queue in a retry loop.
function _resolveQueueArgs(item, s) {
  if (item.type === "resume") return undefined;
  return item.recipe ? finalArgsFor(item.recipe, s) : item.args;
}

// Spawn a queue item via the right IPC with the args from _resolveQueueArgs.
// Returns main's { downloadId }. The resume branch mirrors resumeDownload's
// original payload. grep _spawnQueueItem for the call sites.
async function _spawnQueueItem(item, args) {
  if (item.type === "resume") {
    return window.electronAPI.resumeDownload({
      url: item.url,
      tmpDir: item.tmpDir,
      format: item.format,          // optional format override from the resume UI
      epubLayout: item.epubLayout,  // optional epub layout when format is epub
    });
  }
  return window.electronAPI.startDownload({ url: item.url, args });
}

// Build the activeDownloads entry for a just-spawned queue item. `args` are the
// RESOLVED args it was spawned with (the running snapshot persists them, and
// _restoreQueueItems reads their format). `tmpDir` is carried onto the active
// entry ONLY so resumeDownload's dedupe can spot an in-flight resume for the
// same folder (grep dupActive in resumeDownload).
function _activeEntryForQueueItem(item, args) {
  return {
    url: item.url,
    displayUrl: item.displayUrl,
    args,                     // undefined for resume items — harmless
    tmpDir: item.tmpDir,      // undefined for download items — harmless
    status: "running",
    progress:
      item.type === "resume"
        ? { phase: "resuming", title: item.title || "" }
        : { phase: "starting" },
    logs: [],
    queuedAt: item.queuedAt,
    startedAt: Date.now(),
  };
}

// ── Queue persistence across app close ──
// The queue is pure React state and dies with the renderer, so it's mirrored
// to download_queue.json via the queue:get / queue:save IPC (storage + shape:
// electron/history.js:saveQueueSnapshot). Restore behavior is RESTORE +
// AUTO-START: whatever was queued comes back in order, and whatever was
// RUNNING at close is re-inserted at the HEAD as a resume job.
const QUEUE_PERSIST_LIMIT = 200;
// Long enough to coalesce a burst of enqueues (queue-from-search fires several
// setQueue calls in a row), short enough that a close right after a click has
// already flushed. The confirm-quit dialog also buys dwell time whenever
// something is actually running.
const QUEUE_PERSIST_DEBOUNCE_MS = 300;

// Rebuild the queue array from a saved snapshot. Module-level for the same
// reason as _spawnQueueItem: no dep-array churn. `mintId` supplies fresh
// `q<N>` ids from the caller's counter; `resumableList` is a FRESH
// scanResumable (the mount effect awaits it first — the ordering is
// load-bearing).
//
// A snapshot's `running` entries were mid-download when the app closed. They
// come back as type:"resume" jobs at the HEAD of the restored queue, NOT as
// fresh downloads: their tmp_<hid> folder holds the chapters that already
// finished, and replaying them from scratch would redo that work. An entry
// with no LIVE tmp folder is DROPPED, not resurrected — the user deleted it,
// or the run never got far enough to write run_params.json.
//
// Queued items are restored VERBATIM (both types) — they never started, so
// there's nothing on disk to validate them against.
function _restoreQueueItems(snap, resumableList, mintId) {
  const savedQueue = Array.isArray(snap?.queue) ? snap.queue : [];
  const savedRunning = Array.isArray(snap?.running) ? snap.running : [];
  const live = Array.isArray(resumableList) ? resumableList : [];

  const revived = [];
  for (const r of savedRunning) {
    // hid is the tmp folder's own key; url is the fallback for a run that
    // died before the "Title (hid=…)" line landed. main.js's scan-resumable
    // handler back-fills BOTH fields from download history, so either can hit.
    // A series can have a chapter run AND a volume run in flight (tmp_<hid> /
    // tmpvol_<hid>, same hid), so the kind must agree too. A snapshot saved
    // before `kind` existed was always a chapter run.
    const kind = r?.kind || "chapter";
    const match = live.find(
      (x) => (x.kind || "chapter") === kind
        && ((r?.hid && x.hid === r.hid) || (r?.url && x.url === r.url))
    );
    if (!match) continue;
    // Two processes writing one tmp_ folder corrupts the resume state — same
    // invariant resumeDownload's dupInQueue/dupActive check protects.
    if (revived.some((v) => v.tmpDir === match.tmpDir)) continue;
    revived.push({
      id: mintId(),
      type: "resume",
      url: r.url || match.url,
      tmpDir: match.tmpDir,
      // A running RESUME carries no args (see _activeEntryForQueueItem), so
      // match.format — which scanResumable read out of run_meta.json — is the
      // authoritative fallback. undefined is fine too: downloader.resume
      // re-reads run_meta.json when no format is passed.
      format: r.args?.format || match.format || undefined,
      epubLayout: r.args?.epubLayout,
      title: r.title || match.title || "",
      cachedChapters: match.cachedChapters,
      displayUrl: r.displayUrl || r.title || match.title || r.url || match.url,
      queuedAt: Date.now(),
      restored: true,
    });
  }

  const queued = savedQueue
    .filter((item) => item && item.id && (item.type === "resume" ? item.tmpDir : item.url))
    .slice(0, QUEUE_PERSIST_LIMIT)
    // `restored` is purely a UI marker — QueueTab badges it so the auto-start
    // isn't mysterious. Nothing in the spawn path reads it.
    .map((item) => ({ ...item, restored: true }));

  return [...revived, ...queued];
}

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
    // Global toggle: collapse split-cluster chapters + cross-source duplicate
    // fragments at download time AND in search-display diagnostic counts.
    // Default OFF as of 2026-05-27 (opt-in flip). The new refinement drops
    // source-only .1/.2/.3/.4 fragments under --multi-source, which is more
    // aggressive than the old behavior — explicit user buy-in is required.
    // The Python side reads args.collapse_splits; the UI emits the positive
    // --collapse-splits flag (downloader.js / searcher.js) when this is
    // explicitly true. See sites/chapter_merger.py:group_chapters_for_download
    // for the full Rule 2 / 3b / 6 refinement.
    collapseSplits: false,
    // Inter-chapter image prefetch worker count (Phase G7, 2026-05-08).
    // While chapter N is encoding/processing on the main thread, a
    // background thread downloads chapter N+1's images using this many
    // parallel workers. -1 = match the main download pool's image_workers.
    // 0 = disable prefetch entirely. Drop to 4 (or 0) when the upstream
    // CDN is rate-limiting and the extra concurrent burst from N+1's
    // downloads compounds throttling. See aio-dl.py:_start_image_prefetch.
    prefetchImageWorkers: -1,
    // Durable list of handler names the user disabled (Settings → Search →
    // "Search Sources", or the SlowSitesCallout). Excluded from the search
    // fan-out/probe AND from multi-source download alternatives. Injected into
    // useSearch's runSearch and every queued download (→ --disable-sites).
    // Placeholder here for the pre-load window; SettingsTab.DEFAULT_SETTINGS
    // owns the canonical default and get-settings hydrates the saved value.
    disabledSites: [],
  });
  const [queue, setQueue] = useState([]);
  const [currentDownloadId, setCurrentDownloadId] = useState(null);

  // ── Library state ──
  // Lifted from LibraryTab.jsx so the entries survive tab switches without
  // re-running the manga/ folder walk + cover-cache lookup on every mount.
  // null = not yet loaded; [] = loaded but empty (no series). LibraryTab
  // checks for null on mount and only calls loadLibrary when uninitialized.
  // The setter stays internal: its two writers are the thumbnail-ready
  // stream below and useUpdateCheck's updatedMeta splice, both of which run
  // in this hook. No component needs it.
  const [libraryEntries, setLibraryEntries] = useState(null);
  const [libraryLoading, setLibraryLoading] = useState(false);

  // ── "Check All" update-sweep state ──
  // Also lifted out of LibraryTab, and for a harder reason than the entries
  // were: the sweep runs in the MAIN process and keeps going after the tab
  // unmounts, so state owned by the tab was silently diverging from a live
  // scan. Owning it here keeps the subscription alive across tab switches and
  // lets a returning Library tab pick the scan back up. See
  // hooks/useUpdateCheck.js. It writes fresh site metadata back through
  // setLibraryEntries, hence the injection.
  const updateCheck = useUpdateCheck({ setLibraryEntries });

  // Refs so callbacks always see the latest state without re-subscribing
  const queueRef = useRef(queue);
  queueRef.current = queue;
  // Monotonic id source for queue items — the React key AND the stable handle
  // removeFromQueue / startQueuedNow operate on (indices shift under the auto-
  // drain, so id, not index). A counter (not Date.now) so rapid enqueues never
  // collide. Refs are exempt from exhaustive-deps, so the useCallback closures
  // below mint ids inline via `q${++queueIdRef.current}`.
  const queueIdRef = useRef(0);
  // Flips true once the saved snapshot has been read (whether or not it had
  // anything in it). The persist effect below refuses to write until then —
  // otherwise the initial EMPTY queue state would overwrite the saved file in
  // the ~50ms before hydration lands, which is exactly the data we're trying
  // to keep. grep QUEUE_PERSIST_DEBOUNCE_MS.
  const queueHydratedRef = useRef(false);
  const currentIdRef = useRef(currentDownloadId);
  currentIdRef.current = currentDownloadId;
  // activeDownloadsRef gives the IPC complete-handler synchronous read access
  // to the just-completed download's title / displayUrl without resorting to
  // setActiveDownloads-callback side effects (which run inside React's
  // batching window and aren't a clean place for log-buffer mutation).
  const activeDownloadsRef = useRef(activeDownloads);
  activeDownloadsRef.current = activeDownloads;
  // settingsRef keeps queueDownload / useSearch's runSearch pointed at the latest
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
  // Resolves once the saved settings have been read on mount, or the read
  // failed; it never rejects. Every spawn path awaits it before building a
  // recipe's args. The restored queue auto-starts ~300ms after mount, and
  // resolving against the pre-load placeholder (`defaults: {}`) would start it
  // with none of the user's settings. The mount effect replaces it.
  const settingsReadyRef = useRef(Promise.resolve());
  // Library scans can be triggered manually from the Library tab and
  // automatically after downloads finish. Keep only one scan active and
  // coalesce any extra requests into one follow-up scan so a batch finishing
  // several jobs doesn't hammer scan-library.
  const libraryRefreshInFlightRef = useRef(false);
  const libraryRefreshPendingRef = useRef(false);

  const refreshLibraryFromDisk = useCallback(async ({ showLoading = true } = {}) => {
    if (!hasAPI()) return;
    if (libraryRefreshInFlightRef.current) {
      libraryRefreshPendingRef.current = true;
      return;
    }

    libraryRefreshInFlightRef.current = true;
    if (showLoading) setLibraryLoading(true);
    try {
      do {
        libraryRefreshPendingRef.current = false;
        const data = await window.electronAPI.scanLibrary();
        setLibraryEntries(Array.isArray(data) ? data : []);
      } while (libraryRefreshPendingRef.current);
    } catch (err) {
      console.error("Failed to scan library:", err);
      // Keep prior entries on failure — surfacing nothing would mask the
      // existing list. The next manual refresh or completed download retries.
    } finally {
      libraryRefreshInFlightRef.current = false;
      if (showLoading) setLibraryLoading(false);
    }
  }, []);

  // ── Load settings + resumable list + the saved queue on mount ──
  //
  // ORDER MATTERS inside the async block: scanResumable must RESOLVE before
  // the queue snapshot is applied. Restored `running` entries are
  // reconstituted against that fresh list (see _restoreQueueItems), so running
  // the two in parallel would race the liveness check and either drop a valid
  // resume or revive a job whose tmp folder is gone.
  //
  // getSettings runs alongside it. The restore itself doesn't need settings,
  // but SPAWNING does (queue items build their args at start), so the read is
  // kept in settingsReadyRef and every spawn path awaits it.
  //
  // Deliberately references _startNextInQueue, which is defined further down:
  // effect BODIES run after the whole component body, and it's a
  // useCallback(…, []) so it never changes identity. Same arrangement as the
  // IPC-subscription effect below, which also omits it from its deps.
  useEffect(() => {
    if (!hasAPI()) return;

    settingsReadyRef.current = Promise.resolve()
      .then(() => window.electronAPI.getSettings())
      .then((s) => {
        if (!s) return;
        // Write the ref too: a spawn awaiting this promise reads settingsRef
        // before React has re-rendered with the new state.
        settingsRef.current = s;
        setSettings(s);
      })
      .catch((err) => console.error("Failed to load settings:", err));

    let cancelled = false;

    (async () => {
      let resumableList = [];
      try {
        const r = await window.electronAPI.scanResumable();
        if (Array.isArray(r)) {
          resumableList = r;
          if (!cancelled) setResumable(r);
        }
      } catch (err) {
        console.error("Failed to scan resumable downloads:", err);
      }
      if (cancelled) return;

      let snap = null;
      try {
        snap = await window.electronAPI.getQueueSnapshot?.();
      } catch (err) {
        console.error("Failed to read the saved download queue:", err);
      }
      if (cancelled) return;

      // Restored queue items KEEP their original q<N> ids (they're React keys
      // and the stable handle removeFromQueue / startQueuedNow operate on), so
      // reseed the counter past the highest one before minting anything new —
      // otherwise a fresh enqueue would collide with a restored row and both
      // would vanish on the first remove.
      let maxId = queueIdRef.current;
      for (const item of Array.isArray(snap?.queue) ? snap.queue : []) {
        const n = Number(String(item?.id || "").replace(/^q/, ""));
        if (Number.isFinite(n) && n > maxId) maxId = n;
      }
      queueIdRef.current = maxId;

      const restored = _restoreQueueItems(
        snap,
        resumableList,
        () => `q${++queueIdRef.current}`,
      );

      // Set BEFORE the early return so the persist effect is unblocked even
      // when there was nothing to restore.
      queueHydratedRef.current = true;
      if (restored.length === 0) return;

      setQueue(restored);
      // Auto-start. The serial anchor is always free at mount, so this drains
      // the head — the revived running job when the last session had one. The
      // existing serial-anchor logic needs no change, and resumeDownload's
      // tmpDir dedupe already blocks a double-spawn if the user also clicks
      // Resume in the bar. Delayed so React has committed setQueue before
      // _startNextInQueue reads queueRef (same reason the completion path
      // waits 500ms).
      setTimeout(() => _startNextInQueue(), 300);
    })();

    return () => { cancelled = true; };
  }, []);

  // ── Configurable: how often to flush buffered logs/progress to the UI ──
  // Lower = more responsive but more CPU. Default: 100ms (10 updates/sec).
  // Can be changed in Settings → "Log Update Interval"
  const flushInterval = settings?.logUpdateInterval ?? 100;

  // Cross-site search state + actions (hooks/useSearch.js), re-exported
  // below under the same names. Called here, after settingsRef and
  // flushInterval exist; it runs its own log subscription and flush timer.
  const search = useSearch({ settingsRef, flushInterval });

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

      refreshLibraryFromDisk({ showLoading: false });
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
      if (unsubThumb) unsubThumb();
    };
  }, [refreshLibraryFromDisk]); // Subscribe once; refresh helper has stable deps.

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
  // cost when idle is two length checks per fire — genuinely cheap. (The
  // search feed has its own timer in hooks/useSearch.js.)
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

  // ── Cheap signature of what's RUNNING ──
  // "<downloadId>:<hid>" per running entry, sorted and joined. This is a
  // persist TRIGGER, and it exists because activeDownloads itself is a fresh
  // object ~10x/sec under the flush timer above — depending on it directly
  // would turn every log line into a disk write. The signature only changes
  // when a download starts, ends, or first reports its hid (the field the
  // restore matches on), which is exactly when the snapshot is stale.
  // Recomputing it per render is a handful of property reads over 1-3 entries.
  const runningSignature = useMemo(() => {
    const parts = [];
    for (const [id, dl] of Object.entries(activeDownloads || {})) {
      if (dl.status !== "running") continue;
      parts.push(`${id}:${dl.progress?.hid || ""}`);
    }
    return parts.sort().join("|");
  }, [activeDownloads]);

  // ── Persist the queue + what's running (debounced) ──
  // Read back on the next launch by the mount effect above. Reads the CURRENT
  // activeDownloads through its ref rather than closing over the value, so the
  // debounce window always writes the freshest state.
  useEffect(() => {
    if (!hasAPI() || !queueHydratedRef.current) return;
    if (typeof window.electronAPI.saveQueueSnapshot !== "function") return;

    const timer = setTimeout(() => {
      const running = [];
      for (const [id, dl] of Object.entries(activeDownloadsRef.current || {})) {
        if (dl.status !== "running") continue;
        running.push({
          downloadId: id,
          hid: dl.progress?.hid || null,
          kind: dl.progress?.itemKind || "chapter",
          url: Array.isArray(dl.url) ? dl.url[0] : dl.url || null,
          displayUrl: dl.displayUrl || null,
          title: dl.progress?.title || null,
          // tmpDir is only ever set on resume entries (_activeEntryForQueueItem),
          // so it doubles as the type discriminator.
          type: dl.tmpDir ? "resume" : "download",
          tmpDir: dl.tmpDir || null,
          args: dl.args || null,
        });
      }
      window.electronAPI
        .saveQueueSnapshot({
          queue: queueRef.current.slice(0, QUEUE_PERSIST_LIMIT),
          running,
          savedAt: Date.now(),
        })
        .catch((err) => console.error("Failed to persist the download queue:", err));
    }, QUEUE_PERSIST_DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [queue, runningSignature]);

  // ── Internal: start the next item in the queue ──
  const _startNextInQueue = useCallback(async () => {
    if (queueRef.current.length === 0 || currentIdRef.current || startingRef.current) return;
    if (!hasAPI()) return;

    // Reserve the slot synchronously before the first await so a concurrent
    // queueDownload (e.g. user clicks Download again before we resolve) can't
    // also spawn.
    startingRef.current = true;
    let retryAfterMs = 0;
    try {
      await settingsReadyRef.current;
      // Read the head AFTER the wait: the user may have removed it meanwhile.
      const next = queueRef.current[0];
      if (next) {
        // Args are built NOW, from the settings in force at start. A recipe
        // that can't be resolved is dropped, not retried: retrying the same
        // head would block everything behind it.
        let args;
        let resolved = false;
        try {
          args = _resolveQueueArgs(next, settingsRef.current);
          resolved = true;
        } catch (err) {
          console.error("Dropping a queued download whose saved recipe can't be read:", err, next);
          setQueue((prev) => prev.filter((item) => item !== next));
          retryAfterMs = 300; // once React has committed the removal to queueRef
        }

        if (resolved) {
          try {
            // _spawnQueueItem branches resume vs download — the head item
            // becomes the new serial "anchor" regardless of type.
            const { downloadId } = await _spawnQueueItem(next, args);

            // UIR-1: only dequeue AFTER a successful spawn, so a throw leaves
            // the item in place to be retried (the startingRef guard above
            // prevents a concurrent call from double-spawning q[0] during the
            // await). Dequeue by IDENTITY, not slice(1): if the user removed a
            // DIFFERENT row during the spawn await, q[0] may no longer be
            // `next`, and slice(1) would drop that other item instead.
            setQueue((prev) => prev.filter((item) => item !== next));

            setActiveDownloads((prev) => ({
              ...prev,
              [downloadId]: _activeEntryForQueueItem(next, args),
            }));

            setCurrentDownloadId(downloadId);
          } catch (err) {
            console.error("Failed to start queued download:", err);
            retryAfterMs = 1000; // the item stays at the head
          }
        }
      }
    } finally {
      startingRef.current = false;
      // Scheduled after the slot is released, or the retry would find it taken.
      if (retryAfterMs) setTimeout(() => _startNextInQueue(), retryAfterMs);
    }
  }, []);

  // ── Public: add a download (starts immediately or queues) ──
  // url can be a string (single) or string[] (multi-URL batch with --jobs).
  //
  // The second argument is EITHER:
  //   { recipe }  — how to build the args (lib/queueRecipe.js). Every UI path
  //                 passes this. The args are built when the item STARTS, from
  //                 the settings saved at that moment, so a Settings save
  //                 reaches items that are still waiting.
  //   plain args  — frozen now under globalArgs (no current caller; kept so a
  //                 future direct caller still gets the global settings).
  // Returns a promise; Sync's FindSourcesSheet awaits it and reports a throw.
  const queueDownload = useCallback(
    async (url, argsOrRecipe) => {
      const recipe =
        argsOrRecipe && typeof argsOrRecipe.recipe === "object" && argsOrRecipe.recipe
          ? argsOrRecipe.recipe
          : null;
      // Fail at the caller, not silently later at spawn time: a recipe built
      // in-process that can't resolve is a bug in that caller.
      if (recipe) finalArgsFor(recipe, settingsRef.current);
      const frozenArgs = recipe
        ? null
        : { ...globalArgs(settingsRef.current), ...(argsOrRecipe || {}) };

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
          await settingsReadyRef.current;
          const args = recipe ? finalArgsFor(recipe, settingsRef.current) : frozenArgs;
          const { downloadId } = await window.electronAPI.startDownload({ url, args });

          setActiveDownloads((prev) => ({
            ...prev,
            [downloadId]: {
              url,
              displayUrl,
              args,
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

      // Otherwise add to queue. type:"download" discriminates from resume jobs
      // (which _spawnQueueItem replays via the resume IPC); id is the React key
      // + the handle removeFromQueue / startQueuedNow operate on. A recipe item
      // stores NO args — they're built when it starts (_resolveQueueArgs).
      setQueue((prev) => [
        ...prev,
        {
          id: `q${++queueIdRef.current}`,
          type: "download",
          url,
          displayUrl,
          ...(recipe ? { recipe } : { args: frozenArgs }),
          queuedAt: Date.now(),
        },
      ]);
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
  // item: { url, tmpDir, format?, epubLayout?, title?, cachedChapters?, params? }
  //
  // Mirrors queueDownload's serialization: resume now routes through the SAME
  // single-anchor gate instead of spawning immediately (the pre-2026-07-15 bug
  // — clicking Resume on N series spawned N concurrent processes, each clobbering
  // currentDownloadId). Returns { status } so App.jsx can react (jump to Queue):
  //   "started"   — nothing was running; spawned now as the serial anchor
  //   "queued"    — a download was running; parked in the queue
  //   "duplicate" — a resume for this tmpDir is already queued/running (dedupe)
  //   "noop"      — no API / no url
  const resumeDownload = useCallback(async (item) => {
    if (!hasAPI() || !item.url) return { status: "noop" };

    const title = item.title || item.params?.title || "";
    const resumeItem = {
      id: `q${++queueIdRef.current}`,
      type: "resume",
      url: item.url,
      tmpDir: item.tmpDir,
      format: item.format,          // optional format override from the resume UI
      epubLayout: item.epubLayout,  // optional epub layout when format is epub
      title,
      cachedChapters: item.cachedChapters,
      displayUrl: title || item.url,
      queuedAt: Date.now(),
    };

    // Dedupe by tmpDir: two processes writing one tmp_ folder corrupts the
    // resume state — a real risk now that "start alongside" can run resumes in
    // PARALLEL. Bail if the same folder is already queued or actively running.
    // Guarded on a truthy tmpDir so undefined can't collide with other
    // tmpDir-less entries (resumable items always carry tmpDir — it's the scan key).
    if (item.tmpDir) {
      const dupInQueue = queueRef.current.some(
        (q) => q.type === "resume" && q.tmpDir === item.tmpDir
      );
      const dupActive = Object.values(activeDownloadsRef.current).some(
        (d) => d.status === "running" && d.tmpDir === item.tmpDir
      );
      if (dupInQueue || dupActive) return { status: "duplicate" };
    }

    // Same gate as queueDownload (grep currentIdRef.current in this file): run
    // now only if the serial slot is free AND no spawn is mid-flight; else queue.
    if (!currentIdRef.current && !startingRef.current) {
      startingRef.current = true;
      try {
        // Resume items carry no args (they replay run_params.json).
        const { downloadId } = await _spawnQueueItem(resumeItem, undefined);
        setActiveDownloads((prev) => ({
          ...prev,
          [downloadId]: _activeEntryForQueueItem(resumeItem, undefined),
        }));
        setCurrentDownloadId(downloadId);
        return { status: "started" };
      } catch (err) {
        console.error("Failed to resume download:", err);
        // Fall through to enqueue so the user's intent is preserved.
      } finally {
        startingRef.current = false;
      }
    }

    setQueue((prev) => [...prev, resumeItem]);
    return { status: "queued" };
  }, []);

  // ── Public: delete a tmp folder ──
  const deleteTemp = useCallback(async (tmpDir) => {
    if (!hasAPI()) return;
    await window.electronAPI.deleteTemp(tmpDir);
    const r = await window.electronAPI.scanResumable();
    if (Array.isArray(r)) setResumable(r);
  }, []);

  // ── Public: start a queued item NOW, in parallel with the current download ──
  // ("start alongside", the QueueTab ⚡ button). The promoted item is spawned as
  // a NON-anchor extra: it does NOT take over currentDownloadId, so the auto-
  // queue keeps draining off the existing anchor and this extra's completion is
  // a no-op for queue advancement (completion handler / cancelDownload only
  // advance when currentIdRef.current === downloadId). EXCEPTION: if nothing is
  // running it becomes the anchor (becomesPrimary) so the chain still advances.
  // Concurrency model: ~/.claude/plans/there-s-a-weird-bug-intended-purring-lecun.md
  const startQueuedNow = useCallback(async (id) => {
    const item = queueRef.current.find((q) => q.id === id);
    if (!item || !hasAPI()) return;

    const becomesPrimary = !currentIdRef.current && !startingRef.current;

    // Remove optimistically so the card (and its button) vanish before a second
    // click can double-spawn. Restored below if the spawn throws.
    setQueue((prev) => prev.filter((q) => q.id !== id));

    // Only claim the serial-slot mutex when this launch is the anchor; parallel
    // extras are independent and must NOT block each other on one startingRef.
    if (becomesPrimary) startingRef.current = true;
    try {
      await settingsReadyRef.current;
      // Built now from the current settings, like the auto-drain. An unreadable
      // recipe is dropped (it's already off the queue), never restored.
      let args;
      try {
        args = _resolveQueueArgs(item, settingsRef.current);
      } catch (err) {
        console.error("Dropping a queued download whose saved recipe can't be read:", err, item);
        if (becomesPrimary) setTimeout(() => _startNextInQueue(), 300);
        return;
      }
      const { downloadId } = await _spawnQueueItem(item, args);
      setActiveDownloads((prev) => ({
        ...prev,
        [downloadId]: _activeEntryForQueueItem(item, args),
      }));
      if (becomesPrimary) setCurrentDownloadId(downloadId);
    } catch (err) {
      console.error("Failed to start queued item now:", err);
      // Restore the item so the user's intent isn't silently lost.
      setQueue((prev) => [item, ...prev]);
      if (becomesPrimary) setTimeout(() => _startNextInQueue(), 1000);
    } finally {
      if (becomesPrimary) startingRef.current = false;
    }
  }, [_startNextInQueue]);

  // ── Public: remove a queued (not yet started) download ──
  // By id, not index: startQueuedNow / removeFromQueue race against the auto-
  // drain and each other, and indices shift under them — id is stable.
  const removeFromQueue = useCallback((id) => {
    setQueue((prev) => prev.filter((q) => q.id !== id));
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
    await refreshLibraryFromDisk({ showLoading: true });
  }, [refreshLibraryFromDisk]);

  // ── The queue as components see it ──
  // Recipe items gain a display-only `args`: what they WOULD start with under
  // the current settings, so QueueTab's format/quality badges follow a
  // Settings save the same way the spawn does. Recomputed only when the queue
  // or settings change. Never persisted, and never read back by the spawn path
  // (that uses queueRef, the raw items). An unreadable recipe shows no badges
  // here; the drain drops it when it reaches the head.
  const displayQueue = useMemo(
    () =>
      queue.map((item) => {
        if (!item.recipe) return item;
        let args;
        try {
          args = finalArgsFor(item.recipe, settings);
        } catch {
          args = undefined;
        }
        return { ...item, args };
      }),
    [queue, settings]
  );

  return {
    // State
    activeDownloads,
    logs,
    resumable,
    settings,
    queue: displayQueue,
    currentDownloadId,
    searchState: search.searchState,
    searchLogs: search.searchLogs,
    searchSiteHealth: search.searchSiteHealth,
    libraryEntries,
    libraryLoading,
    // { rows, scanState, scanStats, newChapterCounts, foundCount,
    //   start, cancel, resolveRows, setRowIgnored } — see
    //   hooks/useUpdateCheck.js
    updateCheck,

    // Actions
    queueDownload,
    cancelDownload,
    resumeDownload,
    deleteTemp,
    removeFromQueue,
    startQueuedNow,
    clearCompleted,
    clearLogs,
    saveSettings,
    refreshResumable,
    runSearch: search.runSearch,
    cancelSearch: search.cancelSearch,
    clearSearchLogs: search.clearSearchLogs,
    loadLibrary,
  };
}
