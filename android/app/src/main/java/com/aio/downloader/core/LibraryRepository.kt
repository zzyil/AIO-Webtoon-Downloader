package com.aio.downloader.core

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext

/**
 * The library's state: what is on disk, and what each series' last update check
 * found.
 *
 * An `object` for the same reason [DownloadRepository] is one — a whole-library
 * sweep takes minutes and must survive the Activity being destroyed, and a scan
 * result should still be there when the user comes back to the tab rather than
 * costing a fresh disk walk every time.
 *
 * ── EVERY CALL HERE CROSSES INTO PYTHON, SO EVERY CALL IS OFF THE MAIN THREAD ─
 * The scan walks the whole library and opens archives; an update check runs
 * aio-dl.py end to end against a live site. Both are seconds, not milliseconds.
 * The suspend functions confine themselves to Dispatchers.IO; the sweep gets its
 * own scope because it outlives any composition.
 *
 * ── THE CONSTRAINT THIS INHERITS ───────────────────────────────────────────
 * An update check RUNS THE ENGINE (`--list-chapters` is a full aio-dl.py run),
 * and aio-dl.py keeps per-run state in module globals — including the
 * cancellation flag, which every run clears on entry. So a check started during
 * a download would un-cancel a download the user just cancelled.
 * `aio_android._ENGINE_LOCK` is the hard guarantee and returns [ENGINE_BUSY]
 * rather than blocking; [busyWithDownload] is the soft one, so the UI can say so
 * before the user taps rather than after. Both exist on purpose: the soft check
 * is a race (a download can start between the check and the call), and the hard
 * one is what makes losing that race harmless.
 *
 * Cross-file: aio_android.py (scan_library / library_cover / series_files /
 * check_series_updates / delete_series), CoverStore.kt, LibraryScreen.kt.
 */
object LibraryRepository {

    private const val TAG = "AioLibrary"

    /** Mirrors `aio_android.ENGINE_BUSY`. */
    const val ENGINE_BUSY = "engine_busy"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** null until the first scan completes — distinct from "scanned, and empty". */
    private val _series = MutableStateFlow<List<LibrarySeries>?>(null)
    val series: StateFlow<List<LibrarySeries>?> = _series.asStateFlow()

    private val _loading = MutableStateFlow(false)
    val loading: StateFlow<Boolean> = _loading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    /** folder -> the last check's result. Survives tab switches; not persisted. */
    private val _updates = MutableStateFlow<Map<String, UpdateResult>>(emptyMap())
    val updates: StateFlow<Map<String, UpdateResult>> = _updates.asStateFlow()

    private val _sweep = MutableStateFlow<SweepState?>(null)
    val sweep: StateFlow<SweepState?> = _sweep.asStateFlow()

    /** Folders with a check in flight, so their cards can show a spinner. */
    private val _checking = MutableStateFlow<Set<String>>(emptySet())
    val checking: StateFlow<Set<String>> = _checking.asStateFlow()

    @Volatile
    private var sweepJob: Job? = null

    /** True while a download holds the engine. The soft half of the guard above. */
    fun busyWithDownload(): Boolean = DownloadRepository.active.value != null

    // ── scan ──────────────────────────────────────────────────────────────

    /**
     * Rescan the library. Safe to call on every screen entry: a scan already in
     * flight wins and the call is dropped, so tab-switching cannot stack disk
     * walks on top of each other.
     */
    fun refresh(context: Context) {
        if (_loading.value) return
        _loading.value = true
        val app = context.applicationContext
        scope.launch {
            try {
                val json = Aio.module(app).callAttr("scan_library").toString()
                _series.value = parseLibrary(json)
                _error.value = null
            } catch (t: Throwable) {
                // Chaquopy surfaces the Python traceback as the message, so this
                // is usually the entire diagnosis.
                Log.e(TAG, "library scan failed", t)
                _error.value = t.message?.take(240) ?: t.javaClass.simpleName
                // Leave any previous result standing: a transient failure should
                // not blank a library the user was looking at.
                if (_series.value == null) _series.value = emptyList()
            } finally {
                _loading.value = false
            }
        }
    }

    /** Load a series' file list on demand — the grid never needs it. */
    suspend fun files(context: Context, folder: String): SeriesFiles =
        withContext(Dispatchers.IO) {
            runCatching {
                parseSeriesFiles(
                    Aio.module(context.applicationContext)
                        .callAttr("series_files", folder).toString(),
                )
            }.getOrElse {
                Log.w(TAG, "series_files failed for $folder", it)
                SeriesFiles()
            }
        }

    // ── update checks ─────────────────────────────────────────────────────

