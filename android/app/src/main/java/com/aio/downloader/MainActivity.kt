package com.aio.downloader

import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.lifecycle.lifecycleScope
import coil3.SingletonImageLoader
import com.aio.downloader.browser.ChallengeSolver
import com.aio.downloader.browser.WebViewBridge
import com.aio.downloader.core.Aio
import com.aio.downloader.core.AioImageLoader
import com.aio.downloader.core.AppSettingsStore
import com.aio.downloader.core.AppUpdater
import com.aio.downloader.core.DownloadForm
import com.aio.downloader.core.DownloadJob
import com.aio.downloader.core.DownloadRepository
import com.aio.downloader.core.LogTail
import com.aio.downloader.core.finalFileDetail
import com.aio.downloader.ui.AioApp
import com.aio.downloader.ui.theme.AioTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Hosts the Compose UI — and, unchanged from the M0 spike, the intent-driven
 * test harness.
 *
 * ── WHY THE HARNESS SURVIVED THE UI ────────────────────────────────────────
 * `android/TESTING.md` is the runbook every future testing agent follows, and
 * it drives downloads with `adb shell am start ... --es url ...` precisely so a
 * test can change site, chapter range and format without a rebuild. Deleting
 * that contract when the real UI landed would have broken every recipe in that
 * file for no gain, so the extras now feed the SAME queue the Start button does
 * rather than a private code path. What a test exercises is therefore what a
 * user gets, which the old inline path could not claim.
 *
 *   --es url       series URL. Absent => diagnostics only.
 *   --es chapters  --chapters spec. Default "1". "all", "-3" = last 3, ranges.
 *   --es format    cbz | epub | pdf | none. Default "cbz".
 *   --es extra     extra CLI args, space-separated, appended verbatim.
 *   --ez service   accepted and ignored; everything runs through the service
 *                  now. Kept so existing recipes keep working.
 *   --ez cancel    cancel the running download.
 *   --es browsertest <url>       exercise the WebView backend against one URL
 *                                without running a download (see [probeBrowser]).
 *   --ez forcechallenge true     with browsertest: pretend the page is
 *                                bot-checked, so the interactive handoff can be
 *                                tested without finding a live challenge.
 *
 * The `[run] exit=<code>` line TESTING.md greps for is emitted from here when
 * the enqueued job reaches the repository's history.
 *
 * Python's stdout/stderr reach logcat under Chaquopy's own `python.stdout` /
 * `python.stderr` tags, so the full download log is visible from `adb logcat`
 * as well as in the app's own Logs screen.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        // Draw behind the system bars. The screens inset themselves via
        // WindowInsets, so this is what lets the background run edge to edge
        // instead of sitting inside two grey letterboxes.
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)

        // FIRST, and synchronously: this restores the queue the last session
        // left behind, and everything below can enqueue. A restore that landed
        // after the harness intent (or after the user tapped Start) would drop
        // whichever job got there first. It is one preferences read of a few
        // KB — see core/RunStore.kt for why it is not a coroutine.
        DownloadRepository.attach(this)
        // The log tail follows logcat from "now", so it has to be running
        // BEFORE the output it should capture — not from the moment someone
        // opens the Logs tab, which is usually after the interesting part.
        LogTail.start()

        // API 33+ gates notifications behind a runtime grant. Without it the
        // foreground service still runs but is invisible — and so is its Cancel
        // action. Requested fire-and-forget: the download does not depend on it.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        // Give Coil a loader that can actually speak http. Without this every
        // remote AsyncImage fails silently — coil-compose alone ships no network
        // fetcher (see core/AioImageLoader.kt). setSafe, not setUnsafe: it is a
        // no-op if a loader was already created, so a relaunch through the
        // singleTop path cannot swap the loader out from under in-flight
        // requests.
        SingletonImageLoader.setSafe { AioImageLoader.create(it) }

        // Opt-in silent self-update. Idempotent (a singleTop relaunch re-enters
        // onCreate) and returns immediately — its first check is delayed so it
        // never competes with startup. Everything else is driven off its own
        // status flow; see core/AppUpdater.kt's WIRING section, which also
        // explains why it needs REQUEST_INSTALL_PACKAGES *declared* in the
        // manifest or it reports itself unsupported rather than downloading an
        // APK it could never hand over.
        AppUpdater.start(this)

        setContent {
            AioTheme {
                AioApp()
            }
        }

        handleTestIntent(intent)
    }

    /**
     * Once the Activity is running, a second `am start` DELIVERS HERE instead of
     * calling onCreate again (the manifest declares `singleTop`). Without this
     * override a cancel sent while the app is foregrounded is silently dropped
     * — which looks exactly like "cancel doesn't work".
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleTestIntent(intent)
    }

    // ── the intent harness ────────────────────────────────────────────────

    private fun handleTestIntent(intent: Intent?) {
        if (intent == null) return

        // Cancel goes through the ACTIVITY because DownloadService is
        // exported="false" — `adb am startservice` on it fails with "Requires
        // permission not exported", which is correct security behaviour, not a
        // bug to work around by exporting the service.
        if (intent.getBooleanExtra("cancel", false)) {
            Log.i(TAG, "cancel requested via intent (running=${DownloadService.isRunning()})")
            DownloadService.cancel(this)
            return
        }

        val browserTest = intent.getStringExtra("browsertest")?.trim().orEmpty()
        if (browserTest.isNotEmpty()) {
            probeBrowser(browserTest, intent.getBooleanExtra("forcechallenge", false))
            return
        }

        val solveTest = intent.getStringExtra("solvetest")?.trim().orEmpty()
        if (solveTest.isNotEmpty()) {
            // Drives the interactive solve WITHOUT the bridge, so no offscreen
            // WebView is ever created in this process. That difference is the
            // whole point: it isolates the visible WebView from the headless one.
            lifecycleScope.launch(Dispatchers.IO) {
                runCatching {
                    ChallengeSolver.solve(this@MainActivity, solveTest, 180_000L)
                }.onSuccess {
                    Log.i(TAG, "[solve] ok  ua=${it.userAgent}  cookies=${it.cookies.length} chars")
                }.onFailure {
                    Log.e(TAG, "[solve] FAILED ${it.message}")
                }
            }
            return
        }

        val url = intent.getStringExtra("url")?.trim().orEmpty()
        if (url.isEmpty()) {
            // No URL: answer the M0 question — does the downloader's Python
            // import and run here? The UI's diagnostics sheet shows the same
            // payload; this path exists so `am start` with no extras still
            // prints it to logcat for an agent to grep.
            logDiagnostics()
            return
        }

        val form = DownloadForm(
            url = url,
            chapters = intent.getStringExtra("chapters")?.trim().orEmpty().ifEmpty { "1" },
            format = intent.getStringExtra("format")?.trim().orEmpty().ifEmpty { "cbz" },
        )
        val extra = intent.getStringExtra("extra")?.trim().orEmpty()
        val job = DownloadJob(
            id = DownloadRepository.newJobId(),
            url = url,
            // Carries the device-level settings too, so a harness run honours
            // whatever Resource Limits the tester set — what a test exercises
            // has to be what a user gets.
            settingsJson = form.toSettingsJson(url, AppSettingsStore.current(this)),
            extraArgs = if (extra.isEmpty()) emptyList() else extra.split(" ").filter { it.isNotBlank() },
            format = form.format,
            chapters = form.chapters,
        )

        if (!DownloadRepository.enqueue(job)) {
            Log.i(TAG, "[run] already queued: $url")
            return
        }
        Log.i(TAG, "[run] queued ${job.id} $url chapters=${form.chapters} format=${form.format}")
        DownloadService.ensureRunning(this)
        reportWhenFinished(job.id)
    }

    /**
     * Emit the `[run] exit=` line TESTING.md's wait loop greps for, once this
     * job lands in history.
     *
     * lifecycleScope, so a rotation does not orphan the collector — and if the
     * Activity is destroyed entirely the service still finishes the download and
     * still logs its own `download finished exit=` line, which is the backstop
     * the runbook falls back to.
     */
    private fun reportWhenFinished(jobId: String) {
        lifecycleScope.launch {
            val record = DownloadRepository.history
                .first { history -> history.any { it.finished.id == jobId } }
                .first { it.finished.id == jobId }
            val entry = record.finished

            // 130 is aio_android.CANCELLED_EXIT_CODE. Spelling it out keeps a log
            // reader (and android/TESTING.md) from reading a cancel as a failure.
            val verdict = when (entry.exitCode) {
                0 -> "ok"
                DownloadService.CANCELLED_EXIT -> "CANCELLED by user"
                else -> "failed"
            }
            Log.i(
                TAG,
                "[run] exit=${entry.exitCode} ($verdict) in " +
                    "%.1fs".format(entry.durationMs / 1000.0) +
                    "  chapters=${entry.processed}/${entry.total}",
            )
            // On its own line, and only when it happened: this is the engine
            // saying it declined to rebuild the combined archive, which is the
            // one outcome an exit code cannot express (a partial-coverage skip
            // exits 0). android/TESTING.md greps `[run] exit=`; this sits
            // beside it rather than inside it so that grep is unaffected.
            record.finalFileSkip?.let {
                Log.i(TAG, "[run] final file NOT rebuilt: ${finalFileDetail(it)}")
            }
            Log.i(TAG, describeTree())
        }
    }

    /**
     * One round-trip through every operation the WebView backend exposes, with
     * no download in the way.
     *
     * WHY IT EARNS ITS KEEP: when MangaFire fails, the question is always "is
     * the browser bridge broken, or is the signer?" — and answering it through
     * a full download means reading a Python traceback for a symptom four
     * layers downstream. This answers it directly, in about ten seconds.
     *
     * The async evaluation is the load-bearing assertion. `evaluateJavascript`
     * does not await promises, so if the wrapper in [buildEvalScript] were
     * wrong, `asyncSum` would come back as `{}` rather than 42 — which is
     * precisely how a broken bridge presents itself at the signer: every token
     * null, nothing else obviously amiss.
     */
    private fun probeBrowser(url: String, forceChallenge: Boolean) {
        lifecycleScope.launch(Dispatchers.IO) {
            runCatching {
                // Goes through Aio.module so the probe exercises the REAL
                // install path (attach + set_browser_bridge), not a private one.
                Aio.module(this@MainActivity)
                if (forceChallenge) WebViewBridge.forceNextChallenge()

                // A profile of its own, so a probe cannot disturb a signer
                // session that a queued download is relying on.
                val bridge = WebViewBridge.forProfile("probe")
                val startedAt = System.currentTimeMillis()
                bridge.goto(url, 45_000)
                val elapsed = System.currentTimeMillis() - startedAt

                val title = bridge.evaluate("() => document.title", "")
                val asyncSum = bridge.evaluate("async (a) => a[0] + a[1]", "[2,40]")
                val html = bridge.content()
                val cookies = bridge.cookies(url)

                buildString {
                    appendLine("[browser] ok in ${elapsed}ms  $url")
                    appendLine("[browser]   userAgent   = ${bridge.userAgent()}")
                    appendLine("[browser]   title       = $title")
                    appendLine("[browser]   asyncSum    = $asyncSum   (42 == promises are awaited)")
                    appendLine("[browser]   html        = ${html.length} chars")
                    appendLine("[browser]   cookies     = ${cookies.length} chars")
                    appendLine("[browser]   hasSelector = ${bridge.waitForSelector("body", 3_000)}")
                }
            }.onSuccess {
                Log.i(TAG, it)
            }.onFailure {
                Log.e(TAG, "[browser] FAILED ${it.javaClass.simpleName}: ${it.message}", it)
            }
        }
    }

    private fun logDiagnostics() {
        lifecycleScope.launch {
            runCatching {
                withContext(Dispatchers.IO) {
                    // Importing the handler registry pulls in ~65 modules; on the
                    // main thread that is an ANR.
                    Aio.module(this@MainActivity).callAttr("diagnostics").toString()
                }
            }.onSuccess {
                Log.i(TAG, "[ok] configure()  library=${Aio.libraryDir(this@MainActivity)}")
                Log.i(TAG, it)
            }.onFailure {
                // Chaquopy surfaces the Python traceback as the exception
                // message, so this is usually the entire diagnosis.
                Log.e(TAG, "[FAILED] ${it.javaClass.name}: ${it.message}", it)
            }
        }
    }

    /** Flat listing of the library, so a CBZ is verifiable from logcat alone. */
    private fun describeTree(): String {
        val root = Aio.libraryDir(this)
        if (!root.exists()) return "output dir does not exist: ${root.absolutePath}"
        val files = root.walkTopDown().filter { it.isFile }.sortedBy { it.absolutePath }.toList()
        if (files.isEmpty()) return "output dir is EMPTY: ${root.absolutePath}"
        val total = files.sumOf { it.length() }
        return buildString {
            appendLine(
                "${files.size} file(s), ${"%.2f".format(total / 1024.0 / 1024.0)} MB " +
                    "under ${root.absolutePath}",
            )
            files.forEach { appendLine("  %9d  %s".format(it.length(), it.relativeTo(root).path)) }
        }
    }

    private companion object {
        // Unchanged from the spike: TESTING.md filters logcat on this tag.
        const val TAG = "AioM0"
    }
}
