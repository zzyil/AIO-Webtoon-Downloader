package com.aio.downloader.core

/**
 * Line hygiene for aio-dl.py's output, ported from
 * `UI-source/electron/log-filter.js`.
 *
 * Same three steps, same order, same rules:
 *   1. strip ANSI SGR escapes                 ([stripAnsi])
 *   2. drop Playwright-teardown noise         ([NOISY_LINE_RE])
 *   3. classify what survives                 ([classifyLogLevel])
 *
 * WHY PORT RATHER THAN INVENT: these patterns encode bugs that were already
 * paid for. The strict error rules exist because an earlier `/error:|FAILED/i`
 * matched the substring "failed" case-insensitively and painted hundreds of
 * benign retry lines red — "First variant failed, trying 9 more" among them.
 * Anything looser here reproduces that on Android.
 *
 * KEEP IN SYNC with log-filter.js. `LogFilterTest` asserts the behaviours that
 * file's comments call out by name, so a divergence fails a test rather than
 * quietly turning the Logs screen into a wall of red.
 *
 * Pure string helpers — no Android imports, so they run in a plain JVM unit
 * test. Read by [LogTail].
 */

enum class LogLevel { Error, Warning, Success, Verbose, Info }

/**
 * Drop-list for output that can't be usefully surfaced. Playwright's
 * BrowserContext teardown races aio-dl.py's chapter loop and emits a full
 * Node.js crash dump; none of it is actionable, because the chapter either
 * already finished or will be retried by the missed-chapter pass.
 *
 * ANDROID NOTE: most of these can only fire on the desktop, where Playwright
 * runs as a Node subprocess — there is no Node here at all. They are kept
 * anyway so the two implementations stay diffable, and because the
 * `BrokenPipeError` / `ConnectionResetError` half is pure Python and does
 * reach this device.
 *
 * `^` / `$` anchor to the whole input, as in the JS original (no `m` flag
 * there, no MULTILINE here). Callers must pass ONE line — [LogTail] reads
 * logcat line by line, so that holds.
 */
val NOISY_LINE_RE: Regex = Regex(
    listOf(
        // EPIPE-family literals (the single-line "Error: write EPIPE" path).
        """\b(EPIPE|BrokenPipeError|ConnectionResetError)\b""",
        // Node.js internal stack frame paths.
        """node:events:\d""",
        """node:internal/(net|streams|process|timers|destroy)""",
        // Playwright driver internals.
        """playwright[\\/]driver""",
        """\bPipeTransport\b""",
        """\bDispatcherConnection\b""",
        """\bBrowserContextDispatcher\b""",
        """\bCRBrowserContext\b""",
        // Node crash format markers.
        """^\s*throw\s+er\b""",
        """(Unhandled|Emitted) '\w+' event""",
        """^\s*errno:\s*-?\d+""",
        """^\s*syscall:\s*['"]""",
        """^Node\.js v\d""",
        // Lone caret pointer + lone brace-dump open/close lines.
        """^\s*\^\s*$""",
        """^\s*[{}],?\s*$""",
    ).joinToString("|"),
)

/**
 * ESC written as an escape sequence, never as a literal 0x1B byte in the
 * source. A raw ESC survives most editors and no diff tool, and the day
 * something eats it this regex silently stops matching — a failure invisible in
 * review that surfaces as ANSI garbage in the Logs screen. (One did get eaten
 * while this file was first written, which is why the constant exists.)
 */
private const val ESC = "\u001B"

private val ANSI_RE = Regex("$ESC\\[[0-9;]*m")

/**
 * Strip ANSI SGR escapes. Fast-paths the common case — most lines carry no
 * escape byte — by skipping the regex scan and the string allocation entirely.
 *
 * Stripping must happen BEFORE [NOISY_LINE_RE]: an anchored pattern such as
 * `^\s*errno:` cannot match a line that actually begins with a color escape.
 */
fun stripAnsi(rawLine: String): String =
    if (rawLine.contains(ESC)) ANSI_RE.replace(rawLine, "") else rawLine

/**
 * "This line is a success" for the DOWNLOAD stream, green in the log.
 * Mirrors `DOWNLOAD_SUCCESS_RE` in downloader.js. Search has its own set on the
 * desktop (`/--auto-pick selected|alignment/i`); when M6 lands a search screen
 * it should pass that rather than reusing this one.
 */
val DOWNLOAD_SUCCESS_RE: Regex =
    Regex("""Done\.|saved →|✓|Completed|recovered""", RegexOption.IGNORE_CASE)

private val ERR_BANG_RE = Regex("""^\[!]""")
private val ERR_TRACEBACK_RE = Regex("""^Traceback """)
private val ERR_PY_EXC_RE = Regex("""^\w*?Error:""")
private val ERR_FAILED_RE = Regex("""\bFAILED\b""")

