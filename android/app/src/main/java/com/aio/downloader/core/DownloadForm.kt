package com.aio.downloader.core

import android.content.Context
import org.json.JSONObject

/**
 * The Download screen's form state, and its translation into the settings dict
 * `aio_android.build_argv` consumes.
 *
 * ── WHY THE KEYS MATTER MORE THAN THE VALUES ───────────────────────────────
 * Every name in [toSettingsJson] is looked up by string on the Python side
 * (`_VALUED_FLAGS` / `_BOOL_FLAGS` in aio_android.py). A typo does not throw;
 * it produces a run that silently ignores the setting. When adding a field,
 * grep the key in aio_android.py and confirm it lands in one of those tables,
 * or in one of the special cases at the bottom of `build_argv`.
 *
 * ── WHY SO FEW FIELDS ──────────────────────────────────────────────────────
 * The CLI has 99 flags and the desktop surfaces nearly all of them across a New
 * tab and a 2,890-line Settings tab. This is the phone: the fields below are
 * the ones that change what you get, and everything else keeps aio-dl.py's own
 * default. `--modernize` (JXL/AVIF) is deliberately absent — it has no
 * Android wheel behind it (no pillow-jxl), so offering it would offer a run
 * that cannot work.
 *
 * Persisted as one JSON blob in SharedPreferences, so the form survives
 * rotation, process death and a reboot.
 *
 * ── WHAT IS *NOT* HERE ─────────────────────────────────────────────────────
 * Anything that describes the DEVICE rather than this download lives in
 * [AppSettings]: the resource limits, the search tuning, the library root.
 * They moved there in M7 because a search and a library update check need them
 * too, and reaching into a half-filled download form for them (which is what
 * the Search screen used to do) made a per-series form look like global state.
 * [toSettingsJson] merges the two back together at the one place that builds a
 * settings dict.
 */
