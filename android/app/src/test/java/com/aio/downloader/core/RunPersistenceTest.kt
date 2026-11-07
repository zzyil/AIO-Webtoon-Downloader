package com.aio.downloader.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Offline coverage for the queue/history persistence layer.
 *
 * WHY THIS IS WORTH PINNING: the whole feature is invisible when it works and
 * invisible when it fails. A dropped key in [encodeQueueSnapshot] does not
 * throw — it produces an app that comes back with an empty queue, which is
 * exactly what the bug being fixed looked like in the first place. The same
 * goes for [parseFinalFileSkip], whose keys are snake_case because they come
 * straight off aio-dl.py's `_emit("final_file_skipped", …)` payload: read
 * `runChapters` instead of `run_chapters` and every message silently says
 * "0 chapters".
 *
 * Plain JVM tests. `core/RunPersistence.kt` imports nothing from Android on
 * purpose, and `app/build.gradle.kts` puts the real `org.json` on the unit-test
 * classpath (the android.jar stub throws from every method), so all of this
 * runs with no device.
 *
 * Cross-file: core/DownloadRepository.kt (the only writer), core/RunStore.kt
 * (where the strings land), aio-dl.py (grep `final_file_skipped`).
 */
class RunPersistenceTest {

    private fun job(
        id: String = "job-1",
        url: String = "https://site.test/series",
        resumeArgv: String? = null,
        extras: List<String> = emptyList(),
    ) = DownloadJob(
        id = id,
        url = url,
        settingsJson = """{"format":"cbz","chapters":"1-5"}""",
        resumeArgvJson = resumeArgv,
        extraArgs = extras,
        format = "cbz",
        chapters = "1-5",
        title = "Some Series",
        enqueuedAt = 1_700_000_000_000L,
    )

    // ── queue snapshot ─────────────────────────────────────────────────────

    @Test
    fun `a queue snapshot round-trips every field`() {
        val snapshot = QueueSnapshot(
            active = job(id = "running", url = "https://site.test/a"),
            queue = listOf(
                job(id = "q1", url = "https://site.test/b"),
                job(
                    id = "q2",
                    url = "https://site.test/c",
                    resumeArgv = """["--restore-parameters","--format","cbz"]""",
                    extras = listOf("--verbose", "--no-fast-download"),
                ),
            ),
            savedAt = 1_700_000_123_456L,
        )

        val restored = decodeQueueSnapshot(encodeQueueSnapshot(snapshot))

        assertEquals(snapshot.active, restored.active)
        assertEquals(snapshot.queue, restored.queue)
        assertEquals(snapshot.savedAt, restored.savedAt)
        // The two fields with the most ways to go wrong: a resume with no argv
        // would re-run as a fresh download, and lost extras would silently drop
        // whatever the harness was testing.
        assertEquals(
            """["--restore-parameters","--format","cbz"]""",
            restored.queue[1].resumeArgvJson,
        )
        assertEquals(listOf("--verbose", "--no-fast-download"), restored.queue[1].extraArgs)
        assertTrue(restored.queue[0].resumeArgvJson == null)
    }

    @Test
    fun `an empty snapshot round-trips as empty`() {
        val restored = decodeQueueSnapshot(encodeQueueSnapshot(QueueSnapshot()))
        assertTrue(restored.isEmpty)
        assertNull(restored.active)
    }

    @Test
    fun `the active job is what closes the dequeue window`() {
        // DownloadRepository.takeNextOrRelease persists the job it just removed
        // from the queue as `active`, in the same locked step. A restore has to
        // put it back or the job existed nowhere.
        val restored = decodeQueueSnapshot(
            encodeQueueSnapshot(QueueSnapshot(active = job(id = "mid-flight"))),
        )
        assertEquals("mid-flight", restored.active?.id)
        assertTrue(restored.queue.isEmpty())
    }

    @Test
    fun `garbage decodes to an empty snapshot instead of throwing`() {
        assertTrue(decodeQueueSnapshot("not json at all").isEmpty)
        assertTrue(decodeQueueSnapshot("").isEmpty)
        assertTrue(decodeQueueSnapshot(null).isEmpty)
        assertTrue(decodeQueueSnapshot("[]").isEmpty)
    }

