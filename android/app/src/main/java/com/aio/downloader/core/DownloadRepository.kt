package com.aio.downloader.core

import android.content.Context
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONObject
import java.util.UUID

/**
 * The process-wide download state: what is running, what is waiting, what
 * finished.
 *
 * ── THE ONE RULE THIS FILE EXISTS TO ENFORCE ───────────────────────────────
 * `aio_android.poll_events()` DRAINS the Python-side queue. Two callers polling
 * it would each receive a random half of the events, and both would render
 * nonsense — the notification stuck three chapters behind, the Queue screen
 * skipping every other tick. So **DownloadService is the sole poller**, it
 * feeds [applyEvent] here, and the UI only ever READS these flows. Nothing in
 * `ui/` may call poll_events.
 *
 * ── AND THE ONE CONSTRAINT IT INHERITS ─────────────────────────────────────
 * Downloads are SERIAL. aio-dl.py keeps per-run state in module-level globals
 * (`_HOST_CONCURRENCY_CAP`, `_image_prefetch_queue`, `_RATE_LIMIT_SCHEDULE`)
 * and Chaquopy runs one interpreter per process, so two concurrent runs would
 * corrupt each other's backoff and prefetch state. The queue below is how that
 * constraint becomes a feature instead of a rejection: the desktop's
 * DownloadService simply refused a second request, which on a phone reads as
 * "the button does nothing".
 *
 * An `object`, not a ViewModel: the service outlives every Activity, and this
 * state has to survive the UI being destroyed mid-download.
 *
 * ── IT NOW SURVIVES PROCESS DEATH TOO, AND WHAT THAT DOES *NOT* MEAN ───────
 * The queue and the history are persisted through [RunStore] and restored by
 * [attach]. The header here used to say persistence was pointless because "a
 * killed process has no run to resume" — half right. A run that STARTED is
 * recoverable through `tmp_<hid>/` and the Unfinished section, but a job that
 * was only ever QUEUED existed nowhere except this object, and Android killing
 * the app for memory silently threw it away.
 *
 * Restoring is NOT resuming. `DownloadService` still returns START_NOT_STICKY,
 * nothing auto-starts, and the Queue screen offers an explicit Start — silently
 * resuming a multi-hundred-megabyte download on someone's mobile data is the
 * behaviour that rule exists to prevent, and putting the queue back on screen
 * does not require breaking it.
 */
object DownloadRepository {

    private const val TAG = "AioRepo"

    /**
     * Completed/failed/cancelled runs kept on disk. Matches the desktop's cap
     * (`UI-source/electron/history.js`, `slice(0, 200)`), which is sized for the
     * resumable back-fill rather than for the list — see [backfillResumable].
     * The Queue screen shows only the newest handful.
     */
    private const val HISTORY_LIMIT = 200

    /** Same cap on the queue snapshot, for the same reason the desktop has one
     *  (`QUEUE_PERSIST_LIMIT` in useDownloader.js): a runaway blob in a
     *  synchronously-read preferences file would slow every process start. */
    private const val QUEUE_PERSIST_LIMIT = 200

    private val _queue = MutableStateFlow<List<DownloadJob>>(emptyList())
    val queue: StateFlow<List<DownloadJob>> = _queue.asStateFlow()

    private val _active = MutableStateFlow<ActiveRun?>(null)
    val active: StateFlow<ActiveRun?> = _active.asStateFlow()

    private val _history = MutableStateFlow<List<RunRecord>>(emptyList())
    val history: StateFlow<List<RunRecord>> = _history.asStateFlow()

    /**
     * Ids of the jobs that came back from the last session, so the Queue screen
     * can say so. A plain immutable val, not a flow: it is written once inside
     * [attach] — which runs in `MainActivity.onCreate`, before any composition —
     * and never changes again, so there is nothing for Compose to observe.
     */
    @Volatile
    var restoredJobIds: Set<String> = emptySet()
        private set

    /**
     * Set while the service's worker loop is alive. Guarded by [lock] together
     * with the queue so the loop cannot decide "queue empty, I'm done" in the
     * same instant an enqueue decides "a worker is already running".
     */
    private var workerActive = false
    private val lock = Any()

    /** Null until [attach]; every persist call is a no-op without it. */
    private var store: RunStore? = null

    /**
     * The job handed to the worker but not yet (or no longer) an [ActiveRun].
     *
     * Written inside the SAME `synchronized(lock)` step that removes the job
     * from the queue, because that is the window the persistence has to cover:
     * `takeNextOrRelease` drops the head, and only afterwards does the service
     * call [beginRun]. Persisting on `beginRun` instead would leave a real
     * interval in which the job is in neither the saved queue nor the saved
     * active slot, and a kill there loses it outright.
     */
    private var persistedActive: DownloadJob? = null

