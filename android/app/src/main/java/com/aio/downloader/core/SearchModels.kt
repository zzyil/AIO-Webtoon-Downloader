package com.aio.downloader.core

import org.json.JSONArray
import org.json.JSONObject

/**
 * The cross-site search result shapes, and the parse from `aio_android.search`'s
 * JSON into them.
 *
 * ── WHERE THE DATA COMES FROM ──────────────────────────────────────────────
 * `sites/search_orchestrator.py` fans out to every search-capable handler,
 * merges the hits into one candidate per SERIES, and rates each SOURCE. The
 * `--search-json` contract is `{"query", "language", "candidates": [...]}` where
 * each candidate carries a `sources` array — see `SeriesCandidate.to_json`.
 * Nothing here re-derives any of that; it parses.
 *
 * ── THE ONE THING WORTH KNOWING ABOUT THE RATINGS ──────────────────────────
 * A rating is measured only when the source's pages were compared with other
 * sites' copies of the same pages (`sites/match_session.py`, rate_sources):
 * `quality_basis` "matched", or "partial" (compared within a smaller group of
 * sites). Every other basis — "solo", "unmatched", "pending", "unreachable",
 * "seed" (not probed) — has no comparable number, and shows a reason label
 * instead of one (user decision 2026-10-21, plan
 * sync-temp/plans/image-ranker-rework.md "P4 design"): a seed prior next to
 * measured ratings reads as a measurement. [SearchSource.measuredRating] and
 * [SearchSource.qualityLabel] are that rule, the same as the desktop's
 * UI-source/src/lib/qualityDisplay.js (grep measuredRating there).
 *
 * Cross-file: aio_android.search (the payload), SearchRepository.kt (the
 * caller), SearchSourceCard.jsx + lib/qualityDisplay.js (the behaviour ported).
 */

/** One site's copy of a series. */
data class SearchSource(
    val site: String,
    val url: String,
    val title: String,
    val cover: String,
    /** 0..1 query-vs-title similarity. rapidfuzz WRatio; see sites/fuzzy_match. */
    val titleMatch: Double,
    /** 0..1 image quality, or null when nothing was measured. */
    val imgQuality: Double?,
    val seedQuality: Double,
    val compositeScore: Double,
    val qualityBasis: String,
    /** From the site's own listing; null when it does not publish one. */
    val chapterCountHint: Int?,
    /** Counted from a real chapter-list fetch; null when not fetched. */
    val actualChapterCount: Int?,
    val dmcaLikely: Boolean,
    val isOfficial: Boolean,
    /** "original" | "alternate" (a different edition, e.g. fan-colored) | "undecided". */
    val edition: String = "undecided",
    /** This source's pages are in color (`img_quality_metadata.colored`); null = unknown. */
    val colored: Boolean? = null,
    /** Why there is no measured rating (`img_quality_metadata.reason`), when the probe recorded one. */
    val qualityReason: String? = null,
    /** Sites serving byte- or pixel-identical pages (`img_quality_metadata.same_file`). */
    val sameFile: List<String> = emptyList(),
    /** Sites serving the same scan re-encoded (`img_quality_metadata.same_scan`). */
    val sameScan: List<String> = emptyList(),
) {
    /** The rating to show as a number, or null when it was not measured. */
    val measuredRating: Double? get() = imgQuality?.takeIf { qualityBasis in MEASURED_BASES }

    /** Shown in place of the number when [measuredRating] is null. */
    val qualityLabel: String
        get() = when (qualityBasis) {
            "solo", "unmatched" -> "not compared"
            "unreachable" -> "unreachable"
            "pending" -> if (qualityReason?.contains("timeout") == true) "timed out" else "not measured"
            else -> "not measured"
        }

    /** A different edition from most sites': never another download's fallback. */
    val isOtherEdition: Boolean get() = edition == "alternate"

    /** Which way an other-edition source differs, or null. */
    val editionNote: String?
        get() = if (!isOtherEdition) null else when (colored) {
            true -> "colored, where most sites are black-and-white"
            false -> "black-and-white, where most sites are in color"
            null -> "pages differ from most sites' copies"
        }

    /** "Same file as atsu; same scan as mangapill", or null. */
    val sameNote: String?
        get() {
            val parts = buildList {
                if (sameFile.isNotEmpty()) add("same file as ${sameFile.joinToString(", ")}")
                if (sameScan.isNotEmpty()) add("same scan as ${sameScan.joinToString(", ")}")
            }
            return parts.joinToString("; ").takeIf { it.isNotEmpty() }?.replaceFirstChar { it.uppercase() }
        }

    /** What to show as the chapter count, preferring the counted value. */
    val chapterCount: Int? get() = actualChapterCount ?: chapterCountHint

    private companion object {
        /** Bases with a measured rating (match_session.rate_sources). */
        val MEASURED_BASES = setOf("matched", "partial")
    }
}

/** One series, with every site that has it. */
data class SearchCandidate(
    val canonicalTitle: String,
    val canonicalYear: Int?,
    val sources: List<SearchSource>,
) {
    /** The orchestrator ranks sources; the first is its pick. */
    val best: SearchSource? get() = sources.firstOrNull()

    val siteCount: Int get() = sources.size

    /** Highest chapter count any source reports — the useful headline number. */
    val maxChapters: Int? get() = sources.mapNotNull { it.chapterCount }.maxOrNull()

    /** Cover from the first source that has one; sites vary in what they expose. */
    val cover: String? get() = sources.firstOrNull { it.cover.isNotBlank() }?.cover
}