    @Test
    fun `a snapshot from a future version is discarded, not half-read`() {
        val forward = """{"version":99,"queue":[{"id":"x","url":"https://x.test/"}]}"""
        assertTrue(decodeQueueSnapshot(forward).isEmpty)
    }

    @Test
    fun `a job with no url or no id is dropped`() {
        // Either one makes the card a dead row: no URL is nothing to run, no id
        // is nothing to key or remove.
        val blob = """
            {"version":$RUN_PERSISTENCE_VERSION,"queue":[
              {"id":"ok","url":"https://site.test/a"},
              {"id":"","url":"https://site.test/b"},
              {"id":"c","url":""}
            ]}
        """.trimIndent()
        val restored = decodeQueueSnapshot(blob)
        assertEquals(1, restored.queue.size)
        assertEquals("ok", restored.queue.first().id)
    }

    // ── the restore merge ──────────────────────────────────────────────────

    @Test
    fun `the run that was active comes back at the head`() {
        val restored = restoredQueue(
            QueueSnapshot(
                active = job(id = "was-running", url = "https://site.test/a"),
                queue = listOf(
                    job(id = "q1", url = "https://site.test/b"),
                    job(id = "q2", url = "https://site.test/c"),
                ),
            ),
        )
        assertEquals(listOf("was-running", "q1", "q2"), restored.map { it.id })
    }

    @Test
    fun `anything already queued in this process is kept, behind the restore`() {
        val live = listOf(job(id = "live", url = "https://site.test/z"))
        val restored = restoredQueue(
            QueueSnapshot(queue = listOf(job(id = "q1", url = "https://site.test/b"))),
            existing = live,
        )
        // Appended rather than replaced: a race between the restore and an
        // enqueue may only ever mis-order the queue, never drop from it.
        assertEquals(listOf("q1", "live"), restored.map { it.id })
    }

    @Test
    fun `the same URL cannot come back twice`() {
        // Same identity `enqueue` refuses a second copy of. The active job wins,
        // because it is the one that was furthest along.
        val restored = restoredQueue(
            QueueSnapshot(
                active = job(id = "active", url = "https://site.test/same"),
                queue = listOf(job(id = "dupe", url = "https://site.test/same")),
            ),
            existing = listOf(job(id = "live-dupe", url = "https://site.test/same")),
        )
        assertEquals(listOf("active"), restored.map { it.id })
    }

    @Test
    fun `an empty snapshot restores nothing`() {
        assertTrue(restoredQueue(QueueSnapshot()).isEmpty())
    }

    // ── history ────────────────────────────────────────────────────────────

    private fun record(
        id: String = "r1",
        outcome: RunOutcome = RunOutcome.Completed,
        hid: String = "abc123",
        skip: FinalFileSkip? = null,
    ) = RunRecord(
        finished = FinishedRun(
            id = id,
            label = "Some Series",
            outcome = outcome,
            exitCode = if (outcome == RunOutcome.Completed) 0 else 130,
            durationMs = 91_000L,
            processed = 12,
            total = 40,
            finishedAt = 1_700_000_999_000L,
        ),
        url = "https://site.test/series",
        hid = hid,
        finalFileSkip = skip,
    )

    @Test
    fun `history round-trips including the final-file skip`() {
        val records = listOf(
            record(id = "r1"),
            record(
                id = "r2",
                outcome = RunOutcome.Cancelled,
                skip = FinalFileSkip(
                    reason = FinalFileSkip.CANCELLED,
                    format = "cbz",
                    path = "/data/manga/Some Series/Some Series.cbz",
                    runChapters = 3,
                    existingChapters = 40,
                    droppedChapters = 0,
                ),
            ),
        )

        val restored = decodeRunRecords(encodeRunRecords(records))

        assertEquals(records, restored)
        assertEquals(40, restored[1].finalFileSkip?.existingChapters)
    }

