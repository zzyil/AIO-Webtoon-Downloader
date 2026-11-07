package com.aio.downloader.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Offline coverage for the [LogFilter] port of
 * `UI-source/electron/log-filter.js`.
 *
 * WHY THESE CASES: every one is a rule that file's comments call out BY NAME as
 * the reason a pattern is written the way it is. They are the regressions the
 * desktop already paid for, so they are exactly what a port has to be pinned
 * against — "First variant failed, trying 9 more" turning the Logs screen red
 * is a bug that has shipped once already.
 *
 * Plain JVM tests: LogFilter.kt imports nothing from Android on purpose, so
 * these run under `gradlew test` with no device and no emulator.
 */
class LogFilterTest {

    private val esc = "\u001B"

    // ── stripAnsi ──────────────────────────────────────────────────────────

    @Test
    fun `strips SGR colour escapes`() {
        // Trim AFTER stripping, never before: ESC is not whitespace by
        // Character.isWhitespace, so a leading escape blocks trim() entirely.
        // This is also why sanitizeLogLine's blank check runs post-strip.
        assertEquals("errno: -4047", stripAnsi("${esc}[90m errno: -4047${esc}[39m").trim())
        assertEquals("plain", stripAnsi("${esc}[0m${esc}[1mplain${esc}[0m"))
    }

    @Test
    fun `passes through lines with no escape byte unchanged`() {
        val line = "[i] Chapter 12 complete 40/40"
        // Identity, not just equality: the fast path must not reallocate.
        assertTrue(stripAnsi(line) === line)
    }

    // ── NOISY_LINE_RE ──────────────────────────────────────────────────────

    @Test
    fun `drops the Playwright teardown crash dump`() {
        val noisy = listOf(
            "Error: write EPIPE",
            "BrokenPipeError: [Errno 32] Broken pipe",
            "ConnectionResetError: [Errno 104] Connection reset by peer",
            "    at Socket._write (node:internal/streams/writable:1)",
            "    at Immediate (node:events:518)",
            "  at PipeTransport.send (playwright/driver/package/lib/x.js:1)",
            "    at DispatcherConnection.onmessage (x.js:1)",
            "    at BrowserContextDispatcher._dispose (x.js:1)",
            "    at CRBrowserContext.close (x.js:1)",
            "      throw er; // Unhandled 'error' event",
            "Emitted 'error' event on Socket instance at:",
            "  errno: -4047,",
            "  syscall: 'write',",
            "Node.js v20.11.0",
            "        ^",
            "  {",
            "}",
            "},",
        )
        noisy.forEach {
            assertTrue("should have been dropped: $it", NOISY_LINE_RE.containsMatchIn(it))
        }
    }

    @Test
    fun `keeps ordinary download output`() {
        val keep = listOf(
            "[i] Chapter 3 complete 44/44",
            "Fetching chapter list…",
            "Selected group: Official",
            "  page 12/44",
            "First variant failed, trying 9 more",
            "Saved manga/Anne Shirley/Ch.003.cbz",
        )
        keep.forEach {
            assertTrue("should have been kept: $it", !NOISY_LINE_RE.containsMatchIn(it))
        }
    }

    @Test
    fun `noise regex has no global-flag iteration state`() {
        // The JS original is deliberately non-global so `.test()` never advances
        // lastIndex across shared calls. Kotlin Regex is stateless, but assert
        // the OBSERVABLE property so the port can never regress into a Matcher
        // that is reused across lines.
        val line = "Error: write EPIPE"
        repeat(5) { assertTrue(NOISY_LINE_RE.containsMatchIn(line)) }
    }

    // ── classifyLogLevel: the strict error rules ───────────────────────────

    @Test
    fun `bracket-bang is an error, anchored to the trimmed start`() {
        assertEquals(LogLevel.Error, classifyLogLevel("[!] chapter 12 failed after 6 retries"))
        assertEquals(LogLevel.Error, classifyLogLevel("   [!] indented but still an error"))
        // The anchor is why "[!?]" does not fire.
        assertEquals(LogLevel.Info, classifyLogLevel("[!?] not really"))
        // ...and why a mid-line occurrence does not either.
        assertEquals(LogLevel.Info, classifyLogLevel("see [!] in the log above"))
    }

