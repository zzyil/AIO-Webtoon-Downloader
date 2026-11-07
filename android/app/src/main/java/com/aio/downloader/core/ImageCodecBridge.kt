package com.aio.downloader.core

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Build
import android.util.Log
import java.io.ByteArrayOutputStream

/**
 * Android's answer to the WebP codec Chaquopy's Pillow does not ship.
 *
 * WHO CALLS THIS: nothing in Kotlin. `aio_android.set_image_codec_bridge`
 * installs it as the process-wide codec backend, and Python reaches it from
 * `aio_android._BridgeImageCodec`, which adapts this deliberately dumb
 * POSITIONAL, PRIMITIVES-ONLY surface to `sites/image_codec.py`'s Protocol.
 * That module registers opener/saver shims into PIL's own dispatch tables, so
 * aio-dl.py's ~15 `Image.open` / `im.save(format="WebP")` sites never learn
 * this exists. Read `sites/image_codec.py`'s header before changing a signature
 * here — the two are one contract split across a JNI boundary.
 *
 * WHAT IT UNBLOCKS: `--webtoon-recompress` (which silently did nothing), and
 * EPUB/PDF/`--width`/`--scaling`/`--quality<100` on any WebP-serving CDN (which
 * silently dropped pages, or failed the chapter). android/PARITY.md D3 and D4.
 *
 * ── PNG IN, PNG OUT ───────────────────────────────────────────────────────
 * Both directions carry PNG bytes, not raw pixels. Raw RGBA would be faster and
 * is the obvious optimisation, but it demands stride, alpha pre-multiplication
 * and colour-space agreement across JNI, and every bug in that negotiation is a
 * silently wrong pixel — the worst failure shape for an archival downloader.
 * These calls only ever run on aio-dl.py's SLOW paths, where one extra PNG
 * round-trip is noise next to the resize and the WebP encode.
 *
 * ── THREADING: NO MAIN-THREAD HOP ─────────────────────────────────────────
 * Unlike browser/WebViewBridge.kt, nothing here marshals onto the main looper.
 * `BitmapFactory` and `Bitmap.compress` are ordinary thread-safe calls, and
 * that is load-bearing rather than incidental: aio-dl.py encodes pages from a
 * ThreadPoolExecutor sized at half the CPU budget, so a main-thread hop per
 * page would serialize the entire pool behind the UI.
 *
 * ── NEVER THROWS ──────────────────────────────────────────────────────────
 * Every failure returns an EMPTY ByteArray. Python treats that as "this codec
 * cannot service the call" and degrades to exactly the behaviour a codec-less
 * Pillow already had (UnidentifiedImageError on open, OSError on save), which
 * every existing caller in aio-dl.py already handles. An exception crossing JNI
 * would instead surface as an opaque `com.chaquo.python.PyException` from
 * somewhere deep inside a save.
 */
object ImageCodecBridge {

    private const val TAG = "AioCodec"