    @Test
    fun `an unrecognized outcome reads as Failed`() {
        // Over-reporting success is the one direction that hides a problem.
        val blob = """
            {"version":$RUN_PERSISTENCE_VERSION,"runs":[
              {"id":"r","label":"X","outcome":"Warped","exitCode":7}
            ]}
        """.trimIndent()
        assertEquals(RunOutcome.Failed, decodeRunRecords(blob).single().finished.outcome)
    }

    @Test
    fun `garbage history decodes to an empty list`() {
        assertTrue(decodeRunRecords("{{{").isEmpty())
        assertTrue(decodeRunRecords(null).isEmpty())
        assertTrue(decodeRunRecords("""{"version":99,"runs":[{"id":"a"}]}""").isEmpty())
    }

    // ── the resumable back-fill ────────────────────────────────────────────

    private fun resumable(hid: String, url: String = "", title: String = "") = ResumableRun(
        hid = hid,
        tmpDir = "/w/tmp_$hid",
        folderName = "tmp_$hid",
        url = url,
        title = title,
        format = "cbz",
        language = "en",
        quality = null,
        scaling = null,
        cachedChapters = 4,
        sizeBytes = 1024,
        modifiedAt = 1L,
    )

    @Test
    fun `a folder with no url becomes resumable when history knows the hid`() {
        val before = resumable("abc123")
        assertTrue(!before.canResume)

        val after = backfillResumable(listOf(before), listOf(record(hid = "abc123"))).single()

        assertEquals("https://site.test/series", after.url)
        assertEquals("Some Series", after.title)
        assertTrue(after.canResume)
    }

    @Test
    fun `run_meta wins where it exists`() {
        // scan_resumable reads run_meta.json, which aio-dl.py rewrites on every
        // run. History is the older record and must never overwrite it.
        val fresh = resumable("abc123", url = "https://new.test/x", title = "New Title")
        val after = backfillResumable(listOf(fresh), listOf(record(hid = "abc123"))).single()
        assertEquals("https://new.test/x", after.url)
        assertEquals("New Title", after.title)
    }

    @Test
    fun `the newest matching run wins`() {
        val records = listOf(
            record(id = "new", hid = "abc123").copy(url = "https://site.test/new"),
            record(id = "old", hid = "abc123").copy(url = "https://site.test/old"),
        )
        val after = backfillResumable(listOf(resumable("abc123")), records).single()
        assertEquals("https://site.test/new", after.url)
    }

    @Test
    fun `no match leaves the row exactly as it was`() {
        val rows = listOf(resumable("nomatch"))
        assertSame(rows, backfillResumable(rows, emptyList()))
        assertEquals(rows, backfillResumable(rows, listOf(record(hid = "other"))))
        // A record with no hid can never match anything, including a blank hid.
        assertEquals(rows, backfillResumable(rows, listOf(record(hid = ""))))
    }

    // ── the final_file_skipped event ───────────────────────────────────────

    @Test
    fun `the event is parsed with aio-dl's own snake_case keys`() {
        val event = JSONObject(
            """
            {"kind":"final_file_skipped","reason":"partial_coverage","format":"cbz",
             "path":"/data/manga/S/S.cbz","run_chapters":3,"existing_chapters":50,
             "dropped_chapters":47}
            """.trimIndent(),
        )
        val skip = parseFinalFileSkip(event)
        assertNotNull(skip)
        assertEquals("partial_coverage", skip!!.reason)
        assertEquals(3, skip.runChapters)
        assertEquals(50, skip.existingChapters)
        assertEquals(47, skip.droppedChapters)
        assertEquals("S.cbz", skip.fileName)
    }

    @Test
    fun `an event with no reason is not rendered at all`() {
        // There is nothing truthful to say about it, and a wrong reassurance is
        // the exact failure this path exists to end.
        assertNull(parseFinalFileSkip(JSONObject("""{"kind":"final_file_skipped"}""")))
    }

    @Test
    fun `the file name survives either path separator`() {
        val posix = skipOf(path = "/data/manga/S/S.cbz")
        val windows = skipOf(path = """C:\manga\S\S.cbz""")
        assertEquals("S.cbz", posix.fileName)
        assertEquals("S.cbz", windows.fileName)
        // Nothing usable in the path — say something rather than nothing.
        assertEquals("CBZ", skipOf(path = "").fileName)
    }

