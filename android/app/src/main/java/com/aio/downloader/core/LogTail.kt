package com.aio.downloader.core

import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.BufferedReader
import java.io.InputStreamReader
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * aio-dl.py's console output, made visible inside the app.
 *
 * ── WHY LOGCAT AND NOT A PYTHON HOOK ───────────────────────────────────────
 * Chaquopy already redirects the interpreter's `sys.stdout` / `sys.stderr` into
 * logcat under the tags `python.stdout` / `python.stderr`, and aio-dl.py prints
 * a great deal that never becomes a structured event — group selection, per-page
 * retries, backoff decisions, the skipped-chapters report. The structured event
 * sink carries milestones; this carries the reasoning. Reading logcat back gets
 * all of it with ZERO changes to the Python, which matters because the Python is
 * shared with the desktop.
 *
 * An app can read its OWN log entries without any permission — the log daemon
 * filters by UID — so this needs nothing in the manifest. It is also why the
 * output can never contain another app's data.
 *
 * `-T 1` means "print the most recent line, then follow", so a fresh reader
 * starts at roughly now instead of replaying whatever is still in the ring
 * buffer from previous runs. (`-t` is the dump-and-exit sibling; using it here
 * would give one snapshot and no stream.)
 *
 * ── LIFETIME: THE WHOLE PROCESS, NOT THE SCREEN ────────────────────────────
 * [start] is called from `MainActivity.onCreate` and `DownloadService.onCreate`,
 * and nothing calls [stop] except [restart]. It used to run only while the Logs
 * screen was composed, which combined with `-T 1` meant the log of the download
 * you wanted to understand was the one guaranteed not to be there: you open the
 * tab AFTER something goes wrong, and following-from-now had nothing to show.
 * The cost of the new shape is one blocked daemon thread and one `logcat`
 * subprocess for the process lifetime; the buffer is capped either way.
 *
 * Lines go through the same [sanitizeLogLine] hygiene the desktop applies, so
 * classification is identical on both platforms.
 */
object LogTail {

    private const val TAG = "AioLogTail"

    /**
     * Bounded because a long download prints tens of thousands of lines and the
     * whole list is snapshotted into Compose state on every flush. This is the
     * transcript of the last few minutes, not an archive; logcat itself still
     * has the rest for `adb logcat`.
     */
    private const val MAX_LINES = 1500

    /** Default flush cadence, and the range [flushIntervalMs] will accept. */
    const val DEFAULT_FLUSH_MS = 220L
    private const val MIN_FLUSH_MS = 40L
    private const val MAX_FLUSH_MS = 5_000L

    /**
     * How often buffered lines are published to the UI.
     *
     * aio-dl.py can emit hundreds of lines a second while pages are
     * downloading, and recomposing a lazy list per line would peg a core for
     * output nobody can read at that rate. The desktop exposes the same knob as
     * `logUpdateInterval` (see `UI-source/src/hooks/useDownloader.js`, which
     * reads `settings.logUpdateInterval`).
     *
     * A plain runtime property rather than a read of AppSettings: this object
     * is reached from a bare daemon thread with no Context, and the settings
     * layer is owned elsewhere. Whoever wires the setting assigns this once —
     * clamped here, so a corrupt stored value can neither peg a core nor freeze
     * the view for five seconds at a time.
     */
    @Volatile
    var flushIntervalMs: Long = DEFAULT_FLUSH_MS
        set(value) {
            field = value.coerceIn(MIN_FLUSH_MS, MAX_FLUSH_MS)
        }

    private val _lines = MutableStateFlow<List<LogLine>>(emptyList())
    val lines: StateFlow<List<LogLine>> = _lines.asStateFlow()

    /** Non-null while the reader is down; surfaces "logs unavailable" in the UI. */
    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private var reader: Thread? = null
    private var process: Process? = null

    /**
     * Monotonic across readers. An AtomicLong rather than a plain counter
     * because [restart] can briefly overlap two reader threads, and two lines
     * sharing an id would be two LazyColumn items sharing a key — which Compose
     * treats as a hard error, not a cosmetic one.
     */
    private val nextId = AtomicLong(0)

    /**
     * Bumped by [stop]. A reader whose generation is stale exits at its next
     * line instead of writing into a buffer the new reader now owns; each
     * reader keeps its buffer LOCAL, so there is no shared mutable state to
     * race over in the first place.
     */
    @Volatile private var generation = 0

    /** Set by [clear]; the live reader consumes it and drops its own buffer. */
    @Volatile private var clearRequested = false

    @Synchronized
    fun start() {
        if (reader?.isAlive == true) return
        val gen = ++generation
        reader = thread(name = "aio-logtail", isDaemon = true) { run(gen) }
    }

    @Synchronized
    fun stop() {
        generation++
        runCatching { process?.destroy() }
        process = null
        reader = null
    }

    /**
     * Bring the reader back after it died. The only way `logcat` stops on its
     * own is the system killing it, which leaves [error] set and the screen
     * showing a dead end — this is the button behind that message.
     */
    fun restart() {
        stop()
        _error.value = null
        start()
    }

    fun clear() {
        clearRequested = true
        _lines.value = emptyList()
    }

    private fun run(gen: Int) {
        val command = listOf(
            "logcat",
            "-v", "raw",
            "-T", "1",
            // Silent default, then the tags worth showing. Chaquopy's two are
            // the download itself; AioService is the run/cancel/exit lifecycle,
            // which is exactly the context that makes the Python output
            // readable ("run_download_json […]" then the output it produced).
            "-s",
            "python.stdout:V", "python.stderr:V", "AioService:V",
        )
        // LOCAL, not a field: see [generation]. Two readers can overlap for a
        // few milliseconds across a restart, and an ArrayDeque mutated from
        // both is corruption rather than a missing line.
        val buffer = ArrayDeque<LogLine>()
        try {
            val proc = ProcessBuilder(command).redirectErrorStream(true).start()
            synchronized(this) {
                if (gen != generation) {
                    proc.destroy()
                    return
                }
                process = proc
            }
            _error.value = null

            var lastFlush = System.currentTimeMillis()
            var dirty = false

            BufferedReader(InputStreamReader(proc.inputStream)).use { input ->
                while (true) {
                    val raw = input.readLine() ?: break
                    if (gen != generation) return
                    if (clearRequested) {
                        clearRequested = false
                        buffer.clear()
                        dirty = false
                    }
                    val sanitized = sanitizeLogLine(raw)
                    if (sanitized != null) {
                        val (text, level) = sanitized
                        buffer.addLast(LogLine(nextId.getAndIncrement(), text, level))
                        while (buffer.size > MAX_LINES) buffer.removeFirst()
                        dirty = true
                    }
                    val now = System.currentTimeMillis()
                    if (dirty && now - lastFlush >= flushIntervalMs) {
                        _lines.value = buffer.toList()
                        lastFlush = now
                        dirty = false
                    }
                }
            }
            if (gen != generation) return
            if (dirty) _lines.value = buffer.toList()
            // The stream ended without an exception, which means logcat itself
            // exited — the system reclaiming it, most often. Said out loud so
            // the screen can offer a retry; silently freezing is the one
            // outcome that reads as "the download stopped printing".
            _error.value = "The log reader exited."
        } catch (t: Throwable) {
            // Never fatal: the Logs screen is a convenience, and every line here
            // is also in `adb logcat`. Surface it in the UI rather than dying.
            Log.w(TAG, "log tail stopped", t)
            if (gen == generation) _error.value = t.message ?: t.javaClass.simpleName
        }
    }
}