// The `i` flag makes the two spellings redundant; kept as written on the
// desktop so a future edit there diffs cleanly against this.
private val WARN_RE = Regex("""Warning:|warning:|⚠""", RegexOption.IGNORE_CASE)

private val INDENTED_RE = Regex("""^\s{2,}""")

/**
 * Severity for one already-stripped line.
 *
 * The error patterns are deliberately STRICT, each for a reason:
 *  - `[!]` is aio-dl.py's genuine-error convention, anchored to the trimmed
 *    start so `[!?] not really` doesn't fire.
 *  - `Traceback ` heads a Python crash.
 *  - `^\w*?Error:` catches `ImportError: …` / `ValueError: …` / bare `Error: …`.
 *  - `\bFAILED\b` is the UPPERCASE word only. Lowercase "failed" appears in
 *    ordinary retry chatter and must not paint red.
 *
 * [successRe] is the only caller-specific part — the download stream and the
 * search stream celebrate different lines.
 */
fun classifyLogLevel(line: String, successRe: Regex = DOWNLOAD_SUCCESS_RE): LogLevel {
    val trimmed = line.trim()
    if (ERR_BANG_RE.containsMatchIn(trimmed)) return LogLevel.Error
    if (ERR_TRACEBACK_RE.containsMatchIn(trimmed)) return LogLevel.Error
    if (ERR_PY_EXC_RE.containsMatchIn(trimmed)) return LogLevel.Error
    if (ERR_FAILED_RE.containsMatchIn(line)) return LogLevel.Error

    if (WARN_RE.containsMatchIn(line)) return LogLevel.Warning
    if (successRe.containsMatchIn(line)) return LogLevel.Success
    // Indented lines are usually verbose detail hanging off the line above.
    if (INDENTED_RE.containsMatchIn(line)) return LogLevel.Verbose
    return LogLevel.Info
}

/**
 * The whole pipeline: strip, drop, classify. Returns null for a line that
 * should not be shown at all.
 */
fun sanitizeLogLine(
    rawLine: String,
    successRe: Regex = DOWNLOAD_SUCCESS_RE,
): Pair<String, LogLevel>? {
    if (rawLine.isEmpty()) return null
    val line = stripAnsi(rawLine)
    if (line.isBlank()) return null
    if (NOISY_LINE_RE.containsMatchIn(line)) return null
    return line to classifyLogLevel(line, successRe)
}

// ── text search (LogsScreen) ───────────────────────────────────────────────

/**
 * Split what the user typed into search terms.
 *
 * Whitespace-separated, and every term must match — AND, not OR. A download log
 * is thousands of near-identical lines, so the useful query is almost always a
 * narrowing one ("chapter 12 failed"), and OR would return more than the
 * unfiltered list already shows.
 *
 * NOT a regex, deliberately: `[!]`, `(`, `+` and `.` are all ordinary
 * characters in this output, and a user typing `[!]` to find the error lines
 * would otherwise get a character class matching `!`, or a syntax error there
 * is nowhere to report. Substring matching does the obvious thing instead.
 */
fun logQueryTerms(query: String): List<String> =
    query.trim().split(WHITESPACE_RE).filter { it.isNotEmpty() }

private val WHITESPACE_RE = Regex("""\s+""")

/** True when every term in [terms] appears in [text], case-insensitively. */
fun matchesLogQuery(text: String, terms: List<String>): Boolean =
    terms.isEmpty() || terms.all { text.contains(it, ignoreCase = true) }

/**
 * Where [terms] occur in [text], as non-overlapping ranges in ascending order,
 * for the Logs screen's match highlighting.
 *
 * Overlaps are MERGED rather than emitted twice: two terms whose hits touch
 * (searching `chapter chap`) would otherwise produce nested spans, and
 * `AnnotatedString` renders the later one over the earlier at full opacity —
 * so an overlapping pair would visibly differ from a non-overlapping one for no
 * reason the reader could explain.
 */
fun logMatchRanges(text: String, terms: List<String>): List<IntRange> {
    if (text.isEmpty() || terms.isEmpty()) return emptyList()
    val hits = ArrayList<IntRange>()
    for (term in terms) {
        if (term.isEmpty()) continue
        var from = 0
        while (from <= text.length - term.length) {
            val at = text.indexOf(term, from, ignoreCase = true)
            if (at < 0) break
            hits += at until (at + term.length)
            from = at + term.length
        }
    }
    if (hits.size <= 1) return hits
    hits.sortBy { it.first }

    val merged = ArrayList<IntRange>(hits.size)
    var current = hits.first()
    for (index in 1 until hits.size) {
        val next = hits[index]
        current = if (next.first <= current.last + 1) {
            current.first..maxOf(current.last, next.last)
        } else {
            merged += current
            next
        }
    }
    merged += current
    return merged
}
