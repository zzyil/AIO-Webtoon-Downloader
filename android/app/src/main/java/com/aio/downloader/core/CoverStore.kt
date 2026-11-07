package com.aio.downloader.core

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.pdf.PdfRenderer
import android.os.ParcelFileDescriptor
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

/**
 * Turns a series folder into an on-disk image path an image loader can open.
 *
 * ── WHY THIS EXISTS AT ALL ─────────────────────────────────────────────────
 * A series stores its cover in one of five places, and only one of them is a
 * plain file:
 *   1. `cover.jpg` at the series root — what `--komikku` always writes.
 *   2. The first page of a raw `Chapter_N/` directory (`--format none`).
 *   3. Inside the CBZ/EPUB, as a zip member with no standalone path.
 *   4. Nowhere on disk, for a PDF-only series — page 1 of the PDF is the cover.
 *   5. Nowhere on disk at all: only the `cover` URL in `.aio_series.json`.
 * `library_state.scan_library` reports 1 and 2 (it is deliberately read-only),
 * `aio_android.library_cover` extracts 3 into the app cache, and 4 and 5 are
 * this file's job — unzipping is Python's strength, rasterizing a PDF and
 * speaking HTTP are Android's.
 *
 * ── EVERYTHING LEAVES HERE AS A LOCAL FILE ─────────────────────────────────
 * including the remote case, which is why step 5 downloads instead of handing
 * the URL on. Coil 3 moved network support into a separate `coil-network-*`
 * artifact that this app deliberately does not depend on (see the comment on
 * `coil` in gradle/libs.versions.toml), so a URL passed to AsyncImage has no
 * fetcher at all. Downloading it here keeps that dependency decision intact,
 * keeps the memo/cache story in one place, and is the only way to attach the
 * hotlink Referer some cover CDNs require — see [refererFor].
 *
 * ── THE MEMO IS KEYED ON MTIME, NOT JUST THE PATH ──────────────────────────
 * so it invalidates itself. Re-downloading a series rewrites its archive, and a
 * memo keyed on the folder alone would keep serving the previous cover for the
 * life of the process with nothing to clear it. Keying on
 * [LibrarySeries.modifiedAt] means the new scan produces a new key for free.
 *
 * Cache files land in `<cacheDir>/covers`, the SAME directory
 * `aio_android._cover_cache_dir` writes to — one place to reason about, and
 * Android reclaims it wholesale under storage pressure.
 */
object CoverStore {

    private const val TAG = "AioCovers"

    /**
     * Long edge of a rendered PDF cover, in pixels. Wide enough for the detail
     * screen (~128dp at 3x) with headroom, small enough that a 400-series
     * library does not fill the cache with megapixel JPEGs.
     */
    private const val PDF_COVER_WIDTH = 640

    /**
     * Refuse a "cover" bigger than this. A cover is ~100-400 KB; anything at
     * this size is a mis-recorded URL pointing at something else, and the cost
     * of finding out is a phone's data plan.
     */
    private const val REMOTE_COVER_MAX_BYTES = 8L * 1024 * 1024

    private const val CONNECT_TIMEOUT_MS = 10_000
    private const val READ_TIMEOUT_MS = 15_000

    /**
     * Ask for an image, explicitly.
     *
     * Mirrors `sites/_image_io.py:IMAGE_ACCEPT` — the same constant, for the
     * same reason: a default Accept that prefers `text/html` makes hosts that
     * content-negotiate answer an image URL with a wrapper PAGE, and the
     * symptom then reads as a dead host rather than a bad request. grep
     * IMAGE_ACCEPT.
     */
    private const val IMAGE_ACCEPT =
        "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

    /**
     * Cover CDNs that hotlink-protect, and the Referer that satisfies them.
     *
     * A one-for-one mirror of the desktop's request filter
     * (`UI-source/electron/main.js`, grep `pstatic`): LINE Webtoon serves its
     * covers from `*.pstatic.net` and `*.webtoons.com`, and both answer a
     * refererless request with an error page instead of the image. Mirrored
     * rather than re-derived so the two apps fail — or don't — on the same set.
     */
    private val REFERER_HOSTS = mapOf(
        "pstatic.net" to "https://www.webtoons.com/",
        "webtoons.com" to "https://www.webtoons.com/",
    )

    /** memo key -> resolved path, or "" for "looked, there is nothing". */
    private val resolved = ConcurrentHashMap<String, String>()

    fun cacheDir(context: Context): File =
        File(context.applicationContext.cacheDir, "covers").apply { mkdirs() }

