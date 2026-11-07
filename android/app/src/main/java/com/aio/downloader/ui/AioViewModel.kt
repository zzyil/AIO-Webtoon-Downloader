package com.aio.downloader.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.aio.downloader.DownloadService
import com.aio.downloader.core.AppSettingsStore
import com.aio.downloader.core.DownloadForm
import com.aio.downloader.core.DownloadJob
import com.aio.downloader.core.DownloadRepository
import com.aio.downloader.core.LibrarySeries

/**
 * Holds the Download form across configuration changes and owns the enqueue
 * path.
 *
 * A ViewModel rather than `remember`: rotating the tablet destroys the Activity,
 * and losing a half-configured download to a screen rotation is the kind of
 * thing that makes an app feel cheap. Everything ELSE the UI shows — the queue,
 * the active run, the log — lives in [DownloadRepository] / `LogTail` instead,
 * because those must outlive the Activity entirely (a download keeps running
 * with the app swiped away, and its state has to still be there on return).
 *
 * So the split is: this class owns what the USER is composing, the singletons
 * own what the SERVICE is doing.
 */
class AioViewModel(app: Application) : AndroidViewModel(app) {

    var form by mutableStateOf(DownloadForm.load(app))
        private set

    fun update(transform: (DownloadForm) -> DownloadForm) {
        form = transform(form)
    }

    /**
     * The form plus the device-level settings, as one settings dict.
     *
     * Read at ENQUEUE time, not held: the Settings screen writes through
     * [AppSettingsStore], and a form composed before a limit changed should
     * still start under the new limit.
     */
    fun settingsJson(url: String): String =
        form.toSettingsJson(url, AppSettingsStore.current(getApplication()))

    /**
     * Persist the form. Called on lifecycle STOP and after a successful start,
     * not on every keystroke — this is a SharedPreferences write, and the URL
     * field would otherwise commit a blob per character typed.
     */
    fun persist() = form.save(getApplication())

    /**
     * Queue every URL in the form and wake the service.
     *
     * Returns how many jobs were actually accepted; the difference against
     * `form.urls().size` is duplicates the repository rejected, which the caller
     * reports rather than silently swallowing — a Start button that appears to
     * do nothing is worse than one that says why.
     *
     * One job PER URL, deliberately: the desktop hands a multi-URL batch to a
     * single process with `--jobs N`, but that spawns parallel workers, and
     * parallel runs inside one Chaquopy interpreter would corrupt aio-dl.py's
     * process-wide backoff and prefetch globals. Serial jobs are the same work
     * in the same order with the constraint respected.
     */
    fun startDownloads(): StartResult {
        val urls = form.urls()
        if (urls.isEmpty()) return StartResult(0, 0)

        var accepted = 0
        urls.forEach { url ->
            val job = DownloadJob(
                id = DownloadRepository.newJobId(),
                url = url,
                settingsJson = settingsJson(url),
                format = if (form.komikku) "cbz" else form.format,
                chapters = form.chapters,
            )
            if (DownloadRepository.enqueue(job)) accepted++
        }

        if (accepted > 0) {
            DownloadService.ensureRunning(getApplication())
            // Clear the URL box only — every other setting is a preference the
            // user will want again for the next series.
            form = form.copy(url = "")
            persist()
        }
        return StartResult(accepted = accepted, duplicates = urls.size - accepted)
    }

    /**
     * Queue "download the chapters this series is missing".
     *
     * Reached from the library — both the detail screen's Download button and
     * the sweep banner's Download-all. Returns false when the same series is
     * already queued or running, which the library treats as "already handled"
     * rather than as an error.
     *
     * The settings are the user's own, with the series overriding format,
     * language and source — see DownloadForm.libraryUpdateSettingsJson for why
     * those three and not others.
     */
    fun queueLibraryUpdate(series: LibrarySeries, chapters: String): Boolean {
        if (series.url.isBlank() || chapters.isBlank()) return false
        val job = DownloadJob(
            id = DownloadRepository.newJobId(),
            url = series.url,
            settingsJson = form.libraryUpdateSettingsJson(
                url = series.url,
                chapters = chapters,
                seriesFormat = series.format,
                seriesLanguage = series.language,
                site = series.site,
                app = AppSettingsStore.current(getApplication()),
            ),
            format = series.format.takeIf { it in DownloadForm.FORMATS } ?: form.format,
            chapters = chapters,
            title = series.name,
        )
        if (!DownloadRepository.enqueue(job)) return false
        DownloadService.ensureRunning(getApplication())
        return true
    }

    data class StartResult(val accepted: Int, val duplicates: Int)
}