    /**
     * `hid` from the run's `series` event, and the `final_file_skipped` payload
     * if one arrived. Both are folded into the history record at [finishRun].
     *
     * Volatile because they cross threads: [applyEvent] runs on
     * DownloadService's poller thread, [finishRun] on its worker thread, and
     * the join between them is bounded by a timeout rather than unconditional.
     */
    @Volatile private var runHid: String = ""
    @Volatile private var runFinalFileSkip: FinalFileSkip? = null

    // ── process start ─────────────────────────────────────────────────────

    /**
     * Install the store and restore the last session's queue and history.
     *
     * Idempotent, and called from BOTH process entry points —
     * `MainActivity.onCreate` and `DownloadService.onCreate` — because either
     * can be the first thing to touch this object. Synchronous, and on the main
     * thread from the Activity: it is a single preferences read of a few KB,
     * and it MUST complete before anything enqueues, or a restore landing later
     * would stomp a job the user just queued.
     *
     * ── WHY A RUN THAT WAS ACTIVE GOES BACK IN THE QUEUE ──────────────────
     * It cannot still be running (the process died), and it is NOT re-queued as
     * a fresh download by accident: aio-dl.py finds its own `tmp_<hid>/`, hashes
     * this run's gating parameters against the saved ones and, on a match, sets
     * `resume_mode` itself — with no `--restore-parameters` involved (grep
     * `resume_mode` in aio-dl.py). The settings blob is the one the job carried
     * when it was queued, so the hashes match and every finished chapter is
     * reused. Where the run died before writing `run_params.json` there is
     * nothing on disk to resume from, and this restore is the ONLY thing that
     * still knows the job existed.
     *
     * `ResumeRepository.visible` filters the Unfinished list by queued URL, so a
     * restored job and its own tmp folder can never both be offered.
     */
    fun attach(context: Context) {
        synchronized(lock) {
            if (store != null) return
            val opened = RunStore(context)
            store = opened

            _history.value = opened.loadHistory()

            val snapshot = opened.loadQueue()
            if (snapshot.isEmpty) return
            // Ordering and de-duplication live in restoredQueue, which is pure
            // and unit-tested; this holds the lock and publishes the result.
            val existing = _queue.value
            val revived = restoredQueue(snapshot, existing)
            _queue.value = revived
            restoredJobIds = revived.asSequence()
                .filterNot { job -> existing.any { it.id == job.id } }
                .mapTo(HashSet()) { it.id }
            Log.i(TAG, "restored ${restoredJobIds.size} queued job(s) from the last session")
        }
    }

    // ── enqueue side (called from the UI / the intent harness) ─────────────

    /**
     * Queue [job]. Returns false if this URL is already queued or running —
     * double-tapping Start must not download the same series twice.
     */
    fun enqueue(job: DownloadJob): Boolean = synchronized(lock) {
        val alreadyQueued = _queue.value.any { it.url == job.url }
        val alreadyRunning = _active.value?.job?.url == job.url
        if (alreadyQueued || alreadyRunning) {
            Log.i(TAG, "duplicate enqueue ignored: ${job.url}")
            return false
        }
        _queue.value = _queue.value + job
        persistQueueLocked()
        true
    }

    fun removeQueued(id: String) = synchronized(lock) {
        _queue.value = _queue.value.filterNot { it.id == id }
        persistQueueLocked()
    }

    /**
     * Forget every finished run. This ALSO drops the URLs
     * [backfillResumable] leans on, which is the honest behaviour: the history
     * is where those URLs came from, and a Clear that quietly kept a hidden
     * copy would be the kind of thing a user is entitled to be annoyed about.
     */
    fun clearHistory() {
        synchronized(lock) {
            _history.value = emptyList()
            store?.saveHistory(emptyList())
        }
    }

    // ── worker side (called ONLY from DownloadService) ─────────────────────

    /**
     * Claim the single worker slot. Returns false when one is already running,
     * in which case the caller must NOT start a loop — the running one will
     * pick the new job up on its next iteration.
     */
    fun claimWorker(): Boolean = synchronized(lock) {
        if (workerActive) return false
        workerActive = true
        true
    }

    /**
     * Next job, or null — and null ALSO releases the worker slot, atomically.
     * Splitting those two into separate calls is what would let a job enqueued
     * between them sit in the queue forever with no worker to run it.
     *
     * The same step records the job as [persistedActive]; see that field.
     */
    fun takeNextOrRelease(): DownloadJob? = synchronized(lock) {
        val next = _queue.value.firstOrNull()
        if (next == null) {
            workerActive = false
            return null
        }
        _queue.value = _queue.value.drop(1)
        persistedActive = next
        persistQueueLocked()
        next
    }

    /** Emergency release for the worker's `finally` — a crash must not wedge the queue. */
    fun releaseWorker() = synchronized(lock) { workerActive = false }

    fun beginRun(job: DownloadJob) {
        runHid = ""
        runFinalFileSkip = null
        _active.value = ActiveRun(job)
    }

