package com.aio.downloader.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The device-level settings, and the dict they merge into.
 *
 * WHY THIS IS WORTH A TEST: every key [AppSettings.putInto] writes is looked up
 * BY STRING on the Python side (`_VALUED_FLAGS` / `_BOOL_FLAGS` /
 * `build_search_argv`). A renamed or misspelled key does not throw — it produces
 * a download that quietly ignores the setting, which is indistinguishable from
 * the setting not working and is the single most expensive kind of bug to chase
 * across a JNI boundary.
 *
 * The omit-when-default rule is the other half. The built command line is logged
 * and read by a human when a run misbehaves, and a wall of default flags is
 * where the one genuinely odd flag hides.
 *
 * Cross-file: aio_android.build_argv / build_search_argv (the consumers),
 * tests/test_android_argv.py (the Python side of the same contract).
 */
class AppSettingsTest {

    private fun merged(settings: AppSettings) = settings.putInto(JSONObject())

    // ── the merge ──────────────────────────────────────────────────────────

    @Test
    fun `defaults emit nothing at all`() {
        assertEquals(0, merged(AppSettings()).length())
    }

    @Test
    fun `resource limits travel under the keys build_argv reads`() {
        val json = merged(AppSettings(networkLimit = "low", cpuLimit = "balanced"))
        assertEquals("low", json.getString("networkLimit"))
        assertEquals("balanced", json.getString("cpuLimit"))
    }

    @Test
    fun `unlimited is the ABSENCE of the key, not the string`() {
        // apply_network_limit treats an unrecognized level as unlimited, so
        // emitting it would work — but "unlimited" appearing in a logged command
        // line reads as a flag that was set, which is the opposite of true.
        val json = merged(AppSettings(networkLimit = AppSettings.LIMIT_UNLIMITED))
        assertFalse(json.has("networkLimit"))
    }

    @Test
    fun `search tuning is omitted at aio-dl's own defaults`() {
        val json = merged(
            AppSettings(
                searchTimeout = AppSettings.DEFAULT_SEARCH_TIMEOUT,
                searchMinMatch = AppSettings.DEFAULT_SEARCH_MIN_MATCH,
            ),
        )
        assertFalse(json.has("searchTimeout"))
        assertFalse(json.has("searchMinMatch"))
    }

    @Test
    fun `search tuning travels once changed`() {
        val json = merged(AppSettings(searchTimeout = 35, searchMinMatch = 0.8f))
        assertEquals(35, json.getInt("searchTimeout"))
        assertEquals(0.8, json.getDouble("searchMinMatch"), 1e-6)
    }

    @Test
    fun `booleans emit only when true`() {
        assertFalse(merged(AppSettings(seededOnly = false)).has("seededOnly"))
        assertTrue(merged(AppSettings(seededOnly = true)).getBoolean("seededOnly"))
        assertFalse(merged(AppSettings(collapseSplits = false)).has("collapseSplits"))
        assertTrue(merged(AppSettings(collapseSplits = true)).getBoolean("collapseSplits"))
    }

    @Test
    fun `the library path never reaches the settings dict`() {
        // It is consumed by aio_android.configure at startup, not by build_argv.
        // Leaking it would put an absolute device path on every command line for
        // no behavioural effect.
        assertFalse(merged(AppSettings(libraryPath = "/storage/emulated/0/Manga")).has("libraryPath"))
    }

    @Test
    fun `autoCheckUpdates is a UI behaviour, not a CLI flag`() {
        assertFalse(merged(AppSettings(autoCheckUpdates = true)).has("autoCheckUpdates"))
    }

    // ── merging with the download form ─────────────────────────────────────

    @Test
    fun `a download settings blob carries both halves`() {
        val form = DownloadForm(format = "cbz", chapters = "1-10", komikku = true)
        val json = JSONObject(form.toSettingsJson("https://x/y", AppSettings(networkLimit = "low")))
        assertEquals("https://x/y", json.getString("url"))
        assertEquals("cbz", json.getString("format"))
        assertTrue(json.getBoolean("komikku"))
        assertEquals("low", json.getString("networkLimit"))
    }

    @Test
    fun `a library update keeps the global keys through its overrides`() {
        // libraryUpdateSettingsJson rebuilds the object to override three
        // series-owned fields; the global keys have to survive that round trip
        // or an update check would silently ignore a data limit.
        val json = JSONObject(
            DownloadForm().libraryUpdateSettingsJson(
                url = "https://x/y",
                chapters = "51-53",
                seriesFormat = "cbz",
                seriesLanguage = "ja",
                site = "mangadex",
                app = AppSettings(networkLimit = "balanced", cpuLimit = "low"),
            ),
        )
        assertEquals("51-53", json.getString("chapters"))
        assertEquals("ja", json.getString("language"))
        assertEquals("balanced", json.getString("networkLimit"))
        assertEquals("low", json.getString("cpuLimit"))
    }

    @Test
    fun `a search blob is global keys only`() {
        val json = JSONObject(AppSettings(networkLimit = "low", seededOnly = true).toGlobalSettingsJson())
        assertEquals("low", json.getString("networkLimit"))
        assertTrue(json.getBoolean("seededOnly"))
        // No form fields: a search has no format, no chapter range, no URL. The
        // Search screen used to hand over a whole half-composed download form.
        assertFalse(json.has("format"))
        assertFalse(json.has("url"))
        assertFalse(json.has("chapters"))
    }

    // ── persistence ────────────────────────────────────────────────────────

    @Test
    fun `resource limits are lifted out of the old form blob on first load`() {
        // They lived in DownloadForm's blob before M7. Without this migration
        // the move would silently reset someone's "low data" choice to
        // unlimited — the exact setting whose whole point is that it is not
        // ignored.
        val legacy = """{"url":"","format":"cbz","networkLimit":"low","cpuLimit":"balanced"}"""
        val migrated = AppSettings.migratedFromForm(legacy)
        assertEquals("low", migrated.networkLimit)
        assertEquals("balanced", migrated.cpuLimit)
    }

    @Test
    fun `migration tolerates a missing or corrupt form blob`() {
        assertEquals(AppSettings(), AppSettings.migratedFromForm(null))
        assertEquals(AppSettings(), AppSettings.migratedFromForm(""))
        assertEquals(AppSettings(), AppSettings.migratedFromForm("{not json"))
        // A form blob that predates the fields entirely.
        assertEquals(AppSettings(), AppSettings.migratedFromForm("""{"format":"cbz"}"""))
    }

    @Test
    fun `isDefault tracks the shipped defaults`() {
        assertTrue(AppSettings().isDefault)
        assertFalse(AppSettings(seededOnly = true).isDefault)
    }

    @Test
    fun `every resource level has a label`() {
        // The level NAMES are the contract with apply_network_limit; a level
        // with no label would render as its raw key in the dropdown.
        listOf(AppSettings.LIMIT_UNLIMITED, "high", "balanced", "low").forEach { level ->
            assertTrue(level, RESOURCE_LEVELS.any { it.first == level })
            assertFalse(level, resourceLevelLabel(level) == level)
        }
    }
}
