package com.aio.downloader.core

import org.json.JSONObject

/**
 * One unfinished download found on disk, and the parse of
 * `aio_android.scan_resumable`'s payload.
 *
 * WHAT "UNFINISHED" MEANS: a `tmp_<hid>/` directory holding a
 * `run_params.json` — aio-dl.py's own record that a run got far enough to be
 * continuable. Resuming re-runs it with `--restore-parameters`, which reads
 * every persisted setting back out of that file, so the resume CLI carries
 * almost nothing (see aio_android.build_resume_argv for the two exceptions and
 * why each one matters).
 *
 * Cross-file: aio_android.scan_resumable (the producer),
 * UI-source/electron/downloader.js:scanResumable (the behaviour ported),
 * ui/screens/QueueScreen.kt (the only renderer).
 */
data class ResumableRun(
    val hid: String,
    val tmpDir: String,
    val folderName: String,
    /**
     * From `run_meta.json`, which aio-dl.py writes on EVERY run. Blank for a
     * tmp folder old enough to predate that file — and a resume without a URL
     * is impossible, since aio-dl.py takes it positionally. [canResume] gates
     * on it rather than letting the button fail at the Python boundary.
     */
    val url: String,
    val title: String,
    /** The format the original run used. Overridable before resuming. */
    val format: String,
    val language: String,
    val quality: Int?,
    val scaling: Int?,
    /** Chapters already complete on disk, which a resume will not re-download. */
    val cachedChapters: Int,
    val sizeBytes: Long,
    val modifiedAt: Long,
) {
    val canResume: Boolean get() = url.isNotBlank()

    /** The series name if the run got far enough to learn it, else the folder. */
    val label: String
        get() = title.takeIf { it.isNotBlank() }
            ?: url.substringAfter("://").removeSuffix("/").takeIf { it.isNotBlank() }
            ?: folderName

    /**
     * The format to resume with, never blank.
     *
     * CBZ rather than argparse's "epub" default: this value is emitted as
     * `--format` on every resume, and a tmp folder with no run_meta.json would
     * otherwise convert the run to EPUB. `aio_android.build_resume_argv`
     * applies the same fallback independently — belt and braces, because the
     * failure is silent and produces files the reader cannot open.
     */
    val effectiveFormat: String
        get() = format.lowercase().takeIf { it in DownloadForm.FORMATS } ?: "cbz"

    /** Human size for the delete affordance. A button that cannot say what it
     *  reclaims is not worth tapping. */
    val sizeLabel: String
        get() = when {
            sizeBytes >= 1_073_741_824 -> "%.1f GB".format(sizeBytes / 1_073_741_824.0)
            sizeBytes >= 1_048_576 -> "%.0f MB".format(sizeBytes / 1_048_576.0)
            sizeBytes >= 1024 -> "%.0f KB".format(sizeBytes / 1024.0)
            else -> "$sizeBytes B"
        }
}

/**
 * Parse `{"root":…, "items":[…]}`.
 *
 * Never throws: a malformed payload yields an empty list, because the caller is
 * a StateFlow feeding a screen and there is no user action that could fix it.
 * A missing field takes its default — the Python side gains keys over time and
 * an older parse must not break on one it has not seen.
 */
fun parseResumable(json: String): List<ResumableRun> = runCatching {
    val items = JSONObject(json).optJSONArray("items") ?: return@runCatching emptyList()
    (0 until items.length()).mapNotNull { index ->
        val o = items.optJSONObject(index) ?: return@mapNotNull null
        val tmpDir = o.optString("tmpDir")
        // Without a directory there is nothing to resume OR delete, so the row
        // would be a card with two dead buttons.
        if (tmpDir.isBlank()) return@mapNotNull null
        ResumableRun(
            hid = o.optString("hid"),
            tmpDir = tmpDir,
            folderName = o.optString("folderName"),
            url = o.optString("url"),
            title = o.optString("title"),
            format = o.optString("format"),
            language = o.optString("language"),
            quality = if (o.isNull("quality")) null else o.optInt("quality"),
            scaling = if (o.isNull("scaling")) null else o.optInt("scaling"),
            cachedChapters = o.optInt("cachedChapters"),
            sizeBytes = o.optLong("sizeBytes"),
            modifiedAt = o.optLong("modifiedAt"),
        )
    }
}.getOrElse { emptyList() }

/** Turn `delete_resumable`'s error vocabulary into something a user can read. */
fun resumeDeleteErrorMessage(code: String): String = when (code) {
    "refused_root" -> "That's the working folder itself, not one download"
    "outside_work_dir" -> "That folder isn't one of this app's downloads"
    "not_a_tmp_dir" -> "That isn't an unfinished download"
    "not_found" -> "Already gone"
    "not_configured" -> "The downloader isn't set up yet"
    else -> "Couldn't discard it ($code)"
}