    /**
     * The cover for [series], or null if it has none.
     *
     * Blocking work happens on IO. Negative results are memoized too — a series
     * with no cover would otherwise re-open its archive on every scroll pass.
     * That includes a FAILED remote fetch: one attempt per series per process,
     * because the alternative is a card that re-hits a dead CDN every time it
     * scrolls back into view. The user-reachable retry is Settings → Clear
     * cover cache, which drops the memo along with the files.
     */
    suspend fun resolve(context: Context, series: LibrarySeries): String? {
        val key = "${series.folder}|${series.modifiedAt}"
        resolved[key]?.let { return it.ifEmpty { null } }

        // The scan already found a plain file for most series; take it without
        // a JNI hop. Re-checked for existence because the scan may be minutes
        // old and the file may have been deleted underneath it.
        val scanned = series.cover
        if (scanned.isNotEmpty() && File(scanned).isFile) {
            resolved[key] = scanned
            return scanned
        }

        // Order is cheapest-and-most-truthful first. The network step is LAST
        // on both counts: it costs a request, and what is inside the archive is
        // what the reader will actually show.
        val found = withContext(Dispatchers.IO) {
            fromPython(context, series.folder)
                ?: fromPdf(context, series.primaryBook)
                ?: fromRemote(context, series.remoteCover)
        }
        resolved[key] = found.orEmpty()
        return found
    }

    /** Drop everything. Only needed if the cache directory is wiped underneath us. */
    fun invalidate() = resolved.clear()

    /**
     * Delete every cached cover and forget the memo. Returns how many files went.
     *
     * Reached from Settings. Both halves have to go together — the Python
     * extractor writes into this same directory and returns an existing file
     * without re-extracting, so clearing the memo alone would change nothing,
     * and clearing the files alone would leave the memo pointing at paths that
     * no longer exist.
     */
    suspend fun clear(context: Context): Int = withContext(Dispatchers.IO) {
        val dir = cacheDir(context)
        val removed = dir.listFiles()?.count { it.isFile && it.delete() } ?: 0
        invalidate()
        removed
    }

    // ── resolution steps ──────────────────────────────────────────────────

    private fun fromPython(context: Context, folder: String): String? = runCatching {
        Aio.module(context.applicationContext)
            .callAttr("library_cover", folder)
            .toString()
            .ifBlank { null }
    }.getOrElse {
        Log.w(TAG, "library_cover failed for $folder", it)
        null
    }

    /**
     * Render page 1 of a PDF to a JPEG in the cover cache.
     *
     * Deliberately NOT done in Python: there is no PDF rasterizer in the
     * dependency set (pypdf reads structure, it does not draw), and Android has
     * had a perfectly good one since API 21.
     */
    private fun fromPdf(context: Context, bookPath: String): String? {
        if (!bookPath.endsWith(".pdf", ignoreCase = true)) return null
        val source = File(bookPath)
        if (!source.isFile) return null

        val out = File(cacheDir(context), "pdf-${digestOf(source)}.jpg")
        if (out.isFile && out.length() > 0) return out.absolutePath

        return runCatching {
            ParcelFileDescriptor.open(source, ParcelFileDescriptor.MODE_READ_ONLY).use { fd ->
                PdfRenderer(fd).use { renderer ->
                    if (renderer.pageCount == 0) return null
                    renderer.openPage(0).use { page ->
                        val width = PDF_COVER_WIDTH
                        val height = (width.toLong() * page.height / page.width)
                            .coerceIn(1L, 4096L).toInt()
                        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
                        // PdfRenderer composites onto whatever is already in the
                        // bitmap and does NOT paint a page background, so an
                        // un-erased bitmap leaves every un-inked area fully
                        // transparent — which renders as a black page in dark
                        // mode and as nothing at all over a themed surface.
                        bitmap.eraseColor(Color.WHITE)
                        page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                        writeAtomically(out) { stream ->
                            bitmap.compress(Bitmap.CompressFormat.JPEG, 88, stream)
                        }
                        bitmap.recycle()
                    }
                }
            }
            out.absolutePath.takeIf { out.isFile && out.length() > 0 }
        }.getOrElse {
            Log.w(TAG, "pdf cover render failed for $bookPath", it)
            null
        }
    }