data class DownloadForm(
    val url: String = "",

    // ── primary ───────────────────────────────────────────────────────────
    /**
     * CBZ, where the desktop defaults to PDF. This is the one default
     * deliberately changed for the platform: an Android library exists to be
     * read on the device, in Mihon / Komikku / Tachiyomi, and all of them want
     * CBZ. A PDF of a vertical webtoon on a phone is close to unreadable.
     */
    val format: String = "cbz",
    val epubLayout: String = "vertical",
    val chapters: String = "all",
    val language: String = "en",
    val komikku: Boolean = false,
    val multiSource: Boolean = false,

    // ── multi-source detail ───────────────────────────────────────────────
    /** Opt-OUT nested inside the opt-in; absent means on, so only false travels. */
    val multiSourceLazy: Boolean = true,
    val multiSourceQualityMin: Float = 0.65f,

    // ── image handling ────────────────────────────────────────────────────
    /**
     * 100, not aio-dl.py's argparse default of 85. Any explicit `--quality` below
     * 100 disables the CBZ byte-preserving fast path (aio-dl.py Phase G4, grep
     * `_user_set_quality`), and since this form always emits a quality, an 85
     * default would push every CBZ download through a needless decode/re-encode.
     * The desktop diverges the same way and for the same reason.
     */
    val quality: Int = 100,
    val scaling: Int = 100,
    val width: String = "",
    val noPartials: Boolean = false,

    // ── output ────────────────────────────────────────────────────────────
    val keepChapters: Boolean = false,
    val noFinalFile: Boolean = false,
    val keepImages: Boolean = false,
    val noProcessing: Boolean = false,

    // ── groups ────────────────────────────────────────────────────────────
    val group: String = "",
    val excludeGroup: String = "",
    val mtl: String = "avoid",

    // ── LINE Webtoon recompression ────────────────────────────────────────
    val webtoonRecompress: Boolean = false,
    val webtoonRecompressQuality: Int = 85,

    // ── network ───────────────────────────────────────────────────────────
    val imageWorkers: Int = 3,
    val httpTimeout: Int = 30,
    val httpMaxRetries: Int = 6,
    val missedRetries: Int = 2,
    val noRetryMissedChapters: Boolean = false,
    val cookies: String = "",
) {

    /**
     * `--webtoon-recompress` needs an archive to write converted pages into, so
     * aio-dl.py hard-errors on `--format pdf/none`. `--komikku` coerces
     * format->cbz BEFORE that check, so it stays valid then. Drives both the
     * toggle's enabled state and the emit guard — build_argv re-checks this
     * anyway, but a control that offers an unlaunchable combination is a bug on
     * its own.
     */
    val recompressAllowed: Boolean
        get() = komikku || format == "cbz" || format == "epub"

    /** Split on newlines so a multi-line paste queues one job per URL. */
    fun urls(): List<String> = url.split("\n").map { it.trim() }.filter { it.isNotEmpty() }

    /**
     * One job's settings dict. [singleUrl] is the positional argument, [app]
     * contributes the device-level keys (resource limits and the rest).
     *
     * THE ONE PLACE the two halves are merged. Every enqueue path goes through
     * here — the Start button, the library update path below, the intent
     * harness — so a download queued an hour before a limit changed still picks
     * the new limit up, because the dict is built at start time and resolved
     * again inside `aio_android.build_argv`.
     *
     * Values equal to the Python-side default are OMITTED rather than emitted.
     * Not cosmetic: the command line is logged and read by a human when a run
     * misbehaves, and a wall of default flags is where a genuinely odd one
     * hides.
     */
    fun toSettingsJson(singleUrl: String, app: AppSettings = AppSettings()): String = JSONObject().apply {
        put("url", singleUrl)
        put("format", format)
        if (format == "epub") put("epubLayout", epubLayout)
        // build_argv drops chapters=="all" and mtl=="avoid" itself, but passing
        // them anyway keeps this function honest about what the user chose.
        put("chapters", chapters)
        put("language", language)
        put("quality", quality)
        if (scaling != 100) put("scaling", scaling)
        width.trim().toIntOrNull()?.let { put("width", it) }

        if (komikku) put("komikku", true)
        if (noPartials) put("noPartials", true)
        if (keepChapters) put("keepChapters", true)
        if (noFinalFile && keepChapters) put("noFinalFile", true)
        if (keepImages) put("keepImages", true)
        if (noProcessing) put("noProcessing", true)

        if (multiSource) {
            put("multiSource", true)
            if (multiSourceQualityMin != 0.65f) {
                put("multiSourceQualityMin", multiSourceQualityMin)
            }
            // Absent-means-on: build_argv emits --multi-source-lazy unless this
            // is an explicit false, so only the opt-out needs forwarding.
            if (!multiSourceLazy) put("multiSourceLazy", false)
        }

        group.trim().takeIf { it.isNotEmpty() }?.let { put("group", it) }
        excludeGroup.trim().takeIf { it.isNotEmpty() }?.let { put("excludeGroup", it) }
        if (mtl != "avoid") put("mtl", mtl)

        // Guarded so a saved-on toggle can never reach an incompatible format —
        // aio-dl.py rejects that combination at startup with a hard error.
        if (webtoonRecompress && recompressAllowed) {
            put("webtoonRecompress", true)
            if (webtoonRecompressQuality != 85) {
                put("webtoonRecompressQuality", webtoonRecompressQuality)
            }
        }

        if (imageWorkers != 3) put("imageWorkers", imageWorkers)
        if (httpTimeout != 30) put("httpTimeout", httpTimeout)
        if (httpMaxRetries != 6) put("httpMaxRetries", httpMaxRetries)
        if (noRetryMissedChapters) put("noRetryMissedChapters", true)
        if (missedRetries != 2) put("missedRetries", missedRetries)
        cookies.trim().takeIf { it.isNotEmpty() }?.let { put("cookies", it) }

        // Device-level keys last. The resource limits are then resolved INSIDE
        // build_argv — Android's analogue of the desktop's single spawn
        // chokepoint — so every download path honours the current level without
        // each one remembering to ask.
        app.putInto(this)
    }.toString()

    /**
     * Settings for "download the chapters this series is missing".
     *
     * Behavioural port of `UI-source/src/lib/downloadArgs.js:
     * buildLibraryDownloadArgs`: start from what the user has configured, then
     * let the SERIES override the three things that are properties of the
     * series rather than of the download — its format, its language, and the
     * handler it came from. Getting `--site` wrong on a URL two handlers both
     * claim is how an update lands from the wrong source.
     *
     * FORCES LAZY MULTI-SOURCE, overriding a global opt-out. An update is
     * typically a one-or-two chapter delta, and eager cross-site discovery costs
     * 30-80s before the first byte — so on the update path it would dwarf the
     * download it was meant to protect. Only meaningful when multi-source is on
     * at all; `build_argv` gates the flag on that, so this is a clean no-op
     * otherwise.
     */
    fun libraryUpdateSettingsJson(
        url: String,
        chapters: String,
        seriesFormat: String,
        seriesLanguage: String,
        site: String,
        app: AppSettings = AppSettings(),
    ): String = JSONObject(toSettingsJson(url, app)).apply {
        put("chapters", chapters)
        // library_state defaults an unknown format to "?", and params written by
        // an older build may carry anything — so only a format aio-dl.py
        // actually accepts is allowed to override the user's choice.
        if (seriesFormat in FORMATS) put("format", seriesFormat)
        if (seriesLanguage.isNotBlank()) put("language", seriesLanguage)
        if (site.isNotBlank()) put("site", site)
        remove("multiSourceLazy")
    }.toString()

    // ── persistence ───────────────────────────────────────────────────────

    /** The whole form, INCLUDING the URL field, so a half-typed entry survives. */
    private fun persistJson(): String = JSONObject().apply {
        put("url", url)
        put("format", format)
        put("epubLayout", epubLayout)
        put("chapters", chapters)
        put("language", language)
        put("komikku", komikku)
        put("multiSource", multiSource)
        put("multiSourceLazy", multiSourceLazy)
        put("multiSourceQualityMin", multiSourceQualityMin.toDouble())
        put("quality", quality)
        put("scaling", scaling)
        put("width", width)
        put("noPartials", noPartials)
        put("keepChapters", keepChapters)
        put("noFinalFile", noFinalFile)
        put("keepImages", keepImages)
        put("noProcessing", noProcessing)
        put("group", group)
        put("excludeGroup", excludeGroup)
        put("mtl", mtl)
        put("webtoonRecompress", webtoonRecompress)
        put("webtoonRecompressQuality", webtoonRecompressQuality)
        put("imageWorkers", imageWorkers)
        put("httpTimeout", httpTimeout)
        put("httpMaxRetries", httpMaxRetries)
        put("missedRetries", missedRetries)
        put("noRetryMissedChapters", noRetryMissedChapters)
        put("cookies", cookies)
    }.toString()

    fun save(context: Context) {
        prefs(context).edit().putString(KEY_FORM, persistJson()).apply()
    }

    companion object {
        /** What `--format` accepts. Guards [libraryUpdateSettingsJson]'s override. */
        val FORMATS = setOf("cbz", "epub", "pdf", "none")

        private const val PREFS = "aio_ui"
        private const val KEY_FORM = "download_form_v1"

        private fun prefs(context: Context) =
            context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        /**
         * Restore, falling back to defaults field by field. A blob written by an
         * older build is missing whatever was added since, and `opt*` with the
         * default as the fallback is what makes that a non-event instead of a
         * crash on first launch after an update.
         */
        fun load(context: Context): DownloadForm {
            val raw = prefs(context).getString(KEY_FORM, null) ?: return DownloadForm()
            return runCatching {
                val o = JSONObject(raw)
                val d = DownloadForm()
                DownloadForm(
                    url = o.optString("url", d.url),
                    format = o.optString("format", d.format),
                    epubLayout = o.optString("epubLayout", d.epubLayout),
                    chapters = o.optString("chapters", d.chapters),
                    language = o.optString("language", d.language),
                    komikku = o.optBoolean("komikku", d.komikku),
                    multiSource = o.optBoolean("multiSource", d.multiSource),
                    multiSourceLazy = o.optBoolean("multiSourceLazy", d.multiSourceLazy),
                    multiSourceQualityMin = o.optDouble(
                        "multiSourceQualityMin",
                        d.multiSourceQualityMin.toDouble(),
                    ).toFloat(),
                    quality = o.optInt("quality", d.quality),
                    scaling = o.optInt("scaling", d.scaling),
                    width = o.optString("width", d.width),
                    noPartials = o.optBoolean("noPartials", d.noPartials),
                    keepChapters = o.optBoolean("keepChapters", d.keepChapters),
                    noFinalFile = o.optBoolean("noFinalFile", d.noFinalFile),
                    keepImages = o.optBoolean("keepImages", d.keepImages),
                    noProcessing = o.optBoolean("noProcessing", d.noProcessing),
                    group = o.optString("group", d.group),
                    excludeGroup = o.optString("excludeGroup", d.excludeGroup),
                    mtl = o.optString("mtl", d.mtl),
                    webtoonRecompress = o.optBoolean("webtoonRecompress", d.webtoonRecompress),
                    webtoonRecompressQuality = o.optInt(
                        "webtoonRecompressQuality",
                        d.webtoonRecompressQuality,
                    ),
                    imageWorkers = o.optInt("imageWorkers", d.imageWorkers),
                    httpTimeout = o.optInt("httpTimeout", d.httpTimeout),
                    httpMaxRetries = o.optInt("httpMaxRetries", d.httpMaxRetries),
                    missedRetries = o.optInt("missedRetries", d.missedRetries),
                    noRetryMissedChapters = o.optBoolean(
                        "noRetryMissedChapters",
                        d.noRetryMissedChapters,
                    ),
                    cookies = o.optString("cookies", d.cookies),
                )
            }.getOrElse { DownloadForm() }
        }
    }
}

/**
 * Language codes forwarded straight to `--language`. Mirrors
 * `UI-source/src/lib/constants.js:LANGUAGES` — the values must match what the
 * Python side accepts, so add here and there together.
 */
val LANGUAGES: List<Pair<String, String>> = listOf(
    "en" to "English",
    "ja" to "Japanese",
    "ko" to "Korean",
    "zh" to "Chinese",
    "es" to "Spanish",
    "fr" to "French",
    "pt-br" to "Portuguese (BR)",
    "de" to "German",
    "it" to "Italian",
    "ru" to "Russian",
    "ar" to "Arabic",
    "tr" to "Turkish",
)

// RESOURCE_LEVELS moved to AppSettings.kt in M7, together with the two fields it
// labels — the limits are a device policy, not a property of one download.
