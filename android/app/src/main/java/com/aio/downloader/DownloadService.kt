package com.aio.downloader

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.aio.downloader.browser.WebViewBridge
import com.aio.downloader.core.Aio
import com.aio.downloader.core.DownloadJob
import com.aio.downloader.core.DownloadRepository
import com.aio.downloader.core.FinalFileSkip
import com.aio.downloader.core.LogTail
import com.aio.downloader.core.finalFileDetail
import com.aio.downloader.core.finalFileHeadline
import com.aio.downloader.core.formatEta
import com.chaquo.python.PyObject
import org.json.JSONArray
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * Runs queued downloads to completion, one at a time, as a foreground service.
 *
 * WHY A SERVICE: `run_download` blocks for minutes and must survive the screen
 * locking and the Activity being destroyed. A foreground service with a
 * notification is the only thing Android will not freeze for that long.
 *
 * WHY THE SAME PROCESS (the plan originally said `android:process=":py"`):
 * Chaquopy's interpreter is per-process, so a separate `:py` process means a
 * SECOND interpreter the moment the Activity also touches Python — double the
 * native memory, and two disjoint copies of aio-dl.py's module-level state.
 * The one thing `:py` bought was a hard-kill cancel; cooperative cancel
 * (`aio_android.cancel()` -> `_RUN_CANCEL`) covers that and leaves the partial
 * download resumable, which killing the process does not.
 *
 * DOWNLOADS ARE SERIALIZED. aio-dl.py keeps per-run state in module-level
 * globals — `_HOST_CONCURRENCY_CAP`, `_image_prefetch_queue`,
 * `_RATE_LIMIT_SCHEDULE` — which are process-wide, so two concurrent runs in
 * one interpreter would corrupt each other's backoff and prefetch state. This
 * is a hard constraint, not a policy choice. It used to be enforced by REFUSING
 * a second request; since M3 the surplus work waits in
 * [DownloadRepository]'s queue instead, which is the same constraint expressed
 * as a feature rather than as a button that does nothing.
 *
 * THIS SERVICE IS THE SOLE CONSUMER OF `aio_android.poll_events()`. That call
 * drains the Python-side queue, so a second poller (say, the UI wanting its own
 * copy) would steal half the events from this one. The UI reads
 * [DownloadRepository] instead. See that file's header.
 */
