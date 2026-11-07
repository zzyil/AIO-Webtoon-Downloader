package com.aio.downloader.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [AppUpdater]'s pure half — feed parsing and applicability.
 *
 * WHY THIS FILE EXISTS. `AppUpdater` decides, with no user present, whether to
 * hand the system installer a 50 MB APK. Every wrong answer here is silent in a
 * different direction: a missed key makes the app never update and say nothing,
 * while a missing guard offers a package Android will refuse *after* the user
 * has spent the data. Neither shows up as a crash, so neither shows up in
 * manual testing.
 *
 * WHAT IS AND IS NOT REACHABLE. `UpdateFeed` and `Applicability` are `internal`
 * rather than `private` precisely so this file can reach them; the rest of
 * AppUpdater is Context/Notification/DownloadManager-bound and needs a device.
 * [AppUpdater.UpdateFeed.applicability] takes `installedVersionCode`,
 * `deviceSdk` and `deviceAbis` as parameters whose defaults read
 * `BuildConfig`/`Build` — **every test below passes all three explicitly**,
 * because the defaults would hit the unit-test `android.jar` stubs, where
 * `Build.SUPPORTED_ABIS` is null and `Build.VERSION.SDK_INT` is 0. Kotlin only
 * evaluates a default when the argument is omitted, so passing them keeps this
 * off the device.
 *
 * The real org.json is on the test classpath (app/build.gradle.kts, grep
 * `libs.json`) — the stubbed one in android.jar throws on every method. Same
 * argument as core/LibraryModelsTest.kt.
 */
class AppUpdaterTest {

    private val validSha = "a".repeat(64)
    private val otherSha = "b".repeat(64)

    /**
     * A feed that passes everything, so each test can vary exactly one field
     * and attribute the outcome to it.
     */
    private fun feedJson(
        schema: Int = 1,
        versionCode: Int = 500,
        versionName: String = "2026.820.915",
        releaseDate: String? = "2026-08-20T09:15:00Z",
        apk: String = "aio-downloader-2026.820.915.apk",
        size: Long = 52_000_000L,
        sha256: String = validSha,
        minSdk: Int = 26,
        abis: List<String>? = listOf("arm64-v8a"),
        signingSha256: String? = validSha,
    ): String = JSONObject().apply {
        put("schema", schema)
        put("versionCode", versionCode)
        put("versionName", versionName)
        releaseDate?.let { put("releaseDate", it) }
        put("apk", apk)
        put("size", size)
        put("sha256", sha256)
        put("minSdk", minSdk)
        abis?.let { put("abis", org.json.JSONArray(it)) }
        signingSha256?.let { put("signingSha256", it) }
    }.toString()

    private fun parsed(raw: String) = AppUpdater.UpdateFeed.parse(raw)

    private fun applicability(
        feed: AppUpdater.UpdateFeed.Feed,
        installedSigning: String? = validSha,
        installedVersionCode: Int = 400,
        deviceSdk: Int = 35,
        deviceAbis: List<String> = listOf("arm64-v8a"),
    ) = AppUpdater.UpdateFeed.applicability(
        feed = feed,
        installedSigningSha256 = installedSigning,
        installedVersionCode = installedVersionCode,
        deviceSdk = deviceSdk,
        deviceAbis = deviceAbis,
    )

    // ── parse ─────────────────────────────────────────────────────────────

    @Test
    fun `a well-formed feed maps every field`() {
        val feed = parsed(feedJson())!!
        assertEquals(1, feed.schema)
        assertEquals(500, feed.versionCode)
        assertEquals("2026.820.915", feed.versionName)
        assertEquals("aio-downloader-2026.820.915.apk", feed.apk)
        assertEquals(52_000_000L, feed.size)
        assertEquals(validSha, feed.sha256)
        assertEquals(26, feed.minSdk)
        assertEquals(listOf("arm64-v8a"), feed.abis)
        assertEquals(validSha, feed.signingSha256)
        assertEquals(1_787_217_300_000L, feed.releaseDateMillis)
    }

    /**
     * The traversal guard. The feed is ours, but `apk` becomes a filename in
     * the update directory, so a separator would let it be written elsewhere.
     */
    @Test
    fun `an apk name containing a path separator is rejected`() {
        assertNull(parsed(feedJson(apk = "../../evil.apk")))
        assertNull(parsed(feedJson(apk = "sub/dir.apk")))
        assertNull(parsed(feedJson(apk = "sub\\dir.apk")))
        assertNull(parsed(feedJson(apk = "..")))
        assertNull(parsed(feedJson(apk = "")))
    }

    @Test
    fun `a sha256 that is not exactly 64 characters is rejected`() {
        assertNull(parsed(feedJson(sha256 = "a".repeat(63))))
        assertNull(parsed(feedJson(sha256 = "a".repeat(65))))
        assertNull(parsed(feedJson(sha256 = "")))
    }

    @Test
    fun `a non-positive versionCode is rejected`() {
        assertNull(parsed(feedJson(versionCode = 0)))
        assertNull(parsed(feedJson(versionCode = -1)))
    }

