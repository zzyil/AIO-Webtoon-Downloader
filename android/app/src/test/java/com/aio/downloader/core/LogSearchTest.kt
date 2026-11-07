package com.aio.downloader.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Offline coverage for the Logs screen's text search.
 *
 * WHY IT IS PINNED: the search is substring matching ON PURPOSE, because
 * aio-dl.py's output is full of characters a regex would eat — `[!]` heads
 * every error line, chapter labels carry `.`, group names carry `(`/`)`/`+`.
 * A user typing `[!]` to find the errors has to get the errors, not a character
 * class matching a bare `!` and not a syntax error there is nowhere to report.
 *
 * The highlight ranges are pinned for a subtler reason: `AnnotatedString`
 * renders overlapping spans on top of each other at full opacity, so a query
 * whose terms overlap would visibly differ from one whose terms do not, for no
 * reason a reader could explain. [logMatchRanges] merges instead.
 *
 * Plain JVM tests — core/LogFilter.kt imports nothing from Android.
 * Cross-file: ui/screens/LogsScreen.kt (the only caller).
 */
class LogSearchTest {

    // ── terms ──────────────────────────────────────────────────────────────

    @Test
    fun `terms split on any run of whitespace`() {
        assertEquals(listOf("chapter", "12"), logQueryTerms("  chapter   12 "))
        assertEquals(listOf("a", "b"), logQueryTerms("a\tb"))
        assertTrue(logQueryTerms("   ").isEmpty())
        assertTrue(logQueryTerms("").isEmpty())
    }

    // ── matching ───────────────────────────────────────────────────────────

    @Test
    fun `an empty query matches everything`() {
        assertTrue(matchesLogQuery("anything at all", logQueryTerms("")))
    }

    @Test
    fun `every term has to appear on the same line`() {
        val line = "[i] Chapter 12 complete 40/40 pages"
        assertTrue(matchesLogQuery(line, logQueryTerms("chapter 12")))
        assertTrue(matchesLogQuery(line, logQueryTerms("12 chapter")))
        // AND, not OR: a log is thousands of near-identical lines, so the useful
        // query is a narrowing one.
        assertFalse(matchesLogQuery(line, logQueryTerms("chapter 13")))
    }

    @Test
    fun `matching ignores case`() {
        assertTrue(matchesLogQuery("Cloudflare challenge", logQueryTerms("CLOUDFLARE")))
        assertTrue(matchesLogQuery("CLOUDFLARE", logQueryTerms("cloudflare")))
    }

    @Test
    fun `regex metacharacters are ordinary text`() {
        val error = "[!] Chapter 12.1 failed (host poisoned)"
        assertTrue(matchesLogQuery(error, logQueryTerms("[!]")))
        assertTrue(matchesLogQuery(error, logQueryTerms("12.1")))
        assertTrue(matchesLogQuery(error, logQueryTerms("(host")))
        // `.` is a literal dot here, so it must NOT match an arbitrary char.
        assertFalse(matchesLogQuery("Chapter 1201 failed", logQueryTerms("12.1")))
    }

    // ── highlight ranges ───────────────────────────────────────────────────

    @Test
    fun `ranges cover every occurrence of every term`() {
        assertEquals(
            listOf(0 until 3, 8 until 11),
            logMatchRanges("abc xyz abc", logQueryTerms("abc")),
        )
    }

    @Test
    fun `ranges are case-insensitive but keep the source offsets`() {
        assertEquals(listOf(4 until 11), logMatchRanges("the CHAPTER", logQueryTerms("chapter")))
    }

    @Test
    fun `overlapping and touching hits are merged`() {
        // "chapter" and "chap" overlap; without merging the shared prefix would
        // be styled twice and read darker than an unshared match.
        assertEquals(
            listOf(0 until 7),
            logMatchRanges("chapter", logQueryTerms("chapter chap")),
        )
        // Adjacent, not overlapping — still one span, because two touching
        // highlights with a seam between them look like a rendering bug.
        assertEquals(listOf(0 until 2), logMatchRanges("ab", logQueryTerms("a b")))
    }

    @Test
    fun `no query and no match both yield no ranges`() {
        assertTrue(logMatchRanges("some line", logQueryTerms("")).isEmpty())
        assertTrue(logMatchRanges("some line", logQueryTerms("absent")).isEmpty())
        assertTrue(logMatchRanges("", logQueryTerms("a")).isEmpty())
        // A term longer than the line must not run off the end of the string.
        assertTrue(logMatchRanges("ab", logQueryTerms("abcdef")).isEmpty())
    }

    @Test
    fun `ranges are ascending and non-overlapping`() {
        val line = "[i] Chapter 12 complete 40/40 pages, chapter 12 saved"
        val ranges = logMatchRanges(line, logQueryTerms("chapter 12 pages"))
        assertTrue(ranges.isNotEmpty())
        ranges.zipWithNext().forEach { (a, b) -> assertTrue(a.last < b.first) }
        // Every range has to be addressable as an AnnotatedString span.
        ranges.forEach {
            assertTrue(it.first >= 0)
            assertTrue(it.last + 1 <= line.length)
        }
    }
}