    /**
     * Fold one aio-dl.py `_emit` payload into [active].
     *
     * Field names come from aio-dl.py's `_emit(` calls — grep that. Two of them
     * have already cost a debugging session apiece:
     *  - `chapters_selected` carries **total**, not `count`. Reading the wrong
     *    key leaves the total at 0, which silently disables both the "x/y" text
     *    AND the determinate progress bar.
     *  - Chapter counting keys off `chapter_start`, not `chapter_saved`, because
     *    `chapter_saved` only fires for cbz/pdf — an EPUB run saves nothing per
     *    chapter and its bar would never move.
     *
     * Unknown kinds are ignored by design: aio-dl.py gains events over time and
     * an older UI must not break on one it has never seen.
     */
    fun applyEvent(e: JSONObject) {
        val run = _active.value ?: return
        val p = run.progress
        val next = when (e.optString("kind")) {
            "series" -> {
                // The hid is the only key that ties this run to its `tmp_<hid>/`
                // folder, which is what lets a folder with no run_meta.json be
                // matched back to a URL later — see backfillResumable.
                e.optString("hid").takeIf { it.isNotBlank() }?.let { runHid = it }
                p.copy(title = e.optString("title").ifBlank { p.title })
            }

            "chapters_selected" -> p.copy(total = e.optInt("total", p.total))

            "chapter_start" -> p.copy(
                chapter = e.optString("chapter").ifBlank { p.chapter },
                // `processed` counts chapters STARTED, the in-flight one
                // included — the same definition the desktop's
                // processedChapters uses, and the one the ETA was computed
                // against. A retried chapter therefore ticks twice; that is
                // correct, not a double-count.
                processed = e.optInt("processed", p.processed + 1),
                total = e.optInt("total", p.total),
                etaMs = if (e.isNull("eta_ms")) null else e.optLong("eta_ms"),
                etaSamples = e.optInt("eta_samples", p.etaSamples),
            )

            "phase" -> p.copy(phase = e.optString("phase").ifBlank { p.phase })

            // aio-dl.py declined to rebuild the combined archive. Held aside
            // rather than shown live: it arrives in the last seconds of a run,
            // and the place it has to be READABLE is the terminal notification
            // and the history card, both of which outlive the active card.
            "final_file_skipped" -> {
                parseFinalFileSkip(e)?.let { runFinalFileSkip = it }
                p
            }

            "done" -> p.copy(phase = "done")

            else -> p
        }
        if (next !== p) _active.value = run.copy(progress = next)
    }

    /**
     * Retire the running job into history, and hand the caller the record it
     * wrote — DownloadService renders the terminal notification straight from
     * it rather than re-reading global state that a second run could already
     * have replaced.
     *
     * [exitCode] 130 is `aio_android.CANCELLED_EXIT_CODE`. It WINS over a real
     * failure code, which can mask a genuine error — accepted deliberately,
     * because the per-chapter reasons still land in the skipped-chapters report
     * and the missed-chapters JSON, so no diagnosis is actually lost.
     */
    fun finishRun(exitCode: Int): RunRecord? {
        val run = _active.value
        if (run == null) {
            // Nothing was running, but the worker slot may still hold a job it
            // never managed to begin. Clearing it here keeps the saved snapshot
            // from re-queueing a job that is already gone.
            synchronized(lock) {
                persistedActive = null
                persistQueueLocked()
            }
            return null
        }

        val outcome = when (exitCode) {
            0 -> RunOutcome.Completed
            CANCELLED_EXIT_CODE -> RunOutcome.Cancelled
            else -> RunOutcome.Failed
        }
        val record = RunRecord(
            finished = FinishedRun(
                id = run.job.id,
                label = run.label,
                outcome = outcome,
                exitCode = exitCode,
                durationMs = System.currentTimeMillis() - run.startedAt,
                processed = run.progress.processed,
                total = run.progress.total,
            ),
            url = run.job.url,
            hid = runHid,
            finalFileSkip = runFinalFileSkip,
        )

        synchronized(lock) {
            persistedActive = null
            val records = (listOf(record) + _history.value).take(HISTORY_LIMIT)
            _history.value = records
            persistQueueLocked()
            store?.saveHistory(records)
        }
        _active.value = null
        runHid = ""
        runFinalFileSkip = null
        return record
    }

    /** Caller must hold [lock]. */
    private fun persistQueueLocked() {
        store?.saveQueue(
            QueueSnapshot(
                active = persistedActive,
                queue = _queue.value.take(QUEUE_PERSIST_LIMIT),
                savedAt = System.currentTimeMillis(),
            ),
        )
    }

    /** Mirrors `aio_android.CANCELLED_EXIT_CODE`. */
    const val CANCELLED_EXIT_CODE = 130

    fun newJobId(): String = UUID.randomUUID().toString()
}
