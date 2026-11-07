package com.aio.downloader.core

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Unfinished downloads: what is on disk, and how to continue or discard one.
 *
 * ── WHY THIS EXISTS AT ALL ─────────────────────────────────────────────────
 * A cancelled download keeps every chapter it finished. So does one Android
 * killed for memory, and one that died when the radio dropped. Without this the
 * only way to get those chapters into the library is to download them again —
 * which on a metered connection is the app charging the user for its own
 * interruption. The desktop has had a resume bar since long before this port;
 * this is the same feature over the same on-disk state.
 *
 * ── THE ONE THING THE PYTHON SIDE CANNOT DO ────────────────────────────────
 * `scan_resumable` reports the RUNNING download's tmp folder too — it only
 * stops being unfinished when the run completes and cleans up. Filtering it
 * needs to know what is running, which lives in [DownloadRepository], so
 * [visible] does that here rather than having Python guess.
 *
 * ── AND THE CONSTRAINT IT INHERITS ─────────────────────────────────────────
 * A resume IS a download. It goes through [DownloadRepository.enqueue] and the
 * same serial worker as everything else, so resuming during a download queues
 * behind it instead of corrupting aio-dl.py's process-wide state.
 *
 * Cross-file: aio_android.py (scan_resumable / build_resume_argv_json /
 * delete_resumable), ui/screens/QueueScreen.kt.
 */
object ResumeRepository {

    private const val TAG = "AioResume"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** null until the first scan — distinct from "scanned, and there are none". */
    private val _runs = MutableStateFlow<List<ResumableRun>?>(null)
    val runs: StateFlow<List<ResumableRun>?> = _runs.asStateFlow()

    private val _scanning = MutableStateFlow(false)
    val scanning: StateFlow<Boolean> = _scanning.asStateFlow()

    /**
     * The rows worth showing: everything on disk minus whatever is queued or
     * running, matched on URL.
     *
     * A running download's tmp folder is real and IS resumable — just not YET,
     * and offering to resume the thing currently downloading is the kind of
     * button that produces a bug report rather than a click.
     */
    fun visible(
        all: List<ResumableRun>?,
        active: ActiveRun?,
        queue: List<DownloadJob>,
    ): List<ResumableRun> {
        val busyUrls = buildSet {
            active?.job?.url?.let { add(it) }
            queue.forEach { add(it.url) }
        }
        return all.orEmpty().filterNot { it.url.isNotBlank() && it.url in busyUrls }
    }

    /**
     * Rescan. Safe to call on every screen entry — a scan already in flight
     * wins and the call is dropped, so tab-switching cannot stack disk walks.
     */
    fun refresh(context: Context) {
        if (_scanning.value) return
        _scanning.value = true
        val app = context.applicationContext
        scope.launch {
            try {
                // The back-fill is applied HERE, at the same seam the desktop
                // applies its own (`UI-source/electron/main.js`, the
                // `scan-resumable` handler): a tmp folder written before
                // aio-dl.py had `run_meta.json` reports no URL, and without one
                // `ResumableRun.canResume` is false and the card says so. The
                // run history knows the URL, keyed by the same `hid` — see
                // backfillResumable in core/RunPersistence.kt.
                _runs.value = backfillResumable(
                    parseResumable(Aio.module(app).callAttr("scan_resumable").toString()),
                    DownloadRepository.history.value,
                )
            } catch (t: Throwable) {
                // Chaquopy surfaces the Python traceback as the message.
                Log.e(TAG, "resume scan failed", t)
                // Leave a previous result standing; a transient failure should
                // not make a resumable download look like it disappeared.
                if (_runs.value == null) _runs.value = emptyList()
            } finally {
                _scanning.value = false
            }
        }
    }

    /**
     * Queue a resume of [run] in [format].
     *
     * The argv is built HERE rather than at run time (which is what an ordinary
     * download does) because a resume is not describable as a settings dict —
     * `--restore-parameters` is a mode, not a setting, and everything else
     * comes back off disk. The one thing still resolved late is the throttle,
     * which `build_resume_argv` takes from the CURRENT settings: a resume may
     * be running on a worse connection than the run it continues.
     *
     * Returns false when the same URL is already queued or running.
     */
    suspend fun resume(
        context: Context,
        run: ResumableRun,
        format: String = run.effectiveFormat,
        epubLayout: String = "vertical",
    ): Boolean {
        if (!run.canResume) return false
        val app = context.applicationContext

        val argv = withContext(Dispatchers.IO) {
            runCatching {
                Aio.module(app).callAttr(
                    "build_resume_argv_json",
                    run.url,
                    format,
                    if (format == "epub") epubLayout else "",
                    AppSettingsStore.current(app).toGlobalSettingsJson(),
                ).toString()
            }.getOrElse {
                Log.e(TAG, "could not build resume argv", it)
                return@withContext null
            }
        } ?: return false

        val job = DownloadJob(
            id = DownloadRepository.newJobId(),
            url = run.url,
            // Empty: DownloadService only consults settingsJson when it has to
            // BUILD an argv, and [resumeArgvJson] short-circuits that.
            settingsJson = "{}",
            resumeArgvJson = argv,
            format = format,
            chapters = "resume",
            title = run.title,
        )
        if (!DownloadRepository.enqueue(job)) return false
        com.aio.downloader.DownloadService.ensureRunning(app)
        // Drop the row immediately. The folder still exists — it is about to be
        // the running download's — and leaving it listed would offer a second
        // resume of work already queued.
        _runs.value = _runs.value?.filterNot { it.tmpDir == run.tmpDir }
        return true
    }

    /**
     * Discard one unfinished download's working folder. Returns null on
     * success, or a human-readable reason.
     *
     * The containment guard lives in Python (`delete_resumable` refuses
     * anything that is not a direct `tmp_*` child of the working directory)
     * rather than here — that is the last layer before `shutil.rmtree`, and the
     * path has crossed JSON and JNI to get there. The working directory also
     * holds the browser profiles and the vrf token cache, which is what makes
     * the guard worth having twice.
     */
    suspend fun discard(context: Context, run: ResumableRun): String? =
        withContext(Dispatchers.IO) {
            val outcome = runCatching {
                Aio.module(context.applicationContext)
                    .callAttr("delete_resumable", run.tmpDir).toString()
            }.getOrElse { return@withContext it.message ?: "Couldn't discard it" }

            val error = runCatching { JSONObject(outcome).optString("error") }.getOrDefault("")
            if (error.isNotBlank()) return@withContext resumeDeleteErrorMessage(error)

            _runs.value = _runs.value?.filterNot { it.tmpDir == run.tmpDir }
            null
        }
}
