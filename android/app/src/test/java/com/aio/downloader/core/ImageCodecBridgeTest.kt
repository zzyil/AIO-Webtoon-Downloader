package com.aio.downloader.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

/**
 * Pins the pure half of ImageCodecBridge.kt — the API-level rule and the
 * quality arithmetic.
 *
 * These run on a plain JVM (`gradlew :app:testDebugUnitTest`), which is why
 * [WebpMode] and [ImageCodecPolicy] carry no Android types in any signature.
 * The `Bitmap` calls next door are NOT covered here and cannot be: `android.jar`
 * on the unit-test classpath is a stub whose methods throw, and this module does
 * not set `returnDefaultValues`. Verifying an actual encode needs a device — see
 * `aio_android.image_capabilities()`, which reports what the install achieved,
 * and android/TESTING.md.
 *
 * WHAT THESE ACTUALLY PROTECT: the app's minSdk is 26, and
 * `Bitmap.CompressFormat.WEBP_LOSSY` / `WEBP_LOSSLESS` do not exist until API
 * 30. Half the supported range therefore runs a different code path from the one
 * a developer sees on a modern test device, and on that path "lossless" is not a
 * flag at all — it is quality == 100. A regression there is invisible until
 * somebody's archival CBZ turns out to be lossy.
 *
 * Cross-file: sites/image_codec.py (the Python half of this seam),
 * aio_android.py:set_image_codec_bridge (what installs it).
 */
class ImageCodecBridgeTest {

    // ── WebpMode.of ───────────────────────────────────────────────────────

    @Test
    fun `modern devices get the split lossy and lossless formats`() {
        assertEquals(WebpMode.LOSSY, WebpMode.of(30, lossless = false))
        assertEquals(WebpMode.LOSSLESS, WebpMode.of(30, lossless = true))
        assertEquals(WebpMode.LOSSY, WebpMode.of(36, lossless = false))
        assertEquals(WebpMode.LOSSLESS, WebpMode.of(36, lossless = true))
    }

    @Test
    fun `pre-API-30 devices get the legacy format regardless of the flag`() {
        // minSdk is 26, so this is a real quarter of the version range and not
        // a hypothetical.
        for (sdk in 26..29) {
            assertEquals("sdk $sdk", WebpMode.LEGACY, WebpMode.of(sdk, lossless = false))
            assertEquals("sdk $sdk", WebpMode.LEGACY, WebpMode.of(sdk, lossless = true))
        }
    }

    @Test
    fun `the split happens exactly at API 30, not around it`() {
        assertEquals(WebpMode.LEGACY, WebpMode.of(WebpMode.SPLIT_FORMATS_SDK - 1, false))
        assertNotEquals(WebpMode.LEGACY, WebpMode.of(WebpMode.SPLIT_FORMATS_SDK, false))
    }

    @Test
    fun `the tested constant is the same API level the bridge branches on`() {
        // ImageCodecBridge.compressFormat spells its guard as
        // `SDK_INT >= Build.VERSION_CODES.R` rather than reusing this constant,
        // because lint's NewApi check cannot follow a guard that reaches the
        // API-30 symbols through an enum. That redundancy is only safe while
        // the two numbers agree. Build.VERSION_CODES.R is 30; it is written as
        // a literal here because android.jar on the unit-test classpath is a
        // stub and its constants are not readable.
        assertEquals(30, WebpMode.SPLIT_FORMATS_SDK)
    }

    // ── ImageCodecPolicy.clampQuality ─────────────────────────────────────

    @Test
    fun `quality is clamped into libwebp's range`() {
        assertEquals(0, ImageCodecPolicy.clampQuality(-1))
        assertEquals(0, ImageCodecPolicy.clampQuality(Int.MIN_VALUE))
        assertEquals(100, ImageCodecPolicy.clampQuality(101))
        assertEquals(100, ImageCodecPolicy.clampQuality(Int.MAX_VALUE))
    }

    @Test
    fun `an in-range quality is passed through untouched`() {
        // aio-dl.py's two real call shapes are q85 (webp_q85) and q100
        // (webp_lossless) — grep save_kwargs there.
        for (q in intArrayOf(0, 1, 85, 99, 100)) {
            assertEquals(q, ImageCodecPolicy.clampQuality(q))
        }
    }

