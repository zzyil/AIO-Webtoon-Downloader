package com.aio.downloader.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Parsing and presenting `aio_android.scan_resumable`'s payload.
 *
 * WHY THE PARSE IS WORTH PINNING: a renamed key here does not throw, it makes
 * the Unfinished section quietly empty — and an empty section is exactly what
 * "there is nothing to resume" looks like, so the bug is invisible. Every field
 * is read by string from a payload built three layers away in Python.
 *
 * [ResumableRun.effectiveFormat] carries the highest-stakes default in this
 * file. It is emitted as `--format` on every resume, and aio-dl.py's argparse
 * defaults that flag to "epub" — so a folder whose format is unknown must fall
 * back to something deliberate, or the resume silently converts a CBZ library.
 *
 * Cross-file: aio_android.scan_resumable (the producer),
 * tests/test_android_resume.py (the Python half), ui/screens/QueueScreen.kt.
 */
class ResumeModelsTest {

    private fun payload(vararg items: String) = """{"root":"/w","items":[${items.joinToString(",")}]}"""

    private val fullItem = """
        {"hid":"abc123","tmpDir":"/w/tmp_abc123","folderName":"tmp_abc123",
         "url":"https://site.test/x","title":"Some Series","format":"cbz",
         "language":"ja","quality":85,"scaling":90,
         "cachedChapters":12,"sizeBytes":52428800,"modifiedAt":1700000000000}
    """.trimIndent()

    // ── happy path ─────────────────────────────────────────────────────────

    @Test
    fun `parses a full item`() {
        val runs = parseResumable(payload(fullItem))
        assertEquals(1, runs.size)
        val run = runs.first()
        assertEquals("abc123", run.hid)
        assertEquals("/w/tmp_abc123", run.tmpDir)
        assertEquals("https://site.test/x", run.url)
        assertEquals("Some Series", run.title)
        assertEquals("cbz", run.format)
        assertEquals(12, run.cachedChapters)
        assertEquals(52428800L, run.sizeBytes)
        assertEquals(85, run.quality)
        assertTrue(run.canResume)
    }

    @Test
    fun `an empty list is a result, not a failure`() {
        assertTrue(parseResumable("""{"root":"/w","items":[]}""").isEmpty())
    }

    @Test
    fun `malformed json yields nothing rather than throwing`() {
        // The caller is a StateFlow feeding a screen; there is no user action
        // that could recover from an exception here.
        assertTrue(parseResumable("not json").isEmpty())
        assertTrue(parseResumable("").isEmpty())
        assertTrue(parseResumable("{}").isEmpty())
    }

    @Test
    fun `an item with no tmpDir is dropped`() {
        // Without a directory both buttons on the card are dead.
        val runs = parseResumable(payload("""{"hid":"x","tmpDir":""}""", fullItem))
        assertEquals(1, runs.size)
        assertEquals("abc123", runs.first().hid)
    }

    @Test
    fun `missing optional fields take their defaults`() {
        val runs = parseResumable(payload("""{"hid":"x","tmpDir":"/w/tmp_x"}"""))
        val run = runs.first()
        assertEquals("", run.url)
        assertEquals(0, run.cachedChapters)
        assertEquals(0L, run.sizeBytes)
        assertNull(run.quality)
        assertFalse(run.canResume)
    }

    @Test
    fun `an explicitly null quality is null, not zero`() {
        // run_params.json carries JSON null for a knob the run never set, and
        // "Q0" is a real-looking quality that would render on the card.
        val runs = parseResumable(payload("""{"tmpDir":"/w/tmp_x","quality":null,"scaling":null}"""))
        assertNull(runs.first().quality)
        assertNull(runs.first().scaling)
    }

    // ── the format fallback ────────────────────────────────────────────────

    @Test
    fun `an unknown format resolves to cbz, never to argparse's epub`() {
        for (raw in listOf("", "   ", "zzz", "PDF ")) {
            val runs = parseResumable(payload("""{"tmpDir":"/w/tmp_x","format":"$raw"}"""))
            assertEquals(raw, "cbz", runs.first().effectiveFormat)
        }
    }

    @Test
    fun `a known format survives, case-insensitively`() {
        for ((raw, expected) in listOf("cbz" to "cbz", "EPUB" to "epub", "pdf" to "pdf", "none" to "none")) {
            val runs = parseResumable(payload("""{"tmpDir":"/w/tmp_x","format":"$raw"}"""))
            assertEquals(raw, expected, runs.first().effectiveFormat)
        }
    }

