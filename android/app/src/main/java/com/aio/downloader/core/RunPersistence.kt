package com.aio.downloader.core

import org.json.JSONArray
import org.json.JSONObject

/**
 * The on-disk SHAPE of the download queue and the run history, plus the two
 * things a persisted history is FOR.
 *
 * ── WHAT LIVES HERE AND WHY IT IS ITS OWN FILE ─────────────────────────────
 * [DownloadRepository] owns the live state; this owns how that state survives
 * process death, and [RunStore] owns where the bytes go. Splitting the shape
 * from the storage is what makes every rule below testable with no device:
 * nothing in this file imports Android, and `app/build.gradle.kts` puts the
 * real `org.json` on the unit-test classpath precisely so JSON parsing can be
 * pinned offline (grep `testImplementation(libs.json)`).
 *
 * ── THE TWO CONSUMERS OF A PERSISTED HISTORY ───────────────────────────────
 *  1. The Queue screen's "Recent" list — cosmetic.
 *  2. [backfillResumable] — NOT cosmetic. `aio_android.scan_resumable` reports
 *     a `tmp_<hid>/` folder's URL out of `run_meta.json`; a folder written
 *     before aio-dl.py had that file has no URL, and a resume without a URL is
 *     impossible (aio-dl.py takes it positionally), so the row renders as
 *     un-resumable. The desktop closes exactly this hole by matching the
 *     folder's `hid` against its own history —
 *     `UI-source/electron/main.js`, the `scan-resumable` handler. This is that,
 *     in Kotlin, keyed the same way.
 *
 * ── AND THE FINAL-FILE STORY ───────────────────────────────────────────────
 * [FinalFileSkip] is the parse of aio-dl.py's `final_file_skipped` event, and
 * [finalFileHeadline] / [finalFileDetail] are the ONE place its wording is
 * written. Two readers — the terminal notification (DownloadService) and the
 * Queue history card — and they must not tell different stories about the same
 * run, which is the bug the event exists to end. See the event's emit site in
 * `aio-dl.py` (grep `final_file_skipped`) and the three console lines it sits
 * beside; the text below deliberately agrees with them.
 */

/** Bumped when a stored shape changes incompatibly; an older blob is dropped. */
const val RUN_PERSISTENCE_VERSION = 1

/**
 * What was queued, and what had just been handed to the worker, at the moment
 * the snapshot was written.
 *
 * [active] is the load-bearing half. A job leaves the queue in
 * `DownloadRepository.takeNextOrRelease` and only becomes an `ActiveRun` a
 * moment later in the service; without recording it here, a process death
 * inside that window would lose the job from BOTH sides. Restoring it puts it
 * back at the head of the queue — see the restore comment in
 * [DownloadRepository.attach] for why that is safe even when the run had
 * already downloaded chapters.
 */
data class QueueSnapshot(
    val active: DownloadJob? = null,
    val queue: List<DownloadJob> = emptyList(),
    val savedAt: Long = 0L,
) {
    val isEmpty: Boolean get() = active == null && queue.isEmpty()
}

/**
 * The queue a restored [QueueSnapshot] becomes, given whatever is already
 * queued in this process.
 *
 * Extracted from `DownloadRepository.attach` so the ordering rules are testable
 * with no Android in the loop; the repository does the locking and the flow
 * assignment around it.
 *
 * Three rules, each load-bearing:
 *  - the snapshot's ACTIVE job goes FIRST. It was the head of the queue moments
 *    before the process died, and the user's own ordering should survive.
 *  - [existing] goes LAST rather than being replaced. In practice it is empty
 *    (the restore runs before anything can enqueue), but appending means a race
 *    could only ever mis-order the queue, never drop from it.
 *  - de-duplicated by URL, which is the same identity
 *    `DownloadRepository.enqueue` refuses a second copy of.
 */
fun restoredQueue(
    snapshot: QueueSnapshot,
    existing: List<DownloadJob> = emptyList(),
): List<DownloadJob> = buildList {
    snapshot.active?.let { add(it) }
    addAll(snapshot.queue)
    addAll(existing)
}.distinctBy { it.url }

/**
 * aio-dl.py declined to (re)build the combined archive, and why.
 *
 * Field names mirror the `_emit("final_file_skipped", …)` payload verbatim —
 * grep that in aio-dl.py before renaming anything here. [reason] is an open
 * vocabulary on purpose: an older app must render a newer reason as *something*
 * rather than claiming the run was fine.
 */