    // ── ImageCodecPolicy.qualityFor ───────────────────────────────────────

    @Test
    fun `legacy lossless is forced to 100 because that is how it is expressed`() {
        // THE bug this file exists to prevent: CompressFormat.WEBP has no
        // lossless flag — it encodes losslessly at exactly 100 and lossily
        // below. Passing the caller's 85 through here would silently downgrade
        // an archival encode on every API 26-29 device.
        assertEquals(100, ImageCodecPolicy.qualityFor(WebpMode.LEGACY, 85, lossless = true))
        assertEquals(100, ImageCodecPolicy.qualityFor(WebpMode.LEGACY, 0, lossless = true))
    }

    @Test
    fun `legacy lossy keeps the caller's quality`() {
        assertEquals(85, ImageCodecPolicy.qualityFor(WebpMode.LEGACY, 85, lossless = false))
    }

    @Test
    fun `legacy lossy at 100 is capped to 99 because 100 means lossless`() {
        // The SAME rule as the lossless test above, read the other way, and it
        // used to be implemented in one direction only. 100 IS the lossless
        // switch for CompressFormat.WEBP, so passing a lossy request of 100
        // through unchanged produced a lossless file — far bigger and far
        // slower, i.e. the opposite of what --webtoon-recompress-quality asks
        // for. The CLI accepts 1-100, so 100 is a value a user can really pick.
        assertEquals(99, ImageCodecPolicy.qualityFor(WebpMode.LEGACY, 100, lossless = false))
        // Over-range input must land on the same side of the switch.
        assertEquals(99, ImageCodecPolicy.qualityFor(WebpMode.LEGACY, 4000, lossless = false))
        // ...and 99 or below is still passed straight through.
        assertEquals(99, ImageCodecPolicy.qualityFor(WebpMode.LEGACY, 99, lossless = false))
    }

    @Test
    fun `only the legacy path caps, since the split formats carry their own flag`() {
        // On API 30+ quality 100 is an ordinary lossy quality and must survive:
        // WEBP_LOSSY never reinterprets it, so capping there would silently
        // degrade the best lossy tier for no reason.
        assertEquals(100, ImageCodecPolicy.qualityFor(WebpMode.LOSSY, 100, lossless = false))
        assertEquals(100, ImageCodecPolicy.qualityFor(WebpMode.LOSSLESS, 100, lossless = true))
    }

    @Test
    fun `the split formats carry their own losslessness, so quality is untouched`() {
        // WEBP_LOSSLESS ignores quality outright; forcing 100 here would be
        // harmless but would hide a real difference between the two paths.
        assertEquals(85, ImageCodecPolicy.qualityFor(WebpMode.LOSSLESS, 85, lossless = true))
        assertEquals(85, ImageCodecPolicy.qualityFor(WebpMode.LOSSY, 85, lossless = false))
    }

    @Test
    fun `qualityFor still clamps`() {
        assertEquals(100, ImageCodecPolicy.qualityFor(WebpMode.LOSSY, 4000, lossless = false))
        assertEquals(0, ImageCodecPolicy.qualityFor(WebpMode.LOSSY, -7, lossless = false))
    }

    // ── the aio-dl.py call shapes, end to end through the pure layer ──────

    @Test
    fun `aio-dl's lossless tier stays lossless on every supported API level`() {
        // save_kwargs = dict(format="WebP", lossless=True, method=4, quality=100)
        for (sdk in intArrayOf(26, 29, 30, 36)) {
            val mode = WebpMode.of(sdk, lossless = true)
            val quality = ImageCodecPolicy.qualityFor(mode, 100, lossless = true)
            val reallyLossless = mode == WebpMode.LOSSLESS ||
                (mode == WebpMode.LEGACY && quality == 100)
            org.junit.Assert.assertTrue("sdk $sdk", reallyLossless)
        }
    }

    @Test
    fun `aio-dl's q85 tier stays lossy on every supported API level`() {
        // save_kwargs = dict(format="WebP", quality=85, method=2)
        for (sdk in intArrayOf(26, 29, 30, 36)) {
            val mode = WebpMode.of(sdk, lossless = false)
            assertEquals("sdk $sdk", 85, ImageCodecPolicy.qualityFor(mode, 85, lossless = false))
            assertNotEquals("sdk $sdk", WebpMode.LOSSLESS, mode)
        }
    }
}