    /**
     * Formats this device can DECODE, comma-joined for the Python side.
     *
     * A String rather than a List because a `java.util.List` does not arrive as
     * a Python iterable — Chaquopy wraps it in an opaque proxy (the trap
     * `aio_android.py`'s module header calls out). JSON would be overkill for a
     * value with no structure.
     *
     * WebP decode has been in the platform since API 14, so it is unconditional
     * at minSdk 26. AVIF decode arrived in API 31 (Android 12); it is declared
     * because `sites/image_codec.py` will shim any format PIL lacks and the
     * backend claims, and Chaquopy's Pillow 11.0.0 predates Pillow's own AVIF
     * support. There is deliberately no AVIF *encode* — see [encodeWebp].
     */
    @JvmStatic
    fun formats(): String =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) "WEBP,AVIF" else "WEBP"

    /**
     * Decode anything the platform can read into PNG bytes.
     *
     * Empty in / empty out, and empty out on any failure. `BitmapFactory`
     * returns null rather than throwing for undecodable input, which is the
     * common case here (a truncated page, an HTML error body that reached the
     * decoder because a CDN lied about Content-Type).
     */
    @JvmStatic
    fun decodeToPng(data: ByteArray): ByteArray {
        if (data.isEmpty()) return EMPTY
        var bitmap: Bitmap? = null
        return try {
            bitmap = BitmapFactory.decodeByteArray(data, 0, data.size)
                ?: return EMPTY.also { Log.w(TAG, "decode: not a decodable image (${data.size} bytes)") }
            // Quality is ignored for PNG (it is lossless); 100 is the
            // conventional argument.
            compress(bitmap, Bitmap.CompressFormat.PNG, 100)
        } catch (e: Throwable) {
            // Throwable, not Exception: an oversized page is an OutOfMemoryError,
            // which is an Error. Letting that escape would take the download
            // service down; returning empty degrades it to one skipped page.
            Log.w(TAG, "decode failed (${data.size} bytes)", e)
            EMPTY
        } finally {
            // Explicit rather than waiting for the GC: a stitched webtoon page
            // is ~72 MB of ARGB_8888, and the encode pool holds several at once.
            bitmap?.recycle()
        }
    }

    /**
     * Encode PNG bytes as WebP.
     *
     * `quality` is libwebp's 0-100; it is ignored by the lossless encoder, which
     * is why [WebpMode] treats lossless as a format choice rather than a
     * quality of 100. aio-dl.py's `method` / effort argument has no
     * `Bitmap.compress` equivalent and is dropped on the Python side — it
     * trades CPU for bytes, never pixels.
     *
     * NO AVIF COUNTERPART: `Bitmap.CompressFormat` has JPEG, PNG and WebP and
     * nothing else at any API level, so [formats] declaring AVIF buys decode
     * only.
     */
    @JvmStatic
    fun encodeWebp(png: ByteArray, quality: Int, lossless: Boolean): ByteArray {
        if (png.isEmpty()) return EMPTY
        val mode = WebpMode.of(Build.VERSION.SDK_INT, lossless)
        var bitmap: Bitmap? = null
        return try {
            bitmap = BitmapFactory.decodeByteArray(png, 0, png.size)
                ?: return EMPTY.also { Log.w(TAG, "encode: interchange PNG did not decode") }
            compress(
                bitmap,
                compressFormat(mode),
                ImageCodecPolicy.qualityFor(mode, quality, lossless),
            )
        } catch (e: Throwable) {
            Log.w(TAG, "encode failed (${png.size} bytes, q=$quality, lossless=$lossless)", e)
            EMPTY
        } finally {
            bitmap?.recycle()
        }
    }

    /**
     * [WebpMode] -> the platform enum.
     *
     * THE SDK CHECK IS SPELLED OUT HERE, redundantly with [WebpMode.of], and
     * that redundancy is deliberate: `WEBP_LOSSY` / `WEBP_LOSSLESS` are API 30
     * symbols and lint's `NewApi` check cannot follow a guard that reaches it
     * through an enum, so branching on `mode` alone would fail the build on a
     * project with `minSdk = 26`. The two agree by construction —
     * `SPLIT_FORMATS_SDK == Build.VERSION_CODES.R`, asserted in
     * ImageCodecBridgeTest.
     *
     * `@Suppress("DEPRECATION")` covers the else branch: `CompressFormat.WEBP`
     * has been deprecated since API 30 and is the ONLY WebP constant that
     * exists on 26-29, which is a real quarter of this app's supported range.
     * Its behaviour is quality-driven (100 is lossless, below it is lossy),
     * which is why [ImageCodecPolicy.qualityFor] pins a legacy lossless
     * request to 100.
     */
    @Suppress("DEPRECATION")
    private fun compressFormat(mode: WebpMode): Bitmap.CompressFormat =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (mode == WebpMode.LOSSLESS) {
                Bitmap.CompressFormat.WEBP_LOSSLESS
            } else {
                Bitmap.CompressFormat.WEBP_LOSSY
            }
        } else {
            Bitmap.CompressFormat.WEBP
        }

    private fun compress(bitmap: Bitmap, format: Bitmap.CompressFormat, quality: Int): ByteArray {
        // 32 KB rather than the default 32 bytes: a page-sized encode would
        // otherwise copy the whole buffer through a dozen doublings.
        val sink = ByteArrayOutputStream(INITIAL_BUFFER_BYTES)
        if (!bitmap.compress(format, quality, sink)) {
            Log.w(TAG, "compress returned false for $format")
            return EMPTY
        }
        return sink.toByteArray()
    }

    private const val INITIAL_BUFFER_BYTES = 32 * 1024

    private val EMPTY = ByteArray(0)
}

