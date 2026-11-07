package com.aio.downloader.browser

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins BrowserJs.kt — the string-building and pattern-matching half of the
 * WebView backend.
 *
 * These run on a plain JVM (`gradlew :app:testDebugUnitTest`), which is the
 * entire reason that file has no Android imports. The alternative is finding out
 * on the device that a quoting bug turned MangaFire's signer bootstrap into a
 * syntax error, three minutes and one install into a test run.
 *
 * Exotic characters are built with `Char(code)` rather than written as escapes,
 * because a literal control byte in a Kotlin source file is invisible in a diff
 * and has already cost this project one debugging session (see core/LogFilter.kt
 * on why its ESC is spelled out).
 */
class BrowserJsTest {

    // ── buildEvalScript ───────────────────────────────────────────────────

    @Test
    fun `wraps the script in parentheses so an arrow function is an expression`() {
        val out = buildEvalScript("id-1", "() => document.title", "")
        // Bare `() => x` is a syntax error where a statement is expected; the
        // parens are what make it evaluable.
        assertTrue(out.contains("var __fn = (() => document.title);"))
    }

    @Test
    fun `awaits a returned promise`() {
        // THE reason this wrapper exists. evaluateJavascript hands back the
        // synchronous result, so without Promise.resolve an async signer
        // returns a stringified Promise and every token comes back null.
        val out = buildEvalScript("id-1", "async () => 1", "")
        assertTrue(out.contains("Promise.resolve(__out).then("))
    }

    @Test
    fun `settles back through the injected bridge`() {
        val out = buildEvalScript("abc-123", "() => 1", "")
        assertTrue(out.contains("AioBridge.settle(__id, ok, payload);"))
        assertTrue(out.contains("var __id = \"abc-123\";"))
    }

    @Test
    fun `empty argJson invokes the function with no argument`() {
        val out = buildEvalScript("id", "() => 1", "")
        assertTrue(out.contains("__fn()"))
        assertFalse(out.contains("JSON.parse"))
    }

    @Test
    fun `a supplied argJson is parsed back into a value in the page`() {
        val out = buildEvalScript("id", "(a) => a", """{"k":[1,2]}""")
        // Quoted, then JSON.parsed — never interpolated raw, which would let a
        // payload containing a quote break out of the expression.
        assertTrue(out.contains("""__fn(JSON.parse("{\"k\":[1,2]}"))"""))
    }

    @Test
    fun `an explicit null argument is distinct from no argument`() {
        // sites/browser_backend.py's NOARG contract: "" means absent, "null"
        // means the value null. Collapsing them invokes fn(null) on a zero-arg
        // script.
        val absent = buildEvalScript("id", "(a) => a", "")
        val explicitNull = buildEvalScript("id", "(a) => a", "null")
        assertTrue(absent.contains("__fn()"))
        assertTrue(explicitNull.contains("""__fn(JSON.parse("null"))"""))
    }

    @Test
    fun `guards against an unserializable result`() {
        // A circular object would otherwise throw inside JSON.stringify, settle
        // nothing, and hang the caller until its timeout.
        val out = buildEvalScript("id", "() => 1", "")
        assertTrue(out.contains("result is not JSON-serializable"))
        // JSON.stringify(undefined) is undefined, not a string.
        assertTrue(out.contains("payload === undefined"))
    }

    // ── jsQuote ───────────────────────────────────────────────────────────

    @Test
    fun `quotes and escapes the obvious characters`() {
        assertEquals("\"\"", jsQuote(""))
        assertEquals("\"plain\"", jsQuote("plain"))
        assertEquals("\"a\\\"b\"", jsQuote("a\"b"))
        assertEquals("\"a\\\\b\"", jsQuote("a\\b"))
        assertEquals("\"a\\nb\"", jsQuote("a\nb"))
        assertEquals("\"a\\tb\"", jsQuote("a\tb"))
    }