    private fun skipOf(
        reason: String = FinalFileSkip.PARTIAL_COVERAGE,
        path: String = "/m/S.cbz",
        runChapters: Int = 3,
        existingChapters: Int = 50,
        droppedChapters: Int = 47,
    ) = FinalFileSkip(
        reason = reason,
        format = "cbz",
        path = path,
        runChapters = runChapters,
        existingChapters = existingChapters,
        droppedChapters = droppedChapters,
    )

    // ── the wording ────────────────────────────────────────────────────────

    @Test
    fun `a cancelled run says the archive was KEPT, not that chapters were kept`() {
        // The old string was "Finished chapters kept - resume to continue",
        // which said nothing about the file that was being overwritten at the
        // time. Both halves have to appear now.
        val detail = finalFileDetail(skipOf(reason = FinalFileSkip.CANCELLED, runChapters = 3))
        assertTrue(detail.contains("50-chapter"))
        assertTrue(detail.contains("S.cbz"))
        assertTrue(detail.contains("temp folder"))
        assertTrue(detail.contains("resume", ignoreCase = true))
        assertTrue(finalFileHeadline(skipOf(reason = FinalFileSkip.CANCELLED)).contains("kept"))
    }

    @Test
    fun `a cancelled run with nothing on disk yet does not claim a file was kept`() {
        val skip = skipOf(
            reason = FinalFileSkip.CANCELLED,
            runChapters = 3,
            existingChapters = 0,
        )
        assertTrue(!finalFileDetail(skip).contains("0-chapter"))
        assertTrue(finalFileDetail(skip).contains("was not built"))
    }

    @Test
    fun `a cancel before any chapter finished claims nothing at all`() {
        val skip = skipOf(
            reason = FinalFileSkip.CANCELLED,
            runChapters = 0,
            existingChapters = 0,
        )
        assertTrue(finalFileDetail(skip).contains("nothing on disk changed"))
        assertTrue(!finalFileHeadline(skip).contains("resume"))
    }

    @Test
    fun `a partial-coverage skip is not reported as a cancel`() {
        // It fires on a run that COMPLETED — a delta download of three chapters
        // against a fifty-chapter archive. Calling that "cancelled" is the
        // mirror image of the bug being fixed.
        val skip = skipOf()
        listOf(finalFileHeadline(skip), finalFileDetail(skip)).forEach {
            assertTrue(!it.contains("ancel"))
        }
        assertTrue(finalFileDetail(skip).contains("47"))
        assertTrue(finalFileDetail(skip).contains("every chapter"))
    }

    @Test
    fun `subset is accepted as an alias for partial_coverage`() {
        // aio-dl.py emits `partial_coverage`; the Wave-2 handoff notes named it
        // `subset`. Rendering the alias the same way costs nothing and beats
        // falling through to the generic wording.
        assertEquals(
            finalFileDetail(skipOf(reason = FinalFileSkip.PARTIAL_COVERAGE)),
            finalFileDetail(skipOf(reason = FinalFileSkip.PARTIAL_COVERAGE_ALIAS)),
        )
    }

    @Test
    fun `an unknown reason still says what happened to the file`() {
        // The event vocabulary is open; an older app must degrade to something
        // true rather than claim the run was fine.
        val detail = finalFileDetail(skipOf(reason = "some_future_reason"))
        assertTrue(detail.contains("S.cbz"))
        assertTrue(detail.contains("still on disk"))
        assertTrue(finalFileHeadline(skipOf(reason = "some_future_reason")).contains("S.cbz"))
    }

    @Test
    fun `chapter counts are singular where they should be`() {
        val one = skipOf(reason = FinalFileSkip.CANCELLED, runChapters = 1)
        assertTrue(finalFileDetail(one).contains("(1 chapter)"))
        assertTrue(finalFileHeadline(one).contains("add 1 chapter"))
        listOf(finalFileDetail(one), finalFileHeadline(one)).forEach {
            assertTrue(!it.contains("1 chapters"))
        }
    }
}