    // ── presentation ───────────────────────────────────────────────────────

    @Test
    fun `the label prefers the title, then the url, then the folder`() {
        fun labelOf(json: String) = parseResumable(payload(json)).first().label
        assertEquals("Some Series", labelOf(fullItem))
        assertEquals(
            "site.test/x",
            labelOf("""{"tmpDir":"/w/tmp_x","url":"https://site.test/x/"}"""),
        )
        assertEquals(
            "tmp_x",
            labelOf("""{"tmpDir":"/w/tmp_x","folderName":"tmp_x"}"""),
        )
    }

    @Test
    fun `size reads in the unit a human would use`() {
        fun sizeOf(bytes: Long) =
            parseResumable(payload("""{"tmpDir":"/w/tmp_x","sizeBytes":$bytes}""")).first().sizeLabel
        assertEquals("512 B", sizeOf(512))
        assertEquals("2 KB", sizeOf(2048))
        assertEquals("50 MB", sizeOf(52_428_800))
        assertEquals("1.5 GB", sizeOf(1_610_612_736))
    }

    @Test
    fun `a run with no url cannot be resumed`() {
        // aio-dl.py takes the series URL positionally, so there is nothing to
        // build a command out of. The card says so instead of offering a button
        // that fails at the Python boundary.
        assertFalse(parseResumable(payload("""{"tmpDir":"/w/tmp_x"}""")).first().canResume)
    }

    // ── what the queue hides ───────────────────────────────────────────────

    @Test
    fun `the running download's folder is not offered as resumable`() {
        val all = parseResumable(payload(fullItem))
        val active = ActiveRun(
            DownloadJob(id = "1", url = "https://site.test/x", settingsJson = "{}"),
        )
        assertTrue(ResumeRepository.visible(all, active, emptyList()).isEmpty())
    }

    @Test
    fun `an already-queued url is not offered either`() {
        val all = parseResumable(payload(fullItem))
        val queued = listOf(
            DownloadJob(id = "1", url = "https://site.test/x", settingsJson = "{}"),
        )
        assertTrue(ResumeRepository.visible(all, null, queued).isEmpty())
    }

    @Test
    fun `an unrelated download hides nothing`() {
        val all = parseResumable(payload(fullItem))
        val active = ActiveRun(
            DownloadJob(id = "1", url = "https://other.test/z", settingsJson = "{}"),
        )
        assertEquals(1, ResumeRepository.visible(all, active, emptyList()).size)
    }

    @Test
    fun `a url-less run is never hidden by a blank match`() {
        // Two folders with no URL must not cancel each other out through an
        // empty-string comparison.
        val all = parseResumable(payload("""{"tmpDir":"/w/tmp_x"}"""))
        val active = ActiveRun(DownloadJob(id = "1", url = "", settingsJson = "{}"))
        assertEquals(1, ResumeRepository.visible(all, active, emptyList()).size)
    }

    @Test
    fun `visible tolerates a null scan`() {
        assertTrue(ResumeRepository.visible(null, null, emptyList()).isEmpty())
    }

    // ── error vocabulary ───────────────────────────────────────────────────

    @Test
    fun `every delete refusal becomes prose, never a raw code`() {
        for (code in listOf(
            "refused_root", "outside_work_dir", "not_a_tmp_dir", "not_found", "not_configured",
        )) {
            val message = resumeDeleteErrorMessage(code)
            assertFalse("raw code leaked for $code: $message", message.contains(code))
            assertTrue(message.isNotBlank())
        }
    }

    @Test
    fun `an unknown refusal still reads as a sentence`() {
        assertTrue(resumeDeleteErrorMessage("some_new_code").isNotBlank())
    }

    // ── the job a resume becomes ───────────────────────────────────────────

    @Test
    fun `a resume job is marked as one and labels itself by title`() {
        val job = DownloadJob(
            id = "1",
            url = "https://site.test/x",
            settingsJson = "{}",
            resumeArgvJson = """["--restore-parameters","--format","cbz","https://site.test/x"]""",
            title = "Some Series",
        )
        assertTrue(job.isResume)
        // Without the title a queued resume would show a bare URL until the run
        // reaches its own `series` event — which for a resume is well after the
        // cached chapters have been collected.
        assertEquals("Some Series", job.displayLabel)
    }

    @Test
    fun `an ordinary job is not a resume and falls back to the url`() {
        val job = DownloadJob(id = "1", url = "https://site.test/x/", settingsJson = "{}")
        assertFalse(job.isResume)
        assertEquals("site.test/x", job.displayLabel)
    }
}