    /** Never throws — a truncated download must read as "no update", not a crash. */
    @Test
    fun `malformed payloads return null rather than throwing`() {
        assertNull(parsed(""))
        assertNull(parsed("not json"))
        assertNull(parsed("{\"versionCode\": 500"))
        assertNull(parsed("[]"))
    }

    /**
     * A signing digest of the wrong length is dropped rather than kept, so the
     * applicability check treats it as absent and defers to the OS instead of
     * comparing against a value that could never match.
     */
    @Test
    fun `a malformed signingSha256 becomes null instead of a value that can never match`() {
        assertNull(parsed(feedJson(signingSha256 = "short"))!!.signingSha256)
        assertNull(parsed(feedJson(signingSha256 = null))!!.signingSha256)
    }

    // ── parseIso ──────────────────────────────────────────────────────────

    /**
     * The maturity gate fails OPEN: an unparseable date must not permanently
     * block updates, it just skips the delay.
     */
    @Test
    fun `parseIso returns null for absent or unparseable values`() {
        assertNull(AppUpdater.UpdateFeed.parseIso(null))
        assertNull(AppUpdater.UpdateFeed.parseIso(""))
        assertNull(AppUpdater.UpdateFeed.parseIso("   "))
        assertNull(AppUpdater.UpdateFeed.parseIso("2026-08-20"))
        assertNull(AppUpdater.UpdateFeed.parseIso("yesterday"))
    }

    @Test
    fun `parseIso reads an RFC-3339 instant`() {
        assertEquals(0L, AppUpdater.UpdateFeed.parseIso("1970-01-01T00:00:00Z"))
        assertEquals(1_787_217_300_000L, AppUpdater.UpdateFeed.parseIso("2026-08-20T09:15:00Z"))
    }

    // ── applicability ─────────────────────────────────────────────────────

    @Test
    fun `the happy path is applicable`() {
        assertEquals(
            AppUpdater.Applicability.Applicable,
            applicability(parsed(feedJson())!!),
        )
    }

    /** No downgrades, ever — and equal is not newer. */
    @Test
    fun `an equal or lower versionCode is never newer`() {
        assertEquals(
            AppUpdater.Applicability.NotNewer,
            applicability(parsed(feedJson(versionCode = 400))!!, installedVersionCode = 400),
        )
        assertEquals(
            AppUpdater.Applicability.NotNewer,
            applicability(parsed(feedJson(versionCode = 399))!!, installedVersionCode = 400),
        )
    }

    /**
     * Checked BEFORE the version comparison: an unreadable feed must not be
     * silently treated as "nothing new", or a format bump would look identical
     * to being up to date.
     */
    @Test
    fun `an unknown schema is blocked, not silently ignored`() {
        val result = applicability(parsed(feedJson(schema = 2))!!)
        assertTrue(result is AppUpdater.Applicability.Blocked)
        assertTrue((result as AppUpdater.Applicability.Blocked).reason.contains("format 2"))
    }

    @Test
    fun `a minSdk above the device is blocked and names both numbers`() {
        val result = applicability(parsed(feedJson(minSdk = 36))!!, deviceSdk = 35)
        assertTrue(result is AppUpdater.Applicability.Blocked)
        val reason = (result as AppUpdater.Applicability.Blocked).reason
        assertTrue(reason.contains("36"))
        assertTrue(reason.contains("35"))
    }

    @Test
    fun `an ABI the device does not have is blocked`() {
        val result = applicability(
            parsed(feedJson(abis = listOf("arm64-v8a")))!!,
            deviceAbis = listOf("x86_64"),
        )
        assertTrue(result is AppUpdater.Applicability.Blocked)
    }

    /** An empty ABI list means "universal", not "matches nothing". */
    @Test
    fun `an empty ABI list does not block`() {
        assertEquals(
            AppUpdater.Applicability.Applicable,
            applicability(parsed(feedJson(abis = emptyList()))!!, deviceAbis = listOf("x86_64")),
        )
    }

    /**
     * The sideloaded-from-elsewhere case. Caught here so the user is told
     * before spending ~50 MB on a package the package manager would refuse.
     */
    @Test
    fun `a different signing key is blocked before downloading`() {
        val result = applicability(parsed(feedJson(signingSha256 = validSha))!!, installedSigning = otherSha)
        assertTrue(result is AppUpdater.Applicability.Blocked)
        assertTrue((result as AppUpdater.Applicability.Blocked).reason.contains("different key"))
    }

    @Test
    fun `the signing comparison ignores hex case`() {
        assertEquals(
            AppUpdater.Applicability.Applicable,
            applicability(
                parsed(feedJson(signingSha256 = validSha.uppercase()))!!,
                installedSigning = validSha.lowercase(),
            ),
        )
    }

    /**
     * Absent on either side => skip the check. The OS still enforces signature
     * identity at install time, so guessing here could only ever produce a
     * false block.
     */
    @Test
    fun `a missing digest on either side skips the signing check`() {
        assertEquals(
            AppUpdater.Applicability.Applicable,
            applicability(parsed(feedJson(signingSha256 = null))!!, installedSigning = validSha),
        )
        assertEquals(
            AppUpdater.Applicability.Applicable,
            applicability(parsed(feedJson(signingSha256 = validSha))!!, installedSigning = null),
        )
    }
}