data class FinalFileSkip(
    val reason: String,
    val format: String,
    val path: String,
    /** Chapters this run assembled. Can be 0 — a CBZ run seeds its content list
     *  with the cover, so the build gate is true with no chapters at all. */
    val runChapters: Int,
    /** Chapters the archive already on disk is RECORDED as covering
     *  (`.aio_series.json`'s `final_file_chapters`). 0 also means "unknown". */
    val existingChapters: Int,
    val droppedChapters: Int,
) {
    /** Just the file name; the emitted path is absolute and unreadable in a
     *  notification. Handles both separators — the Python side builds it with
     *  `os.path.join`, and this app has run against Windows-shaped paths in
     *  unit tests. */
    val fileName: String
        get() = path.substringAfterLast('/').substringAfterLast('\\')
            .ifBlank { format.uppercase().ifBlank { "the combined file" } }

    companion object {
        const val CANCELLED = "cancelled"
        const val NO_CHAPTERS = "no_chapters"

        /**
         * aio-dl.py emits `partial_coverage` (grep `_final_file_would_shrink`).
         * `subset` is accepted as an alias because the Wave-2 handoff notes
         * named it that; treating an unknown-but-plausible spelling as the same
         * case costs nothing and beats rendering the fallback wording.
         */
        const val PARTIAL_COVERAGE = "partial_coverage"
        const val PARTIAL_COVERAGE_ALIAS = "subset"
    }
}

/** True for the reason(s) that mean "this run held less than the archive does". */
private fun FinalFileSkip.isPartialCoverage(): Boolean =
    reason == FinalFileSkip.PARTIAL_COVERAGE || reason == FinalFileSkip.PARTIAL_COVERAGE_ALIAS

/**
 * One entry in the persisted history: the [FinishedRun] the Queue screen has
 * always rendered, plus the fields only a PERSISTED history needs.
 *
 * Composition rather than more fields on `FinishedRun`: the display shape is
 * shared with the live Queue card and has no business growing storage concerns,
 * and [hid] / [url] exist solely to answer [backfillResumable].
 */
data class RunRecord(
    val finished: FinishedRun,
    /** The series URL the run was started with — the back-fill's payload. */
    val url: String = "",
    /**
     * aio-dl.py's own folder id for the series, from the `series` event
     * (`_emit("series", title=…, hid=…, url=…)`). It is the ONLY key that
     * matches a `tmp_<hid>/` folder, because a folder that predates
     * `run_meta.json` has nothing else in it to match on.
     */
    val hid: String = "",
    /** Present when the run ended without rebuilding the combined archive. */
    val finalFileSkip: FinalFileSkip? = null,
)

// ── the wording ────────────────────────────────────────────────────────────

/**
 * One line, for a notification's content text or a card's status line.
 *
 * Every branch states what happened to the ARCHIVE, because that is the fact
 * the old wording got wrong in both directions: it used to say "Finished
 * chapters kept — resume to continue" while the run was silently overwriting a
 * complete archive with a partial one, and after aio-dl.py's guard landed it
 * still said nothing about the archive having been kept.
 */
fun finalFileHeadline(skip: FinalFileSkip): String = when {
    skip.reason == FinalFileSkip.CANCELLED && skip.runChapters == 0 ->
        "Stopped before a chapter finished — nothing was changed"

    skip.reason == FinalFileSkip.CANCELLED && skip.existingChapters > 0 ->
        "${skip.fileName} kept as it was — resume to add ${chapters(skip.runChapters)}"

    skip.reason == FinalFileSkip.CANCELLED ->
        "${chapters(skip.runChapters)} kept — resume to build ${skip.fileName}"

    skip.reason == FinalFileSkip.NO_CHAPTERS ->
        "No chapters this run — ${skip.fileName} left untouched"

    skip.isPartialCoverage() ->
        "Kept the ${skip.existingChapters}-chapter ${skip.fileName} — " +
            "rebuilding would have dropped ${skip.droppedChapters}"

    else -> "${skip.fileName} was left as it was"
}

/**
 * The full explanation, for an expanded notification and the history card.
 *
 * Says where this run's chapters actually ARE, because in every branch they are
 * on disk and the user's next question is whether they have to be downloaded
 * again. They do not.
 */
