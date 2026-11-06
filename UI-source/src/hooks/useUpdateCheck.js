// ============================================================
// useUpdateCheck — "Check All" sweep state, held ABOVE the tab switch
//
// WHY THIS FILE EXISTS: App.jsx renders tab bodies conditionally
// ({activeTab === "library" && <LibraryTab/>}), so LibraryTab UNMOUNTS the
// moment the user clicks Queue. All of this state used to be useState inside
// LibraryTab, so a tab switch wiped the rows, the progress counter and the
// grid's "+N new" badges, and dropped the onUpdateCheckProgress subscription
// so everything the still-running sweep emitted while away went nowhere.
// Coming back showed a pristine "Check All" button, and pressing it restarted
// a scan that was already 20 series deep. Living in useDownloader (mounted
// once, for the app's lifetime) is what fixes that — same reason
// libraryEntries and the thumbnail stream were lifted there.
//
// STATE OWNERSHIP: main.js is the record (grep _updateCheckRun), this hook is
// the live mirror. Rows arrive fully-derived on `event.row` — there is no
// second copy of the "is this found / uptodate / error" reducer here, which
// is what lets a get-update-check-state snapshot and a live event agree.
//
// STARTUP ORDERING: the snapshot IPC and the event stream race, so events
// that arrive before the snapshot resolves are BUFFERED and drained after it
// (grep `hydrated` in the subscribe effect). Snapshot-then-events is the only
// ordering where both sources compose without any merge rules — which is why
// adoptSnapshot can be a plain overwrite.
//
// KNOWN LIMIT: rows the user queues or dismisses are resolved here only —
// main's record still calls them "found". A window reload mid-sweep would
// therefore resurrect a dismissed row. Packaged builds expose no reload, and
// paying an IPC round-trip per dismiss to close a devtools-only hole isn't
// worth it; revisit if a reload path ever ships.
//
// setRowIgnored is the exception and does NOT share that limit: crossing a
// chapter out writes .aio_series.json, so it survives a reload, the app
// closing, and every future sweep — that persistence is the feature, not an
// implementation detail. It is also the only action here that talks to disk;
// resolveRows (queue / dismiss) stays session-local on purpose, because
// "I've seen this" is not the same claim as "never offer me this".
//
// Cross-file:
//   - electron/main.js  — check-all-updates / get-update-check-state /
//                         cancel-check-all-updates handlers + the record
//   - electron/preload.js — event shapes are documented on
//                         onUpdateCheckProgress
//   - hooks/useDownloader.js — instantiates this and re-exports it as
//                         `updateCheck`
//   - components/LibraryTab.jsx — consumes it; owns only the panel's
//                         open/closed flag
//   - components/UpdatesCenter.jsx — renders the rows
// ============================================================

import { useState, useEffect, useCallback, useMemo, useRef } from "react";

const hasAPI = () => typeof window !== "undefined" && !!window.electronAPI;

// Frozen because it is handed straight to setScanStats as a value (not a
// copy) on every reset — a mutation would corrupt the reset itself.
const IDLE_STATS = Object.freeze({ completed: 0, total: 0, durationMs: 0, aborted: false });

// What a row's "+N new" badge counts: new chapters plus new volumes (a
// series that tracks volumes reports them as row.newVolumes; electron/
// update-check-record.js). One definition so the sweep, the snapshot and a
// local cross-out can't disagree.
function rowNewCount(row) {
  return row && row.state === "found"
    ? (row.newChapters?.length || 0) + (row.newVolumes?.length || 0)
    : 0;
}