/**
 * A whole search. [error] non-null means the run failed; an empty [candidates]
 * with a null error is a legitimate "nothing found", and the two must stay
 * distinguishable — collapsing them would present a failed search as an empty
 * one, which is the bug the desktop's searcher.js explicitly guards against.
 */
data class SearchOutcome(
    val query: String,
    val candidates: List<SearchCandidate>,
    val error: String? = null,
) {
    val isEmpty: Boolean get() = error == null && candidates.isEmpty()
}

// ── parsing ────────────────────────────────────────────────────────────────

private fun JSONObject.intOrNull(key: String): Int? =
    if (isNull(key)) null else optInt(key).takeIf { has(key) }

private fun JSONObject.doubleOrNull(key: String): Double? =
    if (isNull(key)) null else optDouble(key).takeIf { !it.isNaN() }

private fun JSONArray?.strings(): List<String> =
    if (this == null) emptyList() else (0 until length()).mapNotNull { optString(it).takeIf { s -> s.isNotBlank() } }

private fun parseSource(o: JSONObject): SearchSource? {
    val url = o.optString("url").orEmpty()
    // A source with no URL cannot be downloaded, so it has no reason to be on
    // screen — this is the same "drop, do not crash" rule parseSeries uses.
    if (url.isBlank()) return null
    // match_session.quality_metadata; absent for sources the search did not probe
    val meta = o.optJSONObject("img_quality_metadata")
    return SearchSource(
        site = o.optString("site").orEmpty(),
        url = url,
        title = o.optString("title").orEmpty(),
        cover = o.optString("cover").orEmpty(),
        titleMatch = o.optDouble("title_match", 0.0).let { if (it.isNaN()) 0.0 else it },
        imgQuality = o.doubleOrNull("img_quality_score"),
        seedQuality = o.optDouble("seed_quality", 0.0).let { if (it.isNaN()) 0.0 else it },
        compositeScore = o.optDouble("composite_score", 0.0).let { if (it.isNaN()) 0.0 else it },
        qualityBasis = o.optString("quality_basis").orEmpty(),
        chapterCountHint = o.intOrNull("chapter_count_hint"),
        actualChapterCount = o.intOrNull("actual_chapter_count"),
        dmcaLikely = o.optBoolean("dmca_likely", false),
        isOfficial = o.optBoolean("is_official", false),
        // optString turns a JSON null into "null"
        edition = o.optString("edition").takeIf { it.isNotBlank() && it != "null" } ?: "undecided",
        colored = meta?.takeIf { it.has("colored") && !it.isNull("colored") }?.optBoolean("colored"),
        qualityReason = meta?.takeIf { !it.isNull("reason") }?.optString("reason")?.takeIf { it.isNotBlank() },
        sameFile = meta?.optJSONArray("same_file").strings(),
        sameScan = meta?.optJSONArray("same_scan").strings(),
    )
}

private fun parseCandidate(o: JSONObject): SearchCandidate? {
    val sources = buildList {
        val arr = o.optJSONArray("sources") ?: JSONArray()
        for (i in 0 until arr.length()) {
            arr.optJSONObject(i)?.let { parseSource(it) }?.let { add(it) }
        }
    }
    if (sources.isEmpty()) return null
    return SearchCandidate(
        canonicalTitle = o.optString("canonical_title").ifBlank { sources.first().title },
        canonicalYear = o.intOrNull("canonical_year"),
        sources = sources,
    )
}

/**
 * Parse one `aio_android.search` payload.
 *
 * Never throws: a malformed payload becomes a [SearchOutcome] carrying an error
 * string, because the alternative on a device is a crash three layers from
 * anything a stack trace would point at.
 */
fun parseSearchOutcome(query: String, json: String): SearchOutcome {
    val root = try {
        JSONObject(json)
    } catch (e: Exception) {
        return SearchOutcome(query, emptyList(), "Search returned unreadable output.")
    }
    root.optString("error").takeIf { it.isNotBlank() }?.let { code ->
        return SearchOutcome(query, emptyList(), searchErrorMessage(code, root.optString("detail")))
    }
    val arr = root.optJSONArray("candidates") ?: JSONArray()
    val candidates = buildList {
        for (i in 0 until arr.length()) {
            arr.optJSONObject(i)?.let { parseCandidate(it) }?.let { add(it) }
        }
    }
    return SearchOutcome(root.optString("query").ifBlank { query }, candidates)
}

/**
 * Turn `aio_android.search`'s error codes into something a person can act on.
 * Raw codes must never reach the UI — "engine_busy" is not an instruction.
 */
fun searchErrorMessage(code: String, detail: String? = null): String = when (code) {
    "engine_busy" ->
        "A download is running. Search shares the engine with downloads, so it " +
            "has to wait until that finishes."
    "no_query" -> "Enter something to search for."
    "no_search_payload" ->
        "The search finished without returning results. Check the Logs tab for " +
            "what went wrong."
    "search_failed" ->
        if (detail.isNullOrBlank()) "The search failed." else "The search failed: $detail"
    else -> if (detail.isNullOrBlank()) "The search failed ($code)." else detail
}