fun finalFileDetail(skip: FinalFileSkip): String = when {
    skip.reason == FinalFileSkip.CANCELLED && skip.runChapters == 0 ->
        "Cancelled before any chapter finished, so nothing was downloaded and " +
            "nothing on disk changed."

    skip.reason == FinalFileSkip.CANCELLED && skip.existingChapters > 0 ->
        "Cancelled, so the existing ${skip.existingChapters}-chapter " +
            "${skip.fileName} was left exactly as it was. Everything this run " +
            "downloaded (${chapters(skip.runChapters)}) is kept in the temp " +
            "folder — resume from Unfinished to finish the series and rebuild it."

    skip.reason == FinalFileSkip.CANCELLED ->
        "Cancelled, so ${skip.fileName} was not built yet. Everything this run " +
            "downloaded (${chapters(skip.runChapters)}) is kept in the temp " +
            "folder — resume from Unfinished to finish the series and build it."

    skip.reason == FinalFileSkip.NO_CHAPTERS ->
        "No chapters were assembled this run, so ${skip.fileName} was left " +
            "untouched."

    skip.isPartialCoverage() ->
        "This run covered ${chapters(skip.runChapters)}, but " +
            "${skip.fileName} already holds ${skip.existingChapters} — " +
            "rebuilding it would have dropped ${skip.droppedChapters}, so the " +
            "existing file was kept. What this run downloaded is still on disk; " +
            "download this series with every chapter selected to rebuild one " +
            "combined file."

    else ->
        "${skip.fileName} was left as it was (${skip.reason}). This run's " +
            "chapters are still on disk."
}

private fun chapters(n: Int): String = if (n == 1) "1 chapter" else "$n chapters"

// ── the resumable back-fill ────────────────────────────────────────────────

/**
 * Fill in the URL and title of any resumable folder that could not report its
 * own, from a run in [records] with the same `hid`.
 *
 * Only BLANK fields are filled: `run_meta.json` is written on every run and is
 * the fresher record, so it always wins where it exists. A row that gains a URL
 * this way becomes resumable — `ResumableRun.canResume` is exactly
 * `url.isNotBlank()`.
 */
fun backfillResumable(
    runs: List<ResumableRun>,
    records: List<RunRecord>,
): List<ResumableRun> {
    if (runs.isEmpty() || records.isEmpty()) return runs
    // First (= most recent) wins: the history list is newest-first, and a
    // series redownloaded under a new URL should resume with the new one.
    val byHid = HashMap<String, RunRecord>(records.size)
    records.forEach { record ->
        if (record.hid.isNotBlank()) byHid.putIfAbsent(record.hid, record)
    }
    if (byHid.isEmpty()) return runs

    return runs.map { run ->
        if (run.url.isNotBlank() && run.title.isNotBlank()) return@map run
        val match = byHid[run.hid] ?: return@map run
        run.copy(
            url = run.url.ifBlank { match.url },
            title = run.title.ifBlank { match.finished.label },
        )
    }
}

// ── serialization ──────────────────────────────────────────────────────────

/**
 * Every decoder here follows the same rule the rest of this app's parsers do:
 * a malformed blob yields the empty result, never an exception. The caller is a
 * process-start restore with no user in the loop and no action that could fix
 * it — losing a queue snapshot is recoverable, crashing on launch is not.
 */

private fun DownloadJob.toJson(): JSONObject = JSONObject().apply {
    put("id", id)
    put("url", url)
    put("settingsJson", settingsJson)
    resumeArgvJson?.let { put("resumeArgvJson", it) }
    if (extraArgs.isNotEmpty()) put("extraArgs", JSONArray(extraArgs))
    put("format", format)
    put("chapters", chapters)
    put("title", title)
    put("enqueuedAt", enqueuedAt)
}

private fun downloadJobFromJson(o: JSONObject): DownloadJob? {
    val url = o.optString("url")
    val id = o.optString("id")
    // A job with no URL cannot be run and a job with no id cannot be keyed in a
    // LazyColumn or removed from the queue — either way the card would be a
    // dead row, so drop it rather than restore it.
    if (url.isBlank() || id.isBlank()) return null
    val extras = o.optJSONArray("extraArgs")
    return DownloadJob(
        id = id,
        url = url,
        settingsJson = o.optString("settingsJson").ifBlank { "{}" },
        resumeArgvJson = if (o.isNull("resumeArgvJson")) null else {
            o.optString("resumeArgvJson").takeIf { it.isNotBlank() }
        },
        extraArgs = if (extras == null) {
            emptyList()
        } else {
            (0 until extras.length()).mapNotNull { extras.optString(it).takeIf(String::isNotBlank) }
        },
        format = o.optString("format"),
        chapters = o.optString("chapters"),
        title = o.optString("title"),
        enqueuedAt = o.optLong("enqueuedAt", System.currentTimeMillis()),
    )
}

