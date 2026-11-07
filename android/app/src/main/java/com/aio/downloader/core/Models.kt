package com.aio.downloader.core

/**
 * The state the UI renders, and the shapes it travels in.
 *
 * Everything here is immutable and free of Android and Chaquopy types on
 * purpose: [DownloadRepository] hands these to Compose, and Compose only
 * recomposes correctly when the values it reads are stable.
 */

/** One unit of work: a URL plus the settings blob that will become an argv. */
data class DownloadJob(
    val id: String,
    val url: String,
    /**
     * The settings dict as JSON, resolved to an argv by
     * `aio_android.build_argv_json` AT RUN TIME rather than at enqueue time.
     *
     * WHY LATE: build_argv is the chokepoint where resource limits and the flag
     * compatibility guards are applied (a stale `--modernize` on a PDF job is a
     * hard error in aio-dl.py). Resolving late means a job queued an hour ago
     * still picks up the limits in force when it actually starts — and it keeps
     * the enqueue path off Python entirely, so tapping Start never has to wait
     * on the interpreter booting.
     */
    val settingsJson: String,
    /**
     * A ready-made argv, used INSTEAD of building one from [settingsJson].
     *
     * Only resumes set this. A resume is not describable as a settings dict:
     * `--restore-parameters` is a mode, and every per-download setting comes
     * back off disk from `run_params.json` rather than from anything the UI
     * holds. See ResumeRepository.resume and aio_android.build_resume_argv.
     */
    val resumeArgvJson: String? = null,
    /**
     * Raw CLI args appended verbatim, spliced in BEFORE the positional URL.
     * Only the `--es extra` intent test harness uses this; see MainActivity.
     */
    val extraArgs: List<String> = emptyList(),
    /** Display-only, for the queue card before the `series` event names it. */
    val format: String = "",
    val chapters: String = "",
    /**
     * A title already known at enqueue time — a resume reads one out of
     * `run_meta.json`. Without it a queued resume shows a bare URL until the
     * run reaches its own `series` event, which for a resume is well after the
     * cached chapters have been collected.
     */
    val title: String = "",
    val enqueuedAt: Long = System.currentTimeMillis(),
) {
    val isResume: Boolean get() = resumeArgvJson != null

    /** What to show before Python reports the real series title. */
    val displayLabel: String
        get() = title.ifBlank { url.substringAfter("://").removeSuffix("/").ifBlank { url } }
}

/**
 * Live progress for the running job.
 *
 * [processed] / [total] / [etaMs] are STAMPED BY PYTHON at the emit site
 * (`aio_android._EtaEstimator`) — never re-derived here. DownloadService polls
 * on a 700ms timer and receives events in batches, so two chapters finishing
 * inside one interval would read as one ~0ms sample plus one full-interval
 * sample. The desktop hit the same trap; see downloader.js:applyChapterEta.
 */
data class RunProgress(
    val title: String? = null,
    val chapter: String? = null,
    val processed: Int = 0,
    val total: Int = 0,
    val etaMs: Long? = null,
    /**
     * How many chapter durations the EMA has seen. Below 2 the estimator
     * publishes nothing; at exactly 2 the UI dims the readout, because the
     * estimate is real but young.
     */
    val etaSamples: Int = 0,
    val phase: String? = null,
) {
    /** Null while the total is unknown — the progress bar reads that as indeterminate. */
    val fraction: Float? get() = if (total > 0) processed.toFloat() / total else null
}

data class ActiveRun(
    val job: DownloadJob,
    val progress: RunProgress = RunProgress(),
    val startedAt: Long = System.currentTimeMillis(),
) {
    /** The series title once known, falling back to the URL. */
    val label: String get() = progress.title?.takeIf { it.isNotBlank() } ?: job.displayLabel
}

enum class RunOutcome { Completed, Cancelled, Failed }

data class FinishedRun(
    val id: String,
    val label: String,
    val outcome: RunOutcome,
    val exitCode: Int,
    val durationMs: Long,
    /** Chapters started, out of however many were selected. */
    val processed: Int,
    val total: Int,
    val finishedAt: Long = System.currentTimeMillis(),
)

/** One classified line for the Logs screen. [id] is monotonic, for list keys. */
data class LogLine(val id: Long, val text: String, val level: LogLevel)

/**
 * Human-readable phase names. Mirrors QueueTab.jsx's `phaseLabel` so the two
 * apps describe the same moment with the same words — "Building final file…"
 * meaning something different on each platform would be its own small bug.
 */
fun phaseLabel(phase: String?): String = when (phase) {
    null, "" -> "Preparing…"
    "starting" -> "Starting…"
    "downloading" -> "Downloading chapters…"
    "resuming" -> "Resuming download…"
    "retrying" -> "Retrying missed chapters…"
    "building" -> "Building final file…"
    "saving" -> "Saving output…"
    "finishing" -> "Finishing up…"
    "done" -> "Complete"
    else -> phase
}

/**
 * Coarse on purpose — "1h 20m", not "1:19:47". The estimate is an EMA over as
 * few as two chapters, so a seconds readout past the one-minute mark advertises
 * a precision it does not have. Kept identical to DownloadService's notification
 * formatter so the notification and the Queue card never disagree.
 */
fun formatEta(ms: Long): String {
    val seconds = (ms / 1000).coerceAtLeast(0)
    val hours = seconds / 3600
    val minutes = (seconds % 3600) / 60
    return when {
        hours > 0 -> "${hours}h ${minutes}m"
        minutes > 0 -> "${minutes}m"
        else -> "${seconds}s"
    }
}

/** Elapsed time, which unlike an ETA is exact and so gets a seconds field. */
fun formatElapsed(ms: Long): String {
    val seconds = (ms / 1000).coerceAtLeast(0)
    val hours = seconds / 3600
    val minutes = (seconds % 3600) / 60
    val secs = seconds % 60
    return if (hours > 0) {
        "%d:%02d:%02d".format(hours, minutes, secs)
    } else {
        "%d:%02d".format(minutes, secs)
    }
}
