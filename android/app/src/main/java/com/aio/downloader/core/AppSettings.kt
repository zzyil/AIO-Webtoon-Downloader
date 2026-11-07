package com.aio.downloader.core

import android.content.Context
import org.json.JSONObject

/**
 * The preferences that belong to the DEVICE rather than to one download.
 *
 * ── WHY THIS IS SEPARATE FROM [DownloadForm] ───────────────────────────────
 * DownloadForm answers "what do I want from THIS series" — format, chapter
 * range, groups. The fields here answer "how should this app behave on this
 * phone", and three different code paths need them:
 *
 *   * a download           (resource limits -> the concurrency knobs)
 *   * a cross-site search  (limits again, plus the search tuning)
 *   * a library update check / a resume
 *
 * Before this file existed the Search screen reached for `vm.form` and handed a
 * whole download form to a search, which worked only because Python ignores
 * keys it does not recognize. Splitting them makes the ownership legible: the
 * form is what the user is composing, this is what the app is configured to do.
 *
 * Resource limits MOVED HERE out of DownloadForm for exactly that reason, and
 * because the desktop keeps them in Settings too (see the resource-limits
 * memory + UI-source/electron/resource-limits.js, the reference table).
 *
 * ── WHY SharedPreferences, WHERE THE PLAN SAID DataStore ───────────────────
 * A DELIBERATE DEVIATION. Every consumer above needs a SYNCHRONOUS read from a
 * non-composable context: DownloadService resolves argv on its worker thread,
 * MainActivity's intent harness runs on the main thread, and
 * AioViewModel.queueLibraryUpdate builds a settings blob inline. DataStore's
 * only synchronous read is `runBlocking`, which on the main thread is precisely
 * the ANR it exists to prevent; the alternative — collecting a Flow into a
 * StateFlow — has a window at process start where the value is still the
 * default, and a job enqueued in that window would silently run with the
 * user's settings ignored. That is the failure mode this codebase least wants
 * (see aio_android._require_settings_object's docstring for the same argument).
 * DataStore's real advantages (Flow observation across processes, atomic
 * multi-writer updates) buy nothing here: one screen writes, a few hundred
 * bytes, one process. DownloadForm already uses SharedPreferences, so this also
 * keeps the app on ONE persistence mechanism.
 *
 * ── THE KEYS ARE THE CONTRACT ──────────────────────────────────────────────
 * [putInto] writes into the same settings dict `aio_android.build_argv` and
 * `build_search_argv` consume, and both look every key up BY STRING. A typo
 * does not throw; it produces a run that quietly ignores the setting. Grep any
 * key you add in aio_android.py and confirm it lands in `_VALUED_FLAGS`,
 * `_BOOL_FLAGS`, the resource-limit block, or `build_search_argv`.
 */