fun encodeQueueSnapshot(snapshot: QueueSnapshot): String = JSONObject().apply {
    put("version", RUN_PERSISTENCE_VERSION)
    put("savedAt", snapshot.savedAt)
    snapshot.active?.let { put("active", it.toJson()) }
    put("queue", JSONArray().apply { snapshot.queue.forEach { put(it.toJson()) } })
}.toString()

fun decodeQueueSnapshot(json: String?): QueueSnapshot = runCatching {
    if (json.isNullOrBlank()) return@runCatching QueueSnapshot()
    val o = JSONObject(json)
    if (o.optInt("version") != RUN_PERSISTENCE_VERSION) return@runCatching QueueSnapshot()
    val items = o.optJSONArray("queue")
    QueueSnapshot(
        active = o.optJSONObject("active")?.let(::downloadJobFromJson),
        queue = if (items == null) {
            emptyList()
        } else {
            (0 until items.length()).mapNotNull {
                items.optJSONObject(it)?.let(::downloadJobFromJson)
            }
        },
        savedAt = o.optLong("savedAt"),
    )
}.getOrElse { QueueSnapshot() }

private fun FinalFileSkip.toJson(): JSONObject = JSONObject().apply {
    put("reason", reason)
    put("format", format)
    put("path", path)
    put("runChapters", runChapters)
    put("existingChapters", existingChapters)
    put("droppedChapters", droppedChapters)
}

/**
 * Parse one `final_file_skipped` event.
 *
 * Returns null when `reason` is missing — an event with no reason cannot be
 * rendered as anything truthful, and a wrong reassurance is the exact failure
 * this whole path exists to stop.
 */
fun parseFinalFileSkip(e: JSONObject): FinalFileSkip? {
    val reason = e.optString("reason")
    if (reason.isBlank()) return null
    return FinalFileSkip(
        reason = reason,
        format = e.optString("format"),
        path = e.optString("path"),
        runChapters = e.optInt("run_chapters"),
        existingChapters = e.optInt("existing_chapters"),
        droppedChapters = e.optInt("dropped_chapters"),
    )
}

private fun finalFileSkipFromJson(o: JSONObject): FinalFileSkip? {
    val reason = o.optString("reason")
    if (reason.isBlank()) return null
    return FinalFileSkip(
        reason = reason,
        format = o.optString("format"),
        path = o.optString("path"),
        runChapters = o.optInt("runChapters"),
        existingChapters = o.optInt("existingChapters"),
        droppedChapters = o.optInt("droppedChapters"),
    )
}

fun encodeRunRecords(records: List<RunRecord>): String = JSONObject().apply {
    put("version", RUN_PERSISTENCE_VERSION)
    put(
        "runs",
        JSONArray().apply {
            records.forEach { record ->
                put(
                    JSONObject().apply {
                        put("id", record.finished.id)
                        put("label", record.finished.label)
                        put("outcome", record.finished.outcome.name)
                        put("exitCode", record.finished.exitCode)
                        put("durationMs", record.finished.durationMs)
                        put("processed", record.finished.processed)
                        put("total", record.finished.total)
                        put("finishedAt", record.finished.finishedAt)
                        put("url", record.url)
                        put("hid", record.hid)
                        record.finalFileSkip?.let { put("finalFileSkip", it.toJson()) }
                    },
                )
            }
        },
    )
}.toString()

fun decodeRunRecords(json: String?): List<RunRecord> = runCatching {
    if (json.isNullOrBlank()) return@runCatching emptyList()
    val o = JSONObject(json)
    if (o.optInt("version") != RUN_PERSISTENCE_VERSION) return@runCatching emptyList()
    val runs = o.optJSONArray("runs") ?: return@runCatching emptyList()
    (0 until runs.length()).mapNotNull { index ->
        val item = runs.optJSONObject(index) ?: return@mapNotNull null
        val id = item.optString("id")
        if (id.isBlank()) return@mapNotNull null
        RunRecord(
            finished = FinishedRun(
                id = id,
                label = item.optString("label"),
                // An unrecognized outcome reads as Failed rather than
                // Completed: over-reporting success is the one direction that
                // would hide a problem from the user.
                outcome = runCatching { RunOutcome.valueOf(item.optString("outcome")) }
                    .getOrDefault(RunOutcome.Failed),
                exitCode = item.optInt("exitCode"),
                durationMs = item.optLong("durationMs"),
                processed = item.optInt("processed"),
                total = item.optInt("total"),
                finishedAt = item.optLong("finishedAt"),
            ),
            url = item.optString("url"),
            hid = item.optString("hid"),
            finalFileSkip = item.optJSONObject("finalFileSkip")?.let(::finalFileSkipFromJson),
        )
    }
}.getOrElse { emptyList() }