    /**
     * Download the recorded cover URL into the cover cache and return its path.
     *
     * This is the only network call in the library layer, and it is deliberately
     * plain `HttpURLConnection`: one request, no dependency, and full control of
     * the request headers — which is the entire point, since the CDNs that need
     * this most are the ones that reject a request without a [refererFor] match.
     *
     * Three guards, each for a failure that would otherwise look like a corrupt
     * cover rather than a bad response:
     *  - status must be 200. `HttpURLConnection` follows same-protocol
     *    redirects itself but NOT https↔http ones, so a cross-protocol hop
     *    surfaces here as a 3xx and is treated as a miss.
     *  - `Content-Type` must start with `image/` when it is present at all.
     *    This is what catches a hotlink block that answers 200 with an HTML
     *    page. (Written as a prefix rather than as the obvious wildcard form
     *    ON PURPOSE: Kotlin block comments NEST, unlike Java's, so a literal
     *    slash-star inside this KDoc opens a second comment and the closing
     *    marker below then only closes THAT one — leaving the whole rest of
     *    the file commented out. It fails as "Unclosed comment" at EOF, which
     *    points nowhere near the real line. Same applies to any MIME wildcard
     *    written in a comment anywhere in this module.)
     *  - the body is capped at [REMOTE_COVER_MAX_BYTES].
     *
     * The cache name is the digest of the URL with no extension: BitmapFactory
     * (and therefore Coil) sniffs the bytes, so the extension buys nothing, and
     * a URL-keyed name invalidates itself when a re-enrichment repoints the
     * series at a different cover.
     */
    private fun fromRemote(context: Context, url: String): String? {
        if (url.isBlank()) return null

        val out = File(cacheDir(context), "web-${digestOf(url)}")
        if (out.isFile && out.length() > 0) return out.absolutePath

        // try/finally rather than runCatching: the connection has to be
        // disconnected on every exit, and an early `return` out of a
        // runCatching lambda is a non-local return that would skip a trailing
        // .also{}.
        var connection: HttpURLConnection? = null
        return try {
            val parsed = URL(url)
            val http = (parsed.openConnection() as HttpURLConnection).also { connection = it }
            http.connectTimeout = CONNECT_TIMEOUT_MS
            http.readTimeout = READ_TIMEOUT_MS
            http.setRequestProperty("Accept", IMAGE_ACCEPT)
            refererFor(parsed.host.orEmpty())?.let { http.setRequestProperty("Referer", it) }

            // Both of these force the request; read them once, in this order,
            // so a failure is reported as its status rather than as whatever
            // content type the error page happened to carry.
            val status = http.responseCode
            val type = http.contentType.orEmpty().substringBefore(';').trim()
            when {
                status != HttpURLConnection.HTTP_OK -> {
                    Log.w(TAG, "cover fetch: HTTP $status for $url")
                    null
                }

                type.isNotEmpty() && !type.startsWith("image/", ignoreCase = true) -> {
                    Log.w(TAG, "cover fetch: $type is not an image, for $url")
                    null
                }

                else -> {
                    writeAtomically(out) { sink -> http.inputStream.copyCapped(sink) }
                    out.absolutePath.takeIf { out.isFile && out.length() > 0 }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "cover fetch failed for $url", e)
            out.delete()
            null
        } finally {
            connection?.disconnect()
        }
    }

    // ── helpers ───────────────────────────────────────────────────────────

    /**
     * The Referer this host demands, or null if it does not care. Matches the
     * host itself and any subdomain of it, which is what the desktop's
     * match pattern for `pstatic.net` means (grep `pstatic` in
     * UI-source/electron/main.js for the literal form).
     *
     * The pattern is described rather than quoted because it contains a
     * slash-star, and Kotlin block comments NEST — see the note on the
     * Content-Type guard above.
     *
     * INTERNAL, not private, because [AioImageLoader] needs the same answer for
     * Coil's OkHttp interceptor. ONE table with two readers, deliberately: a
     * second copy of this map is how the desktop and Android halves of a rule
     * drift apart, which is a mistake this codebase has already made twice
     * (grep the manhwaread/madara rescue duplication note in
     * sites/crawlee_utils.py).
     */
    internal fun refererFor(host: String): String? {
        val lower = host.lowercase()
        return REFERER_HOSTS.entries
            .firstOrNull { (suffix, _) -> lower == suffix || lower.endsWith(".$suffix") }
            ?.value
    }

    /** Copy, aborting past [REMOTE_COVER_MAX_BYTES] so a wrong URL cannot run away. */
    private fun InputStream.copyCapped(sink: OutputStream) {
        var total = 0L
        val buffer = ByteArray(16 * 1024)
        buffered().use { source ->
            while (true) {
                val read = source.read(buffer)
                if (read < 0) break
                total += read
                if (total > REMOTE_COVER_MAX_BYTES) {
                    throw IOException("cover exceeded $REMOTE_COVER_MAX_BYTES bytes")
                }
                sink.write(buffer, 0, read)
            }
        }
    }

    private fun digestOf(value: String): String =
        MessageDigest.getInstance("SHA-1")
            .digest(value.toByteArray())
            .joinToString("") { "%02x".format(it) }
            .take(24)

    /**
     * Identity of a source file for cache naming: path + mtime + size. Same
     * three inputs `aio_android.library_cover` uses, so both halves of the cover
     * cache invalidate on the same event.
     */
    private fun digestOf(file: File): String =
        digestOf("${file.absolutePath}|${file.lastModified()}|${file.length()}")

    /**
     * Write via a sibling and rename. A half-written JPEG that a scrolling card
     * decodes as a corrupt image is worse than no cover, and rename is atomic.
     */
    private inline fun writeAtomically(target: File, write: (java.io.OutputStream) -> Unit) {
        val tmp = File(target.parentFile, "${target.name}.part")
        try {
            tmp.outputStream().buffered().use(write)
            if (!tmp.renameTo(target)) {
                tmp.copyTo(target, overwrite = true)
                tmp.delete()
            }
        } catch (t: Throwable) {
            tmp.delete()
            throw t
        }
    }
}