data class AppSettings(
    /**
     * Absolute path to the manga library, or "" for the default app-scoped
     * directory. A custom path requires MANAGE_EXTERNAL_STORAGE and only takes
     * effect on the next app start — `aio_android.configure` sets the process
     * CWD and env once, and aio-dl.py bakes several constants in at import.
     * See [Aio.resolveLibraryDir].
     */
    val libraryPath: String = "",

    // ── resource limits (a hard override, not a ceiling) ──────────────────
    val networkLimit: String = LIMIT_UNLIMITED,
    val cpuLimit: String = LIMIT_UNLIMITED,

    // ── search ────────────────────────────────────────────────────────────
    /** Per-site timeout in seconds. aio-dl.py's own default is 20. */
    val searchTimeout: Int = DEFAULT_SEARCH_TIMEOUT,
    /** WRatio floor for a search hit, 0..1. aio-dl.py's own default is 0.55. */
    val searchMinMatch: Float = DEFAULT_SEARCH_MIN_MATCH,
    /**
     * Restrict the fan-out to handlers listed in sites/quality_seed.json.
     * Reaches DOWNLOADS as well as searches, deliberately: multi-source
     * discovery IS a cross-site search, and the desktop's boolMap forwards it
     * the same way.
     */
    val seededOnly: Boolean = false,

    // ── library ───────────────────────────────────────────────────────────
    /**
     * Pass `--collapse-splits` to update checks, so a chapter a site publishes
     * as 12.1/12.2 counts as the one chapter it is instead of reading as two
     * missing ones forever.
     */
    val collapseSplits: Boolean = false,
    /** Sweep every series for updates when the Library tab is first opened. */
    val autoCheckUpdates: Boolean = false,
) {

    /**
     * Merge the global keys into a settings dict.
     *
     * ONE method for both the download and the search path, on purpose: the
     * Python builders each ignore what they do not use (`searchTimeout` is
     * inert in build_argv, `cpuLimit` is inert in build_search_argv), so a
     * single merge cannot drift out of step with itself the way two would.
     *
     * Values equal to the Python-side default are OMITTED. Not cosmetic — the
     * built command line is logged and read by a human when a run misbehaves,
     * and a wall of default flags is where the one odd flag hides.
     */
    fun putInto(target: JSONObject): JSONObject = target.apply {
        if (networkLimit != LIMIT_UNLIMITED) put("networkLimit", networkLimit)
        if (cpuLimit != LIMIT_UNLIMITED) put("cpuLimit", cpuLimit)
        if (searchTimeout != DEFAULT_SEARCH_TIMEOUT) put("searchTimeout", searchTimeout)
        if (searchMinMatch != DEFAULT_SEARCH_MIN_MATCH) put("searchMinMatch", searchMinMatch)
        if (seededOnly) put("seededOnly", true)
        if (collapseSplits) put("collapseSplits", true)
    }

    /**
     * The global keys alone, as a settings blob.
     *
     * Used where there is no download form to merge with: a cross-site search,
     * and a resume (whose per-download settings come back off disk from
     * `run_params.json`, leaving only the throttle to resolve).
     */
    fun toGlobalSettingsJson(): String = putInto(JSONObject()).toString()

    /** True when nothing has been changed from the shipped defaults. */
    val isDefault: Boolean get() = this == AppSettings()

    private fun persistJson(): String = JSONObject().apply {
        put("libraryPath", libraryPath)
        put("networkLimit", networkLimit)
        put("cpuLimit", cpuLimit)
        put("searchTimeout", searchTimeout)
        put("searchMinMatch", searchMinMatch.toDouble())
        put("seededOnly", seededOnly)
        put("collapseSplits", collapseSplits)
        put("autoCheckUpdates", autoCheckUpdates)
    }.toString()

    companion object {
        const val LIMIT_UNLIMITED = "unlimited"

        /** aio-dl.py's argparse defaults — grep `--search-timeout` there. */
        const val DEFAULT_SEARCH_TIMEOUT = 20
        const val DEFAULT_SEARCH_MIN_MATCH = 0.55f

        private const val PREFS = "aio_ui"
        private const val KEY = "app_settings_v1"

        /** DownloadForm's blob, read once for the resource-limit migration. */
        private const val LEGACY_FORM_KEY = "download_form_v1"

        internal fun prefs(context: Context) =
            context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        /**
         * Persist. `commit()` rather than `apply()`: a library-path change is
         * followed by the user killing and relaunching the app, and `apply()`
         * only guarantees the write survives a normal process death — not one
         * the user causes seconds later from the task switcher.
         */
        fun save(context: Context, settings: AppSettings) {
            prefs(context).edit().putString(KEY, settings.persistJson()).commit()
        }

        /**
         * Restore, falling back to the default field by field.
         *
         * MIGRATION: resource limits used to live in DownloadForm's blob. On
         * the first load after that move there is no app_settings_v1 yet, so
         * they are lifted out of the form blob instead of silently resetting
         * someone's "low data" choice to unlimited.
         */
        fun load(context: Context): AppSettings {
            val store = prefs(context)
            val raw = store.getString(KEY, null)
                ?: return migratedFromForm(store.getString(LEGACY_FORM_KEY, null))

            return runCatching {
                val o = JSONObject(raw)
                val d = AppSettings()
                AppSettings(
                    libraryPath = o.optString("libraryPath", d.libraryPath),
                    networkLimit = o.optString("networkLimit", d.networkLimit),
                    cpuLimit = o.optString("cpuLimit", d.cpuLimit),
                    searchTimeout = o.optInt("searchTimeout", d.searchTimeout),
                    searchMinMatch = o.optDouble(
                        "searchMinMatch",
                        d.searchMinMatch.toDouble(),
                    ).toFloat(),
                    seededOnly = o.optBoolean("seededOnly", d.seededOnly),
                    collapseSplits = o.optBoolean("collapseSplits", d.collapseSplits),
                    autoCheckUpdates = o.optBoolean("autoCheckUpdates", d.autoCheckUpdates),
                )
            }.getOrElse { AppSettings() }
        }

        internal fun migratedFromForm(formJson: String?): AppSettings {
            if (formJson.isNullOrBlank()) return AppSettings()
            return runCatching {
                val o = JSONObject(formJson)
                AppSettings(
                    networkLimit = o.optString("networkLimit", LIMIT_UNLIMITED),
                    cpuLimit = o.optString("cpuLimit", LIMIT_UNLIMITED),
                )
            }.getOrElse { AppSettings() }
        }
    }
}

/**
 * Resource-limit presets.
 *
 * The LEVEL NAMES are the contract with `aio_android.apply_network_limit` /
 * `cpu_percent_for_level`; the numbers live there (and in the two JS twins
 * `tests/test_android_resource_limits.py` diffs against), never here. An
 * unrecognized level fails OPEN to unlimited on the Python side, so a corrupt
 * setting can only ever run at full speed — never silently throttle someone to
 * two connections.
 */
val RESOURCE_LEVELS: List<Pair<String, String>> = listOf(
    AppSettings.LIMIT_UNLIMITED to "Unlimited",
    "high" to "High",
    "balanced" to "Balanced",
    "low" to "Low",
)

fun resourceLevelLabel(value: String): String =
    RESOURCE_LEVELS.firstOrNull { it.first == value }?.second ?: value