class DownloadService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    /**
     * Both process entry points install the same two singletons, because either
     * can be the first thing to run: the Activity when the user opens the app,
     * this service when a download is started from a notification action or the
     * intent harness while no Activity exists.
     *
     * [DownloadRepository.attach] restores the saved queue — it must happen
     * before [onStartCommand] can claim the worker, or the loop would drain an
     * empty queue and stop before the restore landed. Both calls are idempotent.
     */
    override fun onCreate() {
        super.onCreate()
        DownloadRepository.attach(this)
        // Started here, not by the Logs screen: a download running with the app
        // swiped away is exactly the one whose log a user comes back for, and
        // the tail follows logcat from "now", so anything not captured live is
        // gone. See core/LogTail.kt's header.
        LogTail.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_CANCEL) {
            // A cancel arriving with nothing running would otherwise leave the
            // service started and idle forever — it never reaches the worker's
            // finally-block stopSelf. Reachable from the test harness's
            // `--ez cancel true`, which does not check first.
            if (!isRunning()) {
                Log.i(TAG, "cancel with no active run; stopping")
                stopSelf(startId)
                return START_NOT_STICKY
            }
            cancelRun()
            return START_NOT_STICKY
        }

        lastStartId = startId

        // API 29+ requires the type here when the manifest declares one, and on
        // API 34+ omitting it throws MissingForegroundServiceTypeException.
        // Called on EVERY start command, including one that finds a worker
        // already running: startForegroundService imposes the ~5s deadline per
        // call, and re-posting the same notification id satisfies it cheaply.
        promoteToForeground()

        // The running worker will pick up whatever was just enqueued on its next
        // iteration, so a second loop would only violate serialization.
        if (!DownloadRepository.claimWorker()) {
            Log.i(TAG, "worker already running; queued for it to pick up")
            return START_NOT_STICKY
        }

        thread(name = "aio-download") { drainQueue() }

        // NOT START_REDELIVER_INTENT: if Android kills us mid-download, silently
        // restarting a multi-hundred-megabyte download on someone's mobile data
        // is hostile. The partial run is resumable, so let the user re-start it.
        return START_NOT_STICKY
    }

    // ── the worker ────────────────────────────────────────────────────────

    /**
     * Pull jobs until the queue is empty, then stop.
     *
     * The loop condition and the worker-slot release are ONE atomic step inside
     * [DownloadRepository.takeNextOrRelease] — split them and a job enqueued in
     * the gap sits in the queue with no worker and no way to get one.
     */
    private fun drainQueue() {
        val wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$TAG::download")
        val wifiLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "$TAG::download")

        try {
            wakeLock.acquire(MAX_RUN_MS)
            wifiLock.acquire()

            while (true) {
                val job = DownloadRepository.takeNextOrRelease() ?: break
                runOne(job)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "worker died", t)
            // The release inside takeNextOrRelease never ran, so do it here or
            // the queue is wedged until the process restarts.
            DownloadRepository.releaseWorker()
        } finally {
            runCatching { if (wakeLock.isHeld) wakeLock.release() }
            runCatching { if (wifiLock.isHeld) wifiLock.release() }
            // detach=false would cancel the terminal notification just posted.
            stopForeground(STOP_FOREGROUND_DETACH)
            // stopSelf(id) only stops when no newer start command has arrived,
            // which is what keeps a job enqueued during teardown from being
            // stranded by a plain stopSelf().
            stopSelf(lastStartId)
        }
    }

    private fun runOne(job: DownloadJob) {
        // Fresh bot-check budget per run. The bridge suppresses a second prompt
        // for the same host so one run cannot loop on it, but this process
        // outlives many downloads, and a clearance that expired since the last
        // one has to be renewable.
        WebViewBridge.resetChallengeOffers()
        DownloadRepository.beginRun(job)
        resetRunDisplay(job)
        notify(buildNotification(job.displayLabel, "Starting…"))

        var exit = 1
        // Terminal wording for a run that never reached the engine. Null on the
        // normal path, where the exit code decides. It is THREADED to the
        // `finally` rather than notified from the catch because both post under
        // NOTIFICATION_ID: anything raised in the catch is overwritten by the
        // terminal notification milliseconds later and the user never sees it.
        var abort: RunAbort? = null
        try {
            val aio = Aio.module(this)

            // Argv is resolved HERE, not at enqueue time: build_argv is where
            // resource limits and the flag-compatibility guards are applied, so
            // a job queued an hour ago still picks up the limits in force now.
            val argvJson = buildArgv(aio, job)
            Log.i(TAG, "run_download_json $argvJson")

            // Poll progress on a separate thread: run_download blocks this one
            // for the whole download.
            val done = java.util.concurrent.atomic.AtomicBoolean(false)
            val poller = thread(name = "aio-progress") { pollProgress(aio, done) }

            exit = try {
                // MUST be run_download_json, not run_download — a java.util.List
                // does not cross JNI as a Python iterable. See Aio.kt's header.
                aio.callAttr("run_download_json", argvJson).toInt()
            } finally {
                done.set(true)
                poller.join(POLL_JOIN_MS)
            }
            Log.i(TAG, "download finished exit=$exit")
        } catch (t: Throwable) {
            // Chaquopy surfaces the Python traceback as the exception message,
            // so this is usually the entire diagnosis.
            Log.e(TAG, "download failed", t)
            abort = when (t) {
                // Not a crash: the engine never started, and build_argv_json's
                // `message` is already a finished user-facing sentence, so it is
                // shown verbatim rather than re-worded here.
                is UnsupportedSiteException -> RunAbort("Can't download this link", t.message)
                else -> RunAbort("Download failed", t.message?.take(200) ?: "unknown error")
            }
        } finally {
            val label = DownloadRepository.active.value?.label ?: job.displayLabel
            // The record, not a re-read of the flow: by the time the next job
            // starts the flow's head is a different run, and the notification
            // for THIS one must describe THIS one.
            val record = DownloadRepository.finishRun(exit)
            postTerminalNotification(exit, label, record?.finalFileSkip, abort)
        }
    }

    /**
     * Splice [DownloadJob.extraArgs] into the built argv.
     *
     * The URL is positional and must stay LAST, so raw extras go in before it.
     * Only the `--es extra` intent test harness populates them; see
     * MainActivity and android/TESTING.md.
     *
     * A RESUME arrives with its argv already built (ResumeRepository), because
     * `--restore-parameters` is a mode rather than a settings dict — there is
     * nothing for build_argv to build from.
     *
     * THROWS [UnsupportedSiteException] when the engine refuses the URL
     * outright (today: comix, which needs a desktop browser).
     */
    /**
     * A URL no download in this build can serve. [message] is a finished,
     * user-facing sentence from aio_android.build_argv_json — never a
     * diagnostic, because it is rendered as-is.
     */
    private class UnsupportedSiteException(override val message: String) : Exception(message)

    /** Terminal notification wording for a run that never reached the engine. */
    private data class RunAbort(val title: String, val text: String)

    private fun buildArgv(aio: PyObject, job: DownloadJob): String {
        job.resumeArgvJson?.let { return it }
        val raw = aio.callAttr("build_argv_json", job.settingsJson).toString()
        // TWO RETURN SHAPES, discriminated on the first character: `[` is the
        // argv, `{` is a refusal. build_argv_json's docstring in aio_android.py
        // prescribes exactly this branch. A bare JSONArray() here (what this
        // used to do) renders the refusal as "Value {...} cannot be converted to
        // JSONArray" — so a pasted comix link read as an internal error.
        if (raw.trimStart().startsWith("{")) {
            throw UnsupportedSiteException(
                JSONObject(raw).optString("message").ifEmpty {
                    "This link cannot be downloaded on Android."
                },
            )
        }
        val built = JSONArray(raw)
        if (job.extraArgs.isEmpty()) return built.toString()

        val merged = JSONArray()
        for (i in 0 until built.length() - 1) merged.put(built.getString(i))
        job.extraArgs.forEach { merged.put(it) }
        if (built.length() > 0) merged.put(built.getString(built.length() - 1))
        return merged.toString()
    }

    /**
     * The last thing a run says.
     *
     * ── WHY [skip] IS THE HEADLINE WHEN IT EXISTS ─────────────────────────
     * This notification used to read "Download cancelled / Finished chapters
     * kept — resume to continue", which was reassurance about the exact thing
     * aio-dl.py was destroying at the time: a cancelled run rebuilt the
     * combined archive from its own partial content and overwrote a complete
     * one. That is fixed in the engine now — and the fix moved the lie to the
     * other side, because the wording still said nothing about the archive
     * having been KEPT.
     *
     * `final_file_skipped` is the engine saying which of the three things
     * happened, and [finalFileHeadline] / [finalFileDetail] are where the
     * wording lives so this notification and the Queue history card cannot
     * drift apart. The three console lines the event accompanies are printed
     * beside its emit site in aio-dl.py; the text agrees with them on purpose,
     * because the Logs screen is right there.
     *
     * A run with NO skip event never reached that branch at all
     * (`--format none`, `--komikku`, `--no-final-file`, or a failure before the
     * end), so nothing was overwritten and the old generic wording is still
     * true — that is what the fallbacks say.
     */
    private fun postTerminalNotification(
        exit: Int,
        label: String,
        skip: FinalFileSkip?,
        /**
         * Wording for a run that died before the engine ran. When present it
         * WINS over the exit code, which for those runs is an uninformative 1.
         */
        abort: RunAbort? = null,
    ) {
        val remaining = DownloadRepository.queue.value.size
        val tail = if (remaining > 0) " · $remaining still queued" else ""
        val headline = skip?.let { finalFileHeadline(it) }
        val detail = skip?.let { "$label\n\n${finalFileDetail(it)}" }

        val title = abort?.title ?: when (exit) {
            0 -> "Download complete"
            CANCELLED_EXIT -> "Download cancelled"
            else -> "Download stopped"
        }
        val text = when {
            abort != null -> abort.text + tail
            headline != null -> headline + tail
            exit == 0 -> label + tail
            exit == CANCELLED_EXIT -> "Finished chapters kept — resume to continue$tail"
            else -> "exit code $exit$tail"
        }
        // A refusal sentence runs ~190 characters and a collapsed notification
        // shows one line, so it also goes in the expanded body.
        notify(buildNotification(title, text, ongoing = false, bigText = abort?.text ?: detail))
    }

    // ── progress ──────────────────────────────────────────────────────────

    /** Drains aio_android's event queue into the repository and the notification. */
    private fun pollProgress(aio: PyObject, done: java.util.concurrent.atomic.AtomicBoolean) {
        while (!done.get()) {
            drainOnce(aio)
            try {
                Thread.sleep(POLL_INTERVAL_MS)
            } catch (_: InterruptedException) {
                return
            }
        }
        // One final drain AFTER the run returned. The loop above exits on the
        // flag, so everything emitted in the last poll interval — including the
        // terminal `done` event and the last chapter — would otherwise sit in
        // the queue unread and the notification would freeze one chapter short.
        drainOnce(aio)
    }

    private fun drainOnce(aio: PyObject) {
        try {
            val batch = JSONArray(aio.callAttr("poll_events").toString())
            for (i in 0 until batch.length()) {
                DownloadRepository.applyEvent(batch.optJSONObject(i) ?: continue)
            }
            if (batch.length() > 0) {
                val run = DownloadRepository.active.value ?: return
                notify(buildNotification(run.label, progressText()))
            }
        } catch (t: Throwable) {
            // Progress is cosmetic — never let it take down the download.
            Log.w(TAG, "progress poll failed", t)
        }
    }

    /**
     * The notification's one line of text. Deliberately the same facts, in the
     * same order, as the Queue card — a user glancing at the shade and a user
     * opening the app should not have to reconcile two different stories.
     */
    private fun progressText(): String {
        val p = DownloadRepository.active.value?.progress ?: return "Working"
        return buildString {
            if (p.total > 0) append("Chapter ${p.processed}/${p.total}") else append("Working")
            p.chapter?.let { append("  ·  ch $it") }
            // Below two samples the estimator publishes nothing, so an ETA here
            // is always one the UI would also be willing to show.
            p.etaMs?.takeIf { p.etaSamples >= 2 }?.let { append("  ·  ${formatEta(it)} left") }
            val queued = DownloadRepository.queue.value.size
            if (queued > 0) append("  ·  +$queued queued")
        }
    }

    private fun resetRunDisplay(job: DownloadJob) {
        notifyTotal = 0
        notifyProcessed = 0
        notifyLabel = job.displayLabel
    }

    private fun cancelRun() {
        Log.i(TAG, "cancel requested")
        notify(buildNotification("Cancelling…", "finishing the current page"))
        // Cooperative: pages already in flight finish or time out, then the
        // chapter loop breaks and everything downloaded so far stays resumable.
        // Only the CURRENT run is affected — anything queued behind it still
        // starts, matching the desktop, where cancel targets one download id.
        thread(name = "aio-cancel") {
            runCatching { Aio.moduleOrNull()?.callAttr("cancel") }
                .onFailure { Log.w(TAG, "cancel failed", it) }
        }
    }

    // ── notification ──────────────────────────────────────────────────────

    private fun promoteToForeground() {
        val starting = buildNotification(notifyLabel ?: "Starting…", null)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                starting,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIFICATION_ID, starting)
        }
    }

    private fun buildNotification(
        title: String,
        text: String?,
        ongoing: Boolean = true,
        /**
         * The expanded body. A one-line [text] cannot carry "the existing
         * 50-chapter file was kept and your 3 new chapters are in the temp
         * folder", and that sentence is the whole point of the terminal
         * notification when the engine declines a rebuild.
         */
        bigText: String? = null,
    ): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Downloads", NotificationManager.IMPORTANCE_LOW)
                    .apply { description = "Progress for running downloads" },
            )
        }

        val progress = DownloadRepository.active.value?.progress
        notifyTotal = progress?.total ?: notifyTotal
        notifyProcessed = progress?.processed ?: notifyProcessed

        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val cancel = PendingIntent.getService(
            this,
            1,
            Intent(this, DownloadService::class.java).setAction(ACTION_CANCEL),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentIntent(open)
            .setOngoing(ongoing)
            .setOnlyAlertOnce(true)
            .apply {
                bigText?.let { setStyle(Notification.BigTextStyle().bigText(it)) }
                if (ongoing) {
                    addAction(
                        Notification.Action.Builder(
                            null as android.graphics.drawable.Icon?,
                            "Cancel",
                            cancel,
                        ).build(),
                    )
                    if (notifyTotal > 0) setProgress(notifyTotal, notifyProcessed, false)
                }
            }
            .build()
    }

    private fun notify(n: Notification) {
        runCatching { getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, n) }
    }

    // Mirrors of the repository's numbers, cached so buildNotification can be
    // called from the terminal path after finishRun() has already cleared the
    // active run. Only the worker and poller threads touch them.
    @Volatile private var notifyTotal: Int = 0
    @Volatile private var notifyProcessed: Int = 0
    @Volatile private var notifyLabel: String? = null
    @Volatile private var lastStartId: Int = 0

    companion object {
        const val TAG = "AioService"
        const val ACTION_CANCEL = "com.aio.downloader.CANCEL"

        /**
         * Mirrors `aio_android.CANCELLED_EXIT_CODE` — grep that name. A
         * cancelled run used to come back as 0, indistinguishable from a clean
         * finish, so the notification claimed the download had completed.
         */
        const val CANCELLED_EXIT = DownloadRepository.CANCELLED_EXIT_CODE

        private const val CHANNEL_ID = "downloads"
        private const val NOTIFICATION_ID = 1001
        private const val POLL_INTERVAL_MS = 700L
        private const val POLL_JOIN_MS = 2_000L

        // Upper bound on the wakelock, NOT on the download. A wakelock without a
        // timeout is a battery bug waiting to happen; if a download really runs
        // longer than this the foreground service keeps it alive anyway.
        private const val MAX_RUN_MS = 6L * 60 * 60 * 1000

        /** True while a download is in flight — the UI uses this to label "Start". */
        fun isRunning(): Boolean = DownloadRepository.active.value != null

        /**
         * Wake the worker. Safe to call for every enqueue: if a worker is
         * already draining the queue this is a no-op beyond one notification
         * repost.
         */
        fun ensureRunning(context: Context) {
            context.startForegroundService(Intent(context, DownloadService::class.java))
        }

        fun cancel(context: Context) {
            context.startService(
                Intent(context, DownloadService::class.java).setAction(ACTION_CANCEL),
            )
        }
    }
}