    @Test
    fun `python crash headers and exception lines are errors`() {
        assertEquals(LogLevel.Error, classifyLogLevel("Traceback (most recent call last):"))
        assertEquals(LogLevel.Error, classifyLogLevel("ImportError: No module named rapidfuzz"))
        assertEquals(LogLevel.Error, classifyLogLevel("ValueError: bad chapter spec"))
        assertEquals(LogLevel.Error, classifyLogLevel("Error: something broke"))
    }

    @Test
    fun `lowercase failed is NOT an error but uppercase FAILED is`() {
        // THE regression this whole strictness exists for: an earlier
        // /error:|FAILED/i painted hundreds of benign retry lines red.
        assertEquals(LogLevel.Info, classifyLogLevel("First variant failed, trying 9 more"))
        assertEquals(LogLevel.Info, classifyLogLevel("chapter 4 failed, falling back to alt source"))
        assertEquals(LogLevel.Error, classifyLogLevel("3 tests FAILED"))
    }

    // ── classifyLogLevel: the remaining branches, in priority order ─────────

    @Test
    fun `warnings are matched case-insensitively and include the glyph`() {
        assertEquals(LogLevel.Warning, classifyLogLevel("Warning: no cover found"))
        assertEquals(LogLevel.Warning, classifyLogLevel("warning: falling back to PIL"))
        assertEquals(LogLevel.Warning, classifyLogLevel("⚠ animated pages will be flattened"))
    }

    @Test
    fun `download success markers turn green`() {
        listOf("Done.", "saved → manga/X/Ch.001.cbz", "✓ chapter 4", "Completed 40 chapters", "recovered 2 missed")
            .forEach { assertEquals("for: $it", LogLevel.Success, classifyLogLevel(it)) }
    }

    @Test
    fun `indented lines are verbose detail`() {
        assertEquals(LogLevel.Verbose, classifyLogLevel("    page 12/44 -> 812 KB"))
        // One space is not enough — the rule is two or more.
        assertEquals(LogLevel.Info, classifyLogLevel(" single space stays info"))
    }

    @Test
    fun `error beats warning beats success beats verbose`() {
        // A line hitting several branches must resolve by the documented order.
        assertEquals(LogLevel.Error, classifyLogLevel("  [!] Warning: Done. all at once"))
        assertEquals(LogLevel.Warning, classifyLogLevel("  Warning: Done."))
        assertEquals(LogLevel.Success, classifyLogLevel("  Done."))
    }

    @Test
    fun `everything else is info`() {
        assertEquals(LogLevel.Info, classifyLogLevel("Fetching chapter list…"))
    }

    @Test
    fun `search callers can inject their own success set`() {
        val searchRe = Regex("""--auto-pick selected|alignment""", RegexOption.IGNORE_CASE)
        assertEquals(LogLevel.Success, classifyLogLevel("--auto-pick selected mangadex", searchRe))
        // The download set must not leak into a search classification.
        assertEquals(LogLevel.Info, classifyLogLevel("Done.", searchRe))
    }

    // ── the whole pipeline ─────────────────────────────────────────────────

    @Test
    fun `sanitize strips then drops then classifies`() {
        // Coloured noise: the strip has to happen first or the anchored
        // `^\s*errno:` pattern can never match, and the line leaks through.
        assertNull(sanitizeLogLine("${esc}[90m  errno: -4047${esc}[39m"))
        assertNull(sanitizeLogLine(""))
        assertNull(sanitizeLogLine("   "))

        val ok = sanitizeLogLine("${esc}[31m[!] chapter 12 failed${esc}[0m")
        assertNotNull(ok)
        assertEquals("[!] chapter 12 failed", ok!!.first)
        assertEquals(LogLevel.Error, ok.second)
    }
}