    @Test
    fun `escapes the JS line terminators that are legal inside JSON`() {
        // U+2028/U+2029 pass a JSON validator but terminate a line in JS
        // SOURCE, so an unescaped one is a syntax error at injection time.
        assertEquals("\"a\\u2028b\"", jsQuote("a" + Char(0x2028) + "b"))
        assertEquals("\"a\\u2029b\"", jsQuote("a" + Char(0x2029) + "b"))
    }

    @Test
    fun `escapes less-than so no payload can spell a closing script tag`() {
        assertEquals("\"\\u003C/script>\"", jsQuote("</script>"))
    }

    @Test
    fun `escapes arbitrary control characters`() {
        assertEquals("\"\\u0000\"", jsQuote(Char(0).toString()))
        assertEquals("\"\\u001b\"", jsQuote(Char(0x1B).toString()))
        assertEquals("\"\\f\"", jsQuote(Char(0x0C).toString()))
        assertEquals("\"\\b\"", jsQuote(Char(0x08).toString()))
    }

    @Test
    fun `never emits a raw newline`() {
        // The output is spliced into a single-line var assignment; a raw
        // newline there is an unterminated string literal.
        val quoted = jsQuote("line1\nline2\r\nline3" + Char(0x2028))
        assertFalse(quoted.contains('\n'))
        assertFalse(quoted.contains('\r'))
    }

    // ── looksLikeChallenge ────────────────────────────────────────────────

    @Test
    fun `a challenge DOM node is trusted on its own`() {
        // The widget can render before its surrounding copy does, so the marker
        // must win without any phrase present.
        assertTrue(looksLikeChallenge(title = "", text = "", marker = true))
    }

    @Test
    fun `a short page carrying a challenge phrase is a challenge`() {
        assertTrue(
            looksLikeChallenge(
                title = "Just a moment...",
                text = "Verifying you are human. This may take a few seconds.",
                marker = false,
            ),
        )
    }

    @Test
    fun `matching is case insensitive`() {
        assertTrue(looksLikeChallenge("JUST A MOMENT...", "", false))
    }

    @Test
    fun `a long page is never a challenge on phrase alone`() {
        // THE false positive this guard exists for: a series synopsis that
        // happens to contain the words. WebView gives no status code at
        // onPageFinished, so the desktop's "403 plus a phrase" branch is
        // unavailable and length is the only discriminator left.
        val synopsis = "Just a moment, she thought. ".repeat(200)
        assertTrue(synopsis.length > 2_000)
        assertFalse(looksLikeChallenge("Chapter 12 - Reader", synopsis, false))
    }

    @Test
    fun `an ordinary short page is not a challenge`() {
        assertFalse(looksLikeChallenge("MangaFire - Frieren", "Chapter list", false))
    }

    @Test
    fun `a challenge marker still wins on a long page`() {
        assertTrue(looksLikeChallenge("x", "y".repeat(50_000), marker = true))
    }

    // ── hostOf ────────────────────────────────────────────────────────────

    @Test
    fun `extracts the host from realistic urls`() {
        assertEquals("mangafire.to", hostOf("https://mangafire.to/title/abc-def"))
        assertEquals("mangafire.to", hostOf("https://mangafire.to"))
        assertEquals("sub.example.org", hostOf("http://sub.example.org:8443/a?b=c"))
        assertEquals("example.org", hostOf("https://user:pw@example.org/x"))
        // Query before any slash must not be read as part of the authority.
        assertEquals("example.org", hostOf("https://example.org?q=1"))
    }

    @Test
    fun `accepts a bare authority with no scheme`() {
        assertEquals("mangafire.to", hostOf("mangafire.to/title/abc"))
    }

    @Test
    fun `degrades harmlessly on non-http urls`() {
        // "about" rather than "about:blank" — the port-stripping rule cannot
        // tell a port from a scheme-specific part. A don't-care case: the two
        // callers key a "already asked" set and title a notification, and
        // neither ever sees a non-http URL. Pinned so the behaviour is a
        // decision rather than a surprise.
        assertEquals("about", hostOf("about:blank"))
        assertEquals("", hostOf(""))
    }
}