export function useUpdateCheck({ setLibraryEntries }) {
  // Map<folderPath, row>. Rows are stored verbatim as main built them; see
  // preload.js:onUpdateCheckProgress for the shape. Immutable-on-write (a
  // fresh Map per event) because UpdatesCenter memoizes its grouping on the
  // Map identity — the previous ref+version-counter dance copied the whole
  // Map on every bump anyway, so this costs the same and reads honestly.
  const [rows, setRows] = useState(() => new Map());
  const [scanState, setScanState] = useState("idle"); // "idle" | "running" | "done"
  const [scanStats, setScanStats] = useState(IDLE_STATS);
  // folderPath → new-chapter count. Drives the orange "+N new" badge on the
  // grid cards, so it has to survive the tab switch alongside the rows.
  const [newChapterCounts, setNewChapterCounts] = useState({});

  // runId of the sweep we're mirroring. 0 = none seen yet. A different id on
  // an incoming event means main started a new sweep, so the previous run's
  // rows are dropped wholesale.
  const runIdRef = useRef(0);
  const scanStateRef = useRef(scanState);
  scanStateRef.current = scanState;
  // Latest committed rows, so setRowIgnored can read the row it is patching
  // without taking `rows` as a dependency (which would re-create the callback
  // on every progress event and re-render every chapter chip in the panel).
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  // Keeps applyEvent's identity stable while still reaching the current
  // library setter (the splice below writes fresh site metadata into it).
  const setLibraryEntriesRef = useRef(setLibraryEntries);
  setLibraryEntriesRef.current = setLibraryEntries;

  // ── Snapshot adoption ──
  // Plain overwrite, never a merge: the buffering below guarantees no live
  // event has been applied yet when this runs, so there is nothing newer to
  // preserve.
  const adoptSnapshot = useCallback((snap) => {
    if (!snap || typeof snap !== "object") return;
    runIdRef.current = snap.runId || 0;
    const map = new Map();
    const counts = {};
    for (const row of snap.rows || []) {
      if (!row || !row.folderPath) continue;
      map.set(row.folderPath, row);
      const n = rowNewCount(row);
      if (n > 0) counts[row.folderPath] = n;
    }
    setRows(map);
    setNewChapterCounts(counts);
    setScanState(snap.state === "running" ? "running" : "done");
    setScanStats({
      completed: snap.completed || 0,
      total: snap.total || 0,
      durationMs: snap.durationMs || 0,
      aborted: !!snap.aborted,
    });
  }, []);

  const applyEvent = useCallback((event) => {
    if (!event || typeof event !== "object") return;

    // A runId we haven't seen = a sweep started (by us, or by a Rescan that
    // preempted the previous one). Drop the old run's rows so a 30-series
    // scan doesn't render on top of the 12 rows of the one it replaced.
    // Badges deliberately survive: a series stays "+N new" until this run
    // proves otherwise on its own "completed" event.
    if (typeof event.runId === "number" && event.runId !== runIdRef.current) {
      runIdRef.current = event.runId;
      setRows(new Map());
      setScanState("running");
      setScanStats({ ...IDLE_STATS, total: event.total || 0 });
    }

    if (event.kind === "done") {
      setScanState("done");
      setScanStats({
        completed: event.completed || 0,
        total: event.total || 0,
        durationMs: event.durationMs || 0,
        aborted: !!event.aborted,
      });
      return;
    }

    const row = event.row;
    if (!row || !row.folderPath) return;
    setRows((prev) => new Map(prev).set(row.folderPath, row));

    if (typeof event.completed === "number") {
      setScanStats((s) => ({
        ...s,
        completed: event.completed,
        total: typeof event.total === "number" ? event.total : s.total,
      }));
    }

    if (event.kind !== "completed") return;

    // Badge bookkeeping. The DELETE branch matters as much as the set: now
    // that badges survive tab switches, a series that was "+3 new" and is
    // up to date on the next sweep has to lose its badge, or the fix that
    // made state persistent would just make staleness persistent.
    setNewChapterCounts((prev) => {
      const n = rowNewCount(row);
      if (n > 0) {
        return prev[row.folderPath] === n ? prev : { ...prev, [row.folderPath]: n };
      }
      if (!(row.folderPath in prev)) return prev;
      const next = { ...prev };
      delete next[row.folderPath];
      return next;
    });

    // Splice fresh site metadata (status / authors / cover / genres) into
    // the library entries so the grid card + detail view reflect the live
    // check without a manual Refresh. Only overwrite fields the check
    // actually populated — never drop chapters_downloaded etc.
    const updatedMeta = event.updatedMeta;
    if (updatedMeta && setLibraryEntriesRef.current) {
      setLibraryEntriesRef.current((entries) => {
        if (!Array.isArray(entries)) return entries;
        return entries.map((e) => {
          if (e.folderPath !== row.folderPath) return e;
          const merged = { ...e.seriesMeta };
          for (const [k, v] of Object.entries(updatedMeta)) {
            if (v !== undefined && v !== null) merged[k] = v;
          }
          return { ...e, seriesMeta: merged };
        });
      });
    }
  }, []);

  // ── Subscribe + hydrate (once, for the app's lifetime) ──
  useEffect(() => {
    if (!hasAPI() || !window.electronAPI.onUpdateCheckProgress) return undefined;

    let disposed = false;
    let hydrated = false;
    const pending = [];

    const unsub = window.electronAPI.onUpdateCheckProgress((event) => {
      if (disposed) return;
      if (!hydrated) {
        pending.push(event);
        return;
      }
      applyEvent(event);
    });

    const drain = () => {
      hydrated = true;
      if (disposed) return;
      const queued = pending.splice(0, pending.length);
      for (const event of queued) applyEvent(event);
    };

    // Adopt a sweep this renderer never saw start. Null result = no sweep
    // has run this session, which is also the answer on an older main.js
    // without the handler (the optional-call below resolves undefined).
    const snapshot = window.electronAPI.getUpdateCheckState?.();
    if (snapshot && typeof snapshot.then === "function") {
      snapshot
        .then((snap) => {
          if (!disposed && snap) adoptSnapshot(snap);
        })
        .catch((err) => console.error("Update-check state hydration failed:", err))
        .finally(drain);
    } else {
      drain();
    }

    return () => {
      disposed = true;
      if (unsub) unsub();
    };
  }, [applyEvent, adoptSnapshot]);

  // ── Start a sweep ──
  // `force` is the explicit-rescan gesture (the panel's Rescan button). Any
  // other caller gets a no-op while a sweep is live — restarting a scan the
  // user can watch running is never what they meant by clicking a button
  // that says "Check All".
  const start = useCallback(async ({ force = false } = {}) => {
    if (!hasAPI() || !window.electronAPI.checkAllUpdates) return null;
    if (!force && scanStateRef.current === "running") {
      return { status: "already-running" };
    }
    // Optimistic reset so the panel switches to "preparing scan…" on the
    // click rather than after the library re-scan main does first.
    runIdRef.current = 0;
    setRows(new Map());
    setScanState("running");
    setScanStats(IDLE_STATS);
    try {
      const result = await window.electronAPI.checkAllUpdates({ force });
      // Main refused because a sweep is already live (our local guard can
      // miss it: another window, or a run started before this renderer).
      // Its snapshot is the truth — adopt it instead of showing an empty
      // "preparing scan…" forever.
      if (result?.status === "already-running" && result.snapshot) {
        adoptSnapshot(result.snapshot);
      }
      return result;
    } catch (err) {
      console.error("Check all updates failed:", err);
      setScanState("done");
      return null;
    }
  }, [adoptSnapshot]);

  const cancel = useCallback(async () => {
    if (!hasAPI() || !window.electronAPI.cancelCheckAllUpdates) return;
    try {
      await window.electronAPI.cancelCheckAllUpdates();
    } catch (err) {
      console.error("Cancel check-all failed:", err);
    }
  }, []);

  // ── Mark rows the user has actioned (queued or dismissed) ──
  // The row drops to "uptodate" rather than vanishing, so the Updates Found
  // section shrinks while the series still shows the action took effect.
  const resolveRows = useCallback((folderPaths) => {
    const paths = Array.isArray(folderPaths) ? folderPaths : [folderPaths];
    if (paths.length === 0) return;
    setRows((prev) => {
      let changed = false;
      const next = new Map(prev);
      for (const p of paths) {
        const row = next.get(p);
        if (!row || row.state !== "found") continue;
        // Queue / dismiss covers both kinds the row offered.
        next.set(p, { ...row, state: "uptodate", newChapters: undefined, newVolumes: undefined });
        changed = true;
      }
      return changed ? next : prev;
    });
    setNewChapterCounts((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const p of paths) {
        if (p in next) {
          delete next[p];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, []);

  // ── Cross out / restore individual chapters ──
  // Persists to .aio_series.json:chapters_ignored through main (which does the
  // read-modify-write, so two fast clicks can't clobber each other), then moves
  // the chapters between the row's two lists and re-derives its state.
  //
  // AWAIT FIRST, PATCH SECOND — deliberately not optimistic. The write is a
  // local JSON file, so the delay is imperceptible, and the alternative is a
  // row that renders a chapter struck through while the file that decides what
  // the NEXT check reports says otherwise.
  //
  // The row is rebuilt with the same conditional-spread shape
  // electron/update-check-record.js:resultRow uses (empty list ⇒ key absent),
  // so a locally-patched row and one that arrives from a later sweep are
  // indistinguishable.
  //
  // `kind` "volume" crosses out VOLUMES: it writes volumes.ignored and moves
  // labels between the row's newVolumes / ignoredVolumes instead. The chapter
  // lists are never touched by a volume call, and vice versa.
  const setRowIgnored = useCallback(async (folderPath, chapters, ignored, kind = "chapter") => {
    if (!hasAPI() || !window.electronAPI.setChaptersIgnored) return { ok: false };
    const list = (Array.isArray(chapters) ? chapters : [chapters]).map(String);
    if (list.length === 0) return { ok: true };
    const isVolume = kind === "volume";
    const newKey = isVolume ? "newVolumes" : "newChapters";
    const ignoredKey = isVolume ? "ignoredVolumes" : "ignoredChapters";

    let res;
    try {
      res = await window.electronAPI.setChaptersIgnored(folderPath, list, !!ignored, kind);
    } catch (err) {
      console.error("Cross out chapters failed:", err);
      return { ok: false, error: err?.message };
    }
    if (!res?.ok) return res || { ok: false };

    // Derived ONCE, off the ref, and then handed to both setState calls. The
    // badge count has to agree with the row exactly, and re-deriving it inside
    // a second updater would read the rows Map at an unspecified point in the
    // commit. A progress event landing on this same series in the gap would
    // lose the patch, but it carries its own row and count and so corrects
    // both in the same breath.
    // ONLY these two states carry chapter lists. A queued / running / error row
    // has none, so re-deriving its state from an empty list would rewrite a
    // failed check as "uptodate" — the detail view can cross a chapter out for
    // a series whose panel row is mid-check or errored, and that must not
    // launder the error away. Those rows get their real state from the sweep.
    const row = rowsRef.current.get(folderPath);
    if (row && (row.state === "found" || row.state === "uptodate")) {
      const moved = new Set(list);
      const byNumber = (a, b) => parseFloat(a) - parseFloat(b);
      const wasNew = row[newKey] || [];
      const wasIgnored = row[ignoredKey] || [];
      // Filter-the-source / concat-into-the-target, so clicking a chapter the
      // row already has on the target side is a no-op rather than a duplicate.
      const nextNew = ignored
        ? wasNew.filter((c) => !moved.has(c))
        : [...wasNew, ...wasIgnored.filter((c) => moved.has(c))].sort(byNumber);
      const nextIgnored = ignored
        ? [...wasIgnored, ...wasNew.filter((c) => moved.has(c))].sort(byNumber)
        : wasIgnored.filter((c) => !moved.has(c));

      const patched = { ...row };
      delete patched[newKey];
      delete patched[ignoredKey];
      if (nextNew.length > 0) patched[newKey] = nextNew;
      if (nextIgnored.length > 0) patched[ignoredKey] = nextIgnored;
      // Found while EITHER kind still has something new.
      const anyNew = (patched.newChapters?.length || 0) + (patched.newVolumes?.length || 0) > 0;
      patched.state = anyNew ? "found" : "uptodate";
      setRows((prev) => new Map(prev).set(folderPath, patched));

      // The grid badge counts what is actually downloadable, so crossing out
      // the last new chapter must clear it exactly as an up-to-date sweep does.
      setNewChapterCounts((prev) => {
        const n = rowNewCount(patched);
        if (n > 0) return prev[folderPath] === n ? prev : { ...prev, [folderPath]: n };
        if (!(folderPath in prev)) return prev;
        const next = { ...prev };
        delete next[folderPath];
        return next;
      });
    }

    // Mirror onto the library entry so the detail view's own update-check
    // agrees with the panel without a library rescan. main returned the full
    // new list, so this is an assignment, not another merge — but only when it
    // actually sent one: treating a missing field as "the list is now empty"
    // would let an older main.js silently wipe the entry's cross-outs.
    if (isVolume && setLibraryEntriesRef.current && Array.isArray(res.ignoredItems)) {
      setLibraryEntriesRef.current((entries) => {
        if (!Array.isArray(entries)) return entries;
        return entries.map((e) => {
          if (e.folderPath !== folderPath) return e;
          // Same rule as main's write (series-ledger.js applyIgnored): an
          // empty list never creates the block, which would mean "tracks
          // volumes".
          if (!e.seriesMeta?.volumes && res.ignoredItems.length === 0) return e;
          const meta = { ...e.seriesMeta };
          const block = { ...(meta.volumes || { schema: 1, downloaded: [] }) };
          if (res.ignoredItems.length > 0) block.ignored = res.ignoredItems;
          else delete block.ignored;
          meta.volumes = block;
          return { ...e, seriesMeta: meta };
        });
      });
    } else if (!isVolume && setLibraryEntriesRef.current && Array.isArray(res.chaptersIgnored)) {
      setLibraryEntriesRef.current((entries) => {
        if (!Array.isArray(entries)) return entries;
        return entries.map((e) => {
          if (e.folderPath !== folderPath) return e;
          const meta = { ...e.seriesMeta };
          if (res.chaptersIgnored.length > 0) meta.chapters_ignored = res.chaptersIgnored;
          else delete meta.chapters_ignored;
          return { ...e, seriesMeta: meta };
        });
      });
    }

    return res;
  }, []);

  const foundCount = useMemo(() => {
    let n = 0;
    for (const row of rows.values()) if (row.state === "found") n += 1;
    return n;
  }, [rows]);

  return useMemo(
    () => ({
      rows,
      scanState,
      scanStats,
      newChapterCounts,
      foundCount,
      start,
      cancel,
      resolveRows,
      setRowIgnored,
    }),
    [
      rows, scanState, scanStats, newChapterCounts, foundCount,
      start, cancel, resolveRows, setRowIgnored,
    ]
  );
}

export default useUpdateCheck;