    /**
     * Check one series and record the result. Returns it too, for a caller that
     * wants to react immediately.
     *
     * Never throws: a failed check is a RESULT here, not an exception, because
     * every caller displays it and a sweep must not die on one dead host.
     */
    suspend fun checkOne(context: Context, folder: String): UpdateResult {
        _checking.value = _checking.value + folder
        val app = context.applicationContext
        // Read per check, not captured: the setting is a property of how the
        // user wants checking to work, and it can change between two checks of
        // the same sweep.
        val collapseSplits = AppSettingsStore.current(app).collapseSplits
        val result = withContext(Dispatchers.IO) {
            runCatching {
                parseUpdateResult(
                    folder,
                    Aio.module(app)
                        .callAttr("check_series_updates", folder, collapseSplits).toString(),
                )
            }.getOrElse {
                Log.w(TAG, "update check failed for $folder", it)
                UpdateResult(
                    folder = folder,
                    error = "check_failed",
                    message = it.message?.take(200).orEmpty(),
                )
            }
        }
        _checking.value = _checking.value - folder
        _updates.value = _updates.value + (folder to result)
        // A check reports live status; splice it back so a series that finished
        // publishing stops claiming "Ongoing" without waiting for a rescan.
        if (result.ok && result.status.isNotBlank()) {
            _series.value = _series.value?.map {
                if (it.folder == folder && it.status != result.status) {
                    it.copy(
                        status = result.status,
                        facets = it.facets + (
                            FacetGroup.Status to setOfNotNull(
                                facetKey(result.status).ifEmpty { null },
                            )
                            ),
                    )
                } else {
                    it
                }
            }
        }
        return result
    }

    /**
     * Check every series that has a source URL, one at a time.
     *
     * SERIAL BY CONSTRUCTION, not by choice — the desktop runs a 4-slot worker
     * pool, but there each check is its own OS process and here they would all
     * contend for one interpreter behind `_ENGINE_LOCK`. A pool would spend its
     * time queueing and would read as four spinners making no progress.
     *
     * Stops early — rather than grinding through the rest and reporting a wall
     * of identical failures — when a download takes the engine.
     */
    fun startSweep(context: Context) {
        if (sweepJob?.isActive == true) return
        val targets = _series.value.orEmpty().filter { it.checkable }
        if (targets.isEmpty()) {
            _sweep.value = SweepState(running = false, completed = 0, total = 0)
            return
        }

        val app = context.applicationContext
        _sweep.value = SweepState(running = true, completed = 0, total = targets.size)
        sweepJob = scope.launch {
            var completed = 0
            var stopped = ""
            for (target in targets) {
                ensureActive()
                if (busyWithDownload()) {
                    stopped = "Paused — a download is running"
                    break
                }
                _sweep.value = SweepState(
                    running = true,
                    completed = completed,
                    total = targets.size,
                    current = target.name,
                )
                val result = checkOne(app, target.folder)
                completed++
                if (result.error == ENGINE_BUSY) {
                    stopped = "Paused — a download took over"
                    break
                }
            }
            _sweep.value = SweepState(
                running = false,
                completed = completed,
                total = targets.size,
                stoppedBecause = stopped,
            )
        }
        sweepJob?.invokeOnCompletion { cause ->
            // Cancellation skips the assignment above, so land the final state
            // here. Without it a cancelled sweep leaves the header spinning.
            if (cause != null) {
                val last = _sweep.value ?: return@invokeOnCompletion
                _sweep.value = last.copy(running = false, stoppedBecause = "Stopped")
            }
        }
    }

    fun cancelSweep() {
        sweepJob?.cancel()
        sweepJob = null
    }

    /** How many series the last sweep found updates for. Drives the tab badge. */
    fun updatesFound(): Int = _updates.value.values.count { it.hasUpdates }

    /**
     * Forget one series' result — the user queued or dismissed it. The card's
     * badge clears; a later check repopulates it.
     */
    fun dismissUpdate(folder: String) {
        _updates.value = _updates.value - folder
    }

    fun clearUpdates() {
        _updates.value = emptyMap()
        _sweep.value = null
    }

    // ── delete ────────────────────────────────────────────────────────────

    /**
     * Delete a series folder. Returns null on success, or a human-readable
     * reason.
     *
     * The containment guard lives in Python (`delete_series` refuses anything
     * outside the configured library root, and refuses the root itself) rather
     * than here, because that is the last layer before `shutil.rmtree` and the
     * path has crossed JSON and JNI to get there.
     */
    suspend fun delete(context: Context, folder: String): String? =
        withContext(Dispatchers.IO) {
            val outcome = runCatching {
                Aio.module(context.applicationContext)
                    .callAttr("delete_series", folder).toString()
            }.getOrElse { return@withContext it.message ?: "Delete failed" }

            val error = runCatching { org.json.JSONObject(outcome).optString("error") }
                .getOrDefault("")
            if (error.isNotBlank()) {
                return@withContext when (error) {
                    "refused_root" -> "That's the library root, not a series"
                    "outside_library" -> "That folder isn't inside the library"
                    "not_found" -> "Already gone"
                    "not_configured" -> "The library root isn't set up yet"
                    else -> "Delete failed ($error)"
                }
            }

            // Spliced out in place rather than triggering a rescan: the user is
            // watching the grid, and a full disk walk to remove one card is both
            // slower and visibly jankier than dropping it.
            _series.value = _series.value?.filterNot { it.folder == folder }
            _updates.value = _updates.value - folder
            null
        }
}
