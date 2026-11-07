// ============================================================
// CHECK-ALL SWEEP RECORD (main-process, canonical)
//
// Owns the authoritative state of the library "Check All" update sweep: which
// run is live, every row it has produced, and the progress counters.
// electron/main.js's check-all-updates handler drives it; the
// get-update-check-state IPC serves its snapshot.
//
// WHY IT EXISTS: the sweep runs in the main process and keeps running when
// the renderer's Library tab unmounts — which App.jsx does on every tab
// switch. Before this, the renderer held the ONLY copy of the scan, so a tab
// switch destroyed it and the next "Check All" click aborted the live sweep
// and restarted from zero. The renderer is a mirror now
// (src/hooks/useUpdateCheck.js); this is the record it re-reads.
//
// TWO INVARIANTS THIS MODULE EXISTS TO HOLD:
//   1. ONE reducer. Rows are derived here (resultRow) and shipped whole, so a
//      snapshot handed to a remounting tab and a live event can never
//      disagree about a series.
//   2. ONE live run. Every emission is stamped with a runId and DROPPED when
//      it belongs to a superseded run — a forced rescan preempts workers that
//      are still awaiting Python, and their late "completed"/"done" events
//      would otherwise land in the new run's rows and flip it to done at ~0%.
//
// NOT PERSISTED: the sweep dies with the process, so a record that outlived
// it could only ever describe a scan nothing is driving.
//
// Offline regression: tools/_test_update_check_record.js
// ============================================================

// Derive the panel row from one per-series check result.
//
// `base` carries the identity/display fields the handler already knows
// (folderPath, title, cover, site, enqueuedAt) and is rebuilt per event
// rather than inherited from the previous row — that is what stops an
// "uptodate" row from still carrying the newChapters of a "found" one.
//
// `r` is the _checkSeriesUpdates result: { error } | { ok, newChapters,
// ignoredChapters, total, ... }. Cross-file: main.js:_checkSeriesUpdates
// builds it.
//
// IGNORED CHAPTERS RIDE BOTH NON-ERROR BRANCHES. They are chapters the user
// crossed out, so they must never count toward "found" — a series whose only
// missing chapters are crossed out IS up to date, and the panel's badge, its
// section count and "Queue all" all key off that state. But the row still
// carries them in either state, because the panel renders them struck through
// and the undo lives on that same row: dropping them from the uptodate branch
// would make a fully-crossed-out series impossible to restore.
function resultRow(base, r) {
  const result = r || {};
  if (result.error === "aborted") {
    return { ...base, state: "error", error: "aborted", errorMessage: "cancelled" };
  }
  if (result.error) {
    return {
      ...base,
      state: "error",
      error: result.error,
      errorMessage: result.message || result.error,
    };
  }
  // Spread-when-non-empty, matching how the branches below omit newChapters:
  // an absent key and an empty array mean the same thing to every reader, and
  // omitting keeps a plain up-to-date row as small as it has always been.
  const ignored =
    result.ignoredChapters && result.ignoredChapters.length > 0
      ? { ignoredChapters: result.ignoredChapters }
      : null;
  // Volume half (main.js _checkSeriesUpdates, series that track volumes).
  // Same spread-when-present rule, so a chapter-only row is unchanged. A row
  // is "found" when EITHER kind has new items; the panel shows each kind.
  const hasNewVolumes = !!(result.newVolumes && result.newVolumes.length > 0);
  const volumes = Array.isArray(result.trackedKinds) && result.trackedKinds.includes("volume")
    ? {
        trackedKinds: result.trackedKinds,
        ...(hasNewVolumes ? { newVolumes: result.newVolumes } : {}),
        ...(result.ignoredVolumes && result.ignoredVolumes.length > 0
          ? { ignoredVolumes: result.ignoredVolumes }
          : {}),
        volumesTotal: result.volumesTotal ?? 0,
        ...(result.volumesError ? { volumesError: result.volumesError } : {}),
      }
    : null;
  if ((result.newChapters && result.newChapters.length > 0) || hasNewVolumes) {
    return {
      ...base,
      state: "found",
      ...(result.newChapters && result.newChapters.length > 0
        ? { newChapters: result.newChapters }
        : {}),
      ...ignored,
      ...volumes,
      total: result.total,
    };
  }
  return { ...base, state: "uptodate", ...ignored, ...volumes, total: result.total };
}

// `send(channel, payload)` is injected (main.js passes sendToUI) so this
// module stays free of electron and can be driven by the offline test.
function createUpdateCheckRecord(send) {
  let run = null;
  let seq = 0;

  return {
    // True only while a sweep is actually in flight. main.js's re-entrancy
    // guard reads this: a plain check-all call against a live sweep is
    // refused and answered with snapshot() instead of restarting.
    isRunning() {
      return !!run && run.state === "running";
    },

    runId() {
      return run ? run.runId : 0;
    },

    // Serializable view for the get-update-check-state IPC and for the
    // refusal payload. Rows go over as an array so the wire shape is
    // identical whether the renderer got a row from here or from event.row.
    // Null when no sweep has run this session.
    snapshot() {
      if (!run) return null;
      return {
        runId: run.runId,
        state: run.state,
        completed: run.completed,
        total: run.total,
        durationMs: run.durationMs,
        aborted: run.aborted,
        startedAt: run.startedAt,
        rows: Array.from(run.rows.values()),
      };
    },

    // Open a run, superseding any previous one. From here on, emissions
    // carrying an older runId are dropped (invariant 2).
    begin(total) {
      seq += 1;
      run = {
        runId: seq,
        state: "running",
        startedAt: Date.now(),
        durationMs: 0,
        aborted: false,
        completed: 0,
        total: Number(total) || 0,
        rows: new Map(),
      };
      return { runId: run.runId, startedAt: run.startedAt };
    },

    // Fold one progress event into the record, then forward it with its
    // runId attached. Dropping stale emissions HERE (rather than filtering
    // them in the renderer) keeps exactly one place deciding what is live.
    emit(runId, event) {
      if (!run || run.runId !== runId || !event) return false;
      if (event.row && event.row.folderPath) {
        run.rows.set(event.row.folderPath, event.row);
      }
      if (typeof event.completed === "number") run.completed = event.completed;
      if (typeof event.total === "number") run.total = event.total;
      if (event.kind === "done") {
        run.state = "done";
        run.durationMs = event.durationMs || 0;
        run.aborted = !!event.aborted;
      }
      send("update-check-progress", { ...event, runId });
      return true;
    },

    // The row this run last published for `folderPath`; null once the run is
    // superseded. Used to carry enqueuedAt forward so the panel's "checking"
    // section keeps its FIFO order.
    rowFor(runId, folderPath) {
      if (!run || run.runId !== runId) return null;
      return run.rows.get(folderPath) || null;
    },
  };
}

module.exports = { createUpdateCheckRecord, resultRow };