/**
 * Which WebP encoder to ask for. A plain enum, in this file but with no Android
 * types on it, so [WebpMode.of] is unit-testable on a bare JVM — the same
 * reason browser/BrowserJs.kt exists next to browser/WebViewBridge.kt.
 */
enum class WebpMode {
    LOSSY,
    LOSSLESS,

    /**
     * `Bitmap.CompressFormat.WEBP`, deprecated at API 30 and the only choice
     * below it. Quality-driven: it encodes lossless at exactly 100 and lossy
     * otherwise.
     */
    LEGACY,
    ;

    companion object {
        /** API 30 (Android 11) is where WEBP_LOSSY / WEBP_LOSSLESS appear. */
        const val SPLIT_FORMATS_SDK = 30

        @JvmStatic
        fun of(sdkInt: Int, lossless: Boolean): WebpMode = when {
            sdkInt < SPLIT_FORMATS_SDK -> LEGACY
            lossless -> LOSSLESS
            else -> LOSSY
        }
    }
}

/**
 * The pure decisions the bridge makes, with no Android types in any signature.
 *
 * Same split as browser/BrowserJs.kt: the fiddly parts of a native seam are
 * arithmetic and table lookups, and those are worth testing without a device in
 * the loop. Everything that genuinely needs a `Bitmap` stays in
 * [ImageCodecBridge].
 */
object ImageCodecPolicy {

    /**
     * Clamp a caller's quality into libwebp's 0-100.
     *
     * `Bitmap.compress` does not document its behaviour outside that range, and
     * the value arrives from a settings blob that has crossed JSON and JNI.
     *
     * THE ONE NON-OBVIOUS RULE: on API < 30 a lossless encode is expressed as
     * the legacy format at quality EXACTLY 100 — see [qualityFor].
     */
    @JvmStatic
    fun clampQuality(quality: Int): Int = quality.coerceIn(0, 100)

    /**
     * The quality argument that actually realises [mode].
     *
     * [WebpMode.LEGACY] has no lossless flag of its own: the deprecated
     * `CompressFormat.WEBP` encodes losslessly at quality 100 and lossily below
     * it. So a lossless request on API 26-29 must arrive as 100 regardless of
     * what the caller asked for, or `--webtoon-recompress`'s lossless tier
     * would silently become a lossy one on older devices.
     *
     * THE RULE IS BIDIRECTIONAL, and only one direction used to be implemented.
     * Because 100 *means* lossless to that encoder, a LOSSY request at exactly
     * 100 also has to be handled — it was passed through unchanged, so a user
     * choosing `--webtoon-recompress-quality 100` (the CLI accepts 1-100) got a
     * far larger, far slower LOSSLESS file on API 26-29, the exact inverse of
     * what the flag is for. Capping the lossy branch at 99 is the whole fix;
     * 99 vs 100 is imperceptible where 100-as-lossless is not.
     *
     * The 100-means-lossless mapping is AOSP's documented behaviour for the
     * deprecated constant. It is NOT verifiable on the hardware available here
     * (API 35 never takes the LEGACY branch), so this follows the platform
     * documentation and this file's own KDoc rather than a measurement.
     */
    @JvmStatic
    fun qualityFor(mode: WebpMode, requested: Int, lossless: Boolean): Int =
        when {
            mode != WebpMode.LEGACY -> clampQuality(requested)
            lossless -> 100
            else -> minOf(clampQuality(requested), 99)
        }
}
