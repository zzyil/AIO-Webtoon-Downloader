package com.aio.downloader.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * The library's data shapes, and the parse from `aio_android.scan_library`'s
 * JSON into them.
 *
 * ── WHERE THE DATA COMES FROM ──────────────────────────────────────────────
 * `library_state.scan_library` (pure stdlib, shared with the desktop) does the
 * whole scan: series metadata, chapter numbers, cover discovery, the
 * `next_update` range. Nothing here re-derives any of that — it parses. The one
 * field Kotlin adds is [LibrarySeries.modifiedAt], from the folder's own mtime,
 * because the scan does not report one and the "Newest first" sort needs it.
 *
 * ── FACETS ARE COMPUTED AT PARSE TIME, ON PURPOSE ──────────────────────────
 * [LibrarySeries.facets] is built once per scan so filtering is a set lookup
 * per series rather than a re-walk of every genre and tag array on every
 * keystroke. The desktop reaches the same conclusion the same way (LibraryTab's
 * `facetIndex` memo); at 200 series with ~15 tags each, doing it per keystroke
 * is ~3000 string lowercasings per frame.
 *
 * Cross-file: aio_android.py (scan_library / series_files / check_series_updates
 * produce every payload parsed here), LibraryRepository.kt (the caller),
 * UI-source/src/components/LibraryTab.jsx (the behaviour being ported).
 */

// ── facets ─────────────────────────────────────────────────────────────────

/**
 * The four things a library can be filtered by. Mirrors LibraryTab.jsx's
 * FACET_KEYS one-for-one so the two apps filter on the same axes.
 *
 * [singleValued] groups (a series has exactly one status and one source) always
 * OR their selection: "match all" with two statuses selected could never match
 * anything, so applying it there would silently empty the grid. Standard
 * faceted-search contract, and the same carve-out the desktop makes.
 */
enum class FacetGroup(val label: String, val singleValued: Boolean) {
    Genres("Genres", false),
    Tags("Tags", false),
    Status("Status", true),
    Sources("Source", true),
}

/** One selectable value, with its library-wide count. */
data class FacetValue(
    val key: String,
    val label: String,
    val count: Int,
    val category: String = "",
    val spoiler: Boolean = false,
) {
    /** Precomputed haystack for the sheet's filter-the-filters field. */
    val search: String = (if (category.isEmpty()) label else "$label $category").lowercase()
}

/**
 * Normalize a raw genre/tag/status/site string into its facet key.
 *
 * MUST stay identical wherever a key is derived — the detail screen's chips
 * build keys from raw metadata strings and would otherwise apply a filter that
 * matches nothing.
 */
fun facetKey(raw: String?): String = raw?.trim()?.lowercase().orEmpty()

data class LibraryFilters(
    val selection: Map<FacetGroup, Set<String>> = emptyMap(),
    /** Within a multi-valued group: any (OR) or all (AND). */
    val matchAll: Boolean = false,
    val showSpoilerTags: Boolean = false,
) {
    val activeCount: Int get() = selection.values.sumOf { it.size }
    val isEmpty: Boolean get() = activeCount == 0

    fun selected(group: FacetGroup): Set<String> = selection[group].orEmpty()

    fun toggle(group: FacetGroup, key: String): LibraryFilters {
        val current = selected(group)
        val next = if (key in current) current - key else current + key
        return copy(selection = selection + (group to next))
    }

    /** Additive only — a chip tapped on the detail screen means "more of this". */
    fun add(group: FacetGroup, key: String): LibraryFilters =
        if (key.isEmpty() || key in selected(group)) this
        else copy(selection = selection + (group to (selected(group) + key)))

    fun cleared(): LibraryFilters = LibraryFilters(showSpoilerTags = showSpoilerTags)

    fun matches(series: LibrarySeries): Boolean {
        for (group in FacetGroup.entries) {
            val wanted = selected(group)
            if (wanted.isEmpty()) continue
            val have = series.facets[group].orEmpty()
            if (matchAll && !group.singleValued) {
                if (!have.containsAll(wanted)) return false
            } else {
                if (wanted.none { it in have }) return false
            }
        }
        return true
    }
}

// ── series ─────────────────────────────────────────────────────────────────

/** An AniList tag. Objects, not strings — they carry a category and a spoiler flag. */
data class SeriesTag(val name: String, val category: String, val spoiler: Boolean)

data class LibrarySeries(
    val name: String,
    val folder: String,
    val url: String,
    val site: String,
    val format: String,
    val language: String,
    val status: String,
    val authors: List<String>,
    val genres: List<String>,
    val tags: List<SeriesTag>,
    val synopsis: String,
    /** Distinct chapter numbers found on disk plus those the metadata records. */
    val chapters: Int,
    val latestChapter: String,
    /** `--chapters` spec that would fetch everything after the newest one held. */
    val nextUpdate: String,
    val files: Int,
    val size: Long,
    /**
     * An on-disk cover path, or "" when the only cover lives inside an archive.
     * Blank is NOT "no cover" — see CoverStore, which resolves the rest lazily.
     */
    val cover: String,
    /**
     * The `cover` URL recorded in `.aio_series.json` — AniList's normalized
     * cover on a confident match, the site's own otherwise (grep
     * `anilist_cover` in sites/external_metadata.py for which wins).
     *
     * The LAST resort, not the first: it is the only cover for a series whose
     * archives carry no usable image, which is the case the desktop covers by
     * rendering the remote URL straight into the grid. CoverStore downloads it
     * into the cover cache instead, so everything Coil is ever handed stays a
     * local file. "" unless it parsed as an http(s) URL.
     */
    val remoteCover: String = "",
    val primaryBook: String,
    val modifiedAt: Long,
    val facets: Map<FacetGroup, Set<String>>,
) {
    /** Update checking needs a source URL; a hand-placed folder has none. */
    val checkable: Boolean get() = url.isNotBlank()

    /** Lowercased once per scan for the search box. */
    val searchKey: String = name.lowercase()
}

// ── update checks ──────────────────────────────────────────────────────────

/**
 * One `check_series_updates` result.
 *
 * [error] carries aio_android's vocabulary verbatim (`no_metadata`, `no_url`,
 * `engine_busy`, `check_failed`, …) so [errorText] is the only place that
 * decides how each reads to a human.
 */
data class UpdateResult(
    val folder: String,
    val newChapters: List<String> = emptyList(),
    val range: String = "",
    val total: Int = 0,
    val downloaded: Int = 0,
    val status: String = "",
    val error: String? = null,
    val message: String = "",
    val checkedAt: Long = System.currentTimeMillis(),
) {
    val ok: Boolean get() = error == null
    val hasUpdates: Boolean get() = ok && newChapters.isNotEmpty()

    val errorText: String
        get() = when (error) {
            null -> ""
            "engine_busy" -> "A download is running — try again when it finishes"
            "no_metadata" -> "No saved metadata for this series"
            "invalid_metadata" -> "The saved metadata is unreadable"
            "no_url" -> "No source URL saved, so there's nothing to check against"
            // check_failed carries the Python exception text, which is usually
            // the whole diagnosis (a dead host, a handler that raised).
            else -> message.ifBlank { "Check failed ($error)" }
        }
}

/** Progress of a whole-library sweep. Serial by construction — see the engine lock. */
data class SweepState(
    val running: Boolean,
    val completed: Int,
    val total: Int,
    val current: String = "",
    /** Set when the sweep stopped early; the reason, already human-readable. */
    val stoppedBecause: String = "",
)

// ── files ──────────────────────────────────────────────────────────────────

data class SeriesFileEntry(val name: String, val path: String, val size: Long, val ext: String)

data class ChapterDirEntry(val name: String, val path: String, val images: Int, val size: Long)

data class SeriesFiles(
    val files: List<SeriesFileEntry> = emptyList(),
    val chapterDirs: List<ChapterDirEntry> = emptyList(),
) {
    val isEmpty: Boolean get() = files.isEmpty() && chapterDirs.isEmpty()
}

/**
 * Page extensions a `Chapter_N/` folder can hold, matching
 * `library_state.SUPPORTED_COVER_EXTS` — the same set the scan counts as
 * `ChapterDirEntry.images`, so the count and the pick agree.
 */
private val PAGE_EXTS = setOf(".jpg", ".jpeg", ".png", ".webp", ".gif")

/**
 * Page 1 of a raw chapter folder, or null if it holds no image.
 *
 * WHY IT IS RESOLVED HERE AND NOT IN PYTHON: `series_files` deliberately
 * reports a chapter directory as a NAME + a COUNT (multiplying the payload by
 * the page count is exactly what it avoids), and this is wanted for one folder,
 * once, at the moment a row is tapped. A `listFiles` on that one directory is
 * cheaper than widening the JSON for every series.
 *
 * Sorted with [naturalCompare], which is what makes "page 1" mean the first
 * page rather than `10.jpg` — handlers write both `1.jpg` and `0001.jpg`.
 * Blocking I/O: call it off the main thread.
 *
 * Cross-file: ui/screens/SeriesDetailScreen.kt (grep `openChapterFolder`).
 */
fun firstPageInChapterDir(path: String): File? = runCatching {
    File(path).listFiles()
        ?.filter { it.isFile && PAGE_EXTS.any { ext -> it.name.endsWith(ext, ignoreCase = true) } }
        ?.minWithOrNull { a, b -> naturalCompare(a.name, b.name) }
}.getOrNull()

// ── updates strip ──────────────────────────────────────────────────────────

/**
 * What the updates strip says about one series.
 *
 * The desktop's fifth state, `queued`, is deliberately absent: a sweep here is
 * serial, so "queued" would be every checkable series in the library and the
 * section would be a scroll of nothing happening. The strip reports the waiting
 * COUNT instead — see [UpdateGroups.waiting].
 */
enum class UpdateRowState { Found, Checking, UpToDate, Failed }

/** One row of the strip. Everything needed to draw it, nothing else. */
data class UpdateRow(
    val folder: String,
    val name: String,
    val site: String,
    val state: UpdateRowState,
    val newChapters: Int = 0,
    /** The `--chapters` spec a queue action would pass. Empty unless [Found]. */
    val range: String = "",
    /** Total on the site, for the up-to-date line. */
    val total: Int = 0,
    /** Already-human error wording, from [UpdateResult.errorText]. */
    val problem: String = "",
)

/**
 * The strip's four buckets, in the desktop's own grouping (UpdatesCenter.jsx:
 * FOUND / CHECKING / UP TO DATE / ERRORS).
 */
data class UpdateGroups(
    val found: List<UpdateRow> = emptyList(),
    val checking: List<UpdateRow> = emptyList(),
    val upToDate: List<UpdateRow> = emptyList(),
    val failed: List<UpdateRow> = emptyList(),
    /** Checkable series a running sweep has not reached yet. A count, not rows. */
    val waiting: Int = 0,
) {
    val isEmpty: Boolean
        get() = found.isEmpty() && checking.isEmpty() && upToDate.isEmpty() && failed.isEmpty()
}

/**
 * Fold the scattered per-series update state into the strip's model.
 *
 * This is the consolidation itself: before it, "does this series have updates"
 * lived in three places that each re-derived it (the grid card's `+N` badge,
 * the detail screen's Download button, the sweep banner's found count). One
 * pure function over the three flows LibraryRepository already publishes means
 * they cannot disagree.
 *
 * Ordering mirrors UpdatesCenter.jsx: found by size (the most noticeable first,
 * ties by title), everything else natural-by-title. [sweepRunning] only decides
 * whether unvisited series count as [UpdateGroups.waiting] — outside a sweep,
 * "not checked" is the resting state and reporting it as pending would be
 * noise.
 */
fun buildUpdateGroups(
    series: List<LibrarySeries>,
    updates: Map<String, UpdateResult>,
    checking: Set<String>,
    sweepRunning: Boolean,
): UpdateGroups {
    val found = mutableListOf<UpdateRow>()
    val inFlight = mutableListOf<UpdateRow>()
    val upToDate = mutableListOf<UpdateRow>()
    val failed = mutableListOf<UpdateRow>()
    var waiting = 0

    for (entry in series) {
        val result = updates[entry.folder]
        // A check in flight wins over a stale result: the row should say what
        // is happening now, not what the previous pass concluded.
        if (entry.folder in checking) {
            inFlight += UpdateRow(
                folder = entry.folder,
                name = entry.name,
                site = entry.site,
                state = UpdateRowState.Checking,
            )
            continue
        }
        if (result == null) {
            if (sweepRunning && entry.checkable) waiting++
            continue
        }
        val row = UpdateRow(
            folder = entry.folder,
            name = entry.name,
            site = entry.site,
            state = when {
                !result.ok -> UpdateRowState.Failed
                result.hasUpdates -> UpdateRowState.Found
                else -> UpdateRowState.UpToDate
            },
            newChapters = result.newChapters.size,
            range = if (result.hasUpdates) result.range else "",
            total = result.total,
            problem = result.errorText,
        )
        when (row.state) {
            UpdateRowState.Found -> found += row
            UpdateRowState.UpToDate -> upToDate += row
            UpdateRowState.Failed -> failed += row
            UpdateRowState.Checking -> Unit
        }
    }

    return UpdateGroups(
        found = found.sortedWith { a, b ->
            // Written out rather than compareByDescending().thenBy(): the tie
            // break is naturalCompare, which is a function and not a Comparator,
            // and the file already uses this shape everywhere else.
            val bySize = b.newChapters - a.newChapters
            if (bySize != 0) bySize else naturalCompare(a.name, b.name)
        },
        checking = inFlight.sortedWith { a, b -> naturalCompare(a.name, b.name) },
        upToDate = upToDate.sortedWith { a, b -> naturalCompare(a.name, b.name) },
        failed = failed.sortedWith { a, b -> naturalCompare(a.name, b.name) },
        waiting = waiting,
    )
}

/** True for a string an image loader could actually fetch. */
private fun isHttpUrl(value: String): Boolean =
    value.startsWith("http://", ignoreCase = true) ||
        value.startsWith("https://", ignoreCase = true)

// ── sorting ────────────────────────────────────────────────────────────────

enum class LibrarySort(val label: String) {
    TitleAsc("Title A→Z"),
    TitleDesc("Title Z→A"),
    Newest("Newest first"),
    Oldest("Oldest first"),
    Largest("Largest first"),
    Smallest("Smallest first"),
}

/**
 * Numeric-aware string compare: "Vol 2" before "Vol 10", not after "Vol 100".
 *
 * Port of `UI-source/src/lib/utils.js:naturalCompare`, which gets this from
 * `Intl.Collator(numeric: true)`. Kotlin's `compareTo` is codepoint order, and a
 * library of long-running series sorted that way looks broken.
 */
fun naturalCompare(a: String, b: String): Int {
    var i = 0
    var j = 0
    while (i < a.length && j < b.length) {
        val ca = a[i]
        val cb = b[j]
        if (ca.isDigit() && cb.isDigit()) {
            var ei = i
            while (ei < a.length && a[ei].isDigit()) ei++
            var ej = j
            while (ej < b.length && b[ej].isDigit()) ej++
            // Compared as strings with leading zeros stripped, not parsed: a
            // chapter number can be longer than Long.MAX_VALUE's digit count in
            // pathological folder names, and parsing would throw.
            val na = a.substring(i, ei).trimStart('0').ifEmpty { "0" }
            val nb = b.substring(j, ej).trimStart('0').ifEmpty { "0" }
            if (na.length != nb.length) return na.length - nb.length
            val cmp = na.compareTo(nb)
            if (cmp != 0) return cmp
            i = ei
            j = ej
            continue
        }
        val cmp = ca.lowercaseChar().compareTo(cb.lowercaseChar())
        if (cmp != 0) return cmp
        i++
        j++
    }
    return (a.length - i) - (b.length - j)
}

/**
 * Named `sortedForLibrary`, not `sortedBy`: an overload of a stdlib name that
 * differs only in its parameter type is exactly the kind of thing that resolves
 * to the wrong function three edits from now.
 */
fun List<LibrarySeries>.sortedForLibrary(sort: LibrarySort): List<LibrarySeries> = when (sort) {
    LibrarySort.TitleAsc -> sortedWith { a, b -> naturalCompare(a.name, b.name) }
    LibrarySort.TitleDesc -> sortedWith { a, b -> naturalCompare(b.name, a.name) }
    LibrarySort.Newest -> sortedByDescending { it.modifiedAt }
    LibrarySort.Oldest -> sortedBy { it.modifiedAt }
    LibrarySort.Largest -> sortedByDescending { it.size }
    LibrarySort.Smallest -> sortedBy { it.size }
}

// ── formatting ─────────────────────────────────────────────────────────────

/** Port of LibraryTab.jsx:formatSize — same thresholds, same decimal places. */
fun formatSize(bytes: Long): String = when {
    bytes <= 0 -> "0 B"
    bytes < 1024 -> "$bytes B"
    bytes < 1_048_576 -> "%.1f KB".format(bytes / 1024.0)
    bytes < 1_073_741_824 -> "%.1f MB".format(bytes / 1_048_576.0)
    else -> "%.2f GB".format(bytes / 1_073_741_824.0)
}

/** Two-letter monogram for the no-cover fallback. Port of `utils.js:getInitials`. */
fun initialsOf(title: String): String =
    title.split(Regex("[\\s\\-_]+"))
        .filter { it.isNotEmpty() }
        .take(2)
        .joinToString("") { it.first().uppercase() }
        .ifEmpty { "?" }

// ── parsing ────────────────────────────────────────────────────────────────

private fun JSONArray?.strings(): List<String> {
    if (this == null) return emptyList()
    return (0 until length()).mapNotNull { optString(it).trim().ifBlank { null } }
}

private fun JSONObject.tagList(key: String, spoilerBucket: Boolean): List<SeriesTag> {
    val array = optJSONArray(key) ?: return emptyList()
    return (0 until array.length()).mapNotNull { i ->
        val o = array.optJSONObject(i) ?: return@mapNotNull null
        val name = o.optString("name").trim().ifBlank { return@mapNotNull null }
        SeriesTag(
            name = name,
            category = o.optString("category").trim(),
            // Re-checked rather than trusted to the bucket: the split is done
            // Python-side (external_metadata._split_tags), but a hand-edited or
            // pre-enrichment .aio_series.json can carry either shape, and a
            // spoiler leaking through is the one failure that actually costs
            // the reader something.
            spoiler = spoilerBucket ||
                o.optBoolean("is_media_spoiler") ||
                o.optBoolean("is_general_spoiler"),
        )
    }
}

/** Parse one `scan_library` element. Returns null for an entry with no usable name. */
private fun parseSeries(o: JSONObject): LibrarySeries? {
    val name = o.optString("name").trim().ifBlank { return null }
    val folder = o.optString("folder").trim().ifBlank { return null }
    val meta = o.optJSONObject("series_meta") ?: JSONObject()

    val genres = o.optJSONArray("genres").strings()
    val tags = meta.tagList("anilist_tags", false) + meta.tagList("anilist_spoiler_tags", true)
    val status = o.optString("status").trim()
    val site = meta.optString("site").trim()

    val facets = mapOf(
        FacetGroup.Genres to genres.map(::facetKey).filter { it.isNotEmpty() }.toSet(),
        FacetGroup.Tags to tags.map { facetKey(it.name) }.toSet(),
        FacetGroup.Status to setOfNotNull(facetKey(status).ifEmpty { null }),
        FacetGroup.Sources to setOfNotNull(facetKey(site).ifEmpty { null }),
    )

    return LibrarySeries(
        name = name,
        folder = folder,
        url = o.optString("url").trim(),
        site = site,
        format = o.optString("format").trim(),
        language = o.optString("language").trim(),
        status = status,
        authors = o.optJSONArray("authors").strings(),
        genres = genres,
        tags = tags,
        synopsis = meta.optString("description").ifBlank { meta.optString("synopsis") }.trim(),
        chapters = o.optInt("chapters"),
        latestChapter = o.optString("latest_chapter").trim(),
        nextUpdate = o.optString("next_update").trim().ifBlank { "all" },
        files = o.optInt("files"),
        size = o.optLong("size"),
        cover = o.optString("cover").trim(),
        // Filtered to http(s) HERE rather than at the fetch: `.aio_series.json`
        // is written by many handler paths and an older one can hold a local
        // path or a `data:` blob, neither of which an HTTP GET can do anything
        // with. Blank then means "there is nothing to fetch", which is the only
        // thing CoverStore.fromRemote has to reason about.
        remoteCover = meta.optString("cover").trim().takeIf { isHttpUrl(it) }.orEmpty(),
        primaryBook = o.optString("primary_book").trim(),
        // The scan does not report a timestamp and library_state is shared with
        // the desktop, so this is one stat() per series here rather than a
        // change over there. Folder mtime moves when a chapter lands, which is
        // exactly what "Newest first" should mean.
        modifiedAt = runCatching { File(folder).lastModified() }.getOrDefault(0L),
        facets = facets,
    )
}

fun parseLibrary(json: String): List<LibrarySeries> {
    val array = JSONArray(json)
    return (0 until array.length()).mapNotNull { i ->
        array.optJSONObject(i)?.let(::parseSeries)
    }
}

fun parseUpdateResult(folder: String, json: String): UpdateResult {
    val o = JSONObject(json)
    o.optString("error").takeIf { it.isNotBlank() }?.let { error ->
        return UpdateResult(folder = folder, error = error, message = o.optString("message"))
    }
    return UpdateResult(
        folder = folder,
        newChapters = o.optJSONArray("newChapters").strings(),
        range = o.optString("range"),
        total = o.optInt("total"),
        downloaded = o.optInt("downloaded"),
        status = o.optString("status"),
    )
}

fun parseSeriesFiles(json: String): SeriesFiles {
    val o = JSONObject(json)
    val files = o.optJSONArray("files") ?: JSONArray()
    val dirs = o.optJSONArray("chapter_dirs") ?: JSONArray()
    return SeriesFiles(
        files = (0 until files.length()).mapNotNull { i ->
            val f = files.optJSONObject(i) ?: return@mapNotNull null
            SeriesFileEntry(
                name = f.optString("name"),
                path = f.optString("path"),
                size = f.optLong("size"),
                ext = f.optString("ext"),
            )
        },
        chapterDirs = (0 until dirs.length()).mapNotNull { i ->
            val d = dirs.optJSONObject(i) ?: return@mapNotNull null
            ChapterDirEntry(
                name = d.optString("name"),
                path = d.optString("path"),
                images = d.optInt("images"),
                size = d.optLong("size"),
            )
        },
    )
}

// ── facet index ────────────────────────────────────────────────────────────

/**
 * Library-wide facet vocabulary with counts, ready for the filter sheet.
 *
 * Counts are LIBRARY-WIDE, not contextual — they do not shrink as other facets
 * are selected. Contextual counts would mean rebuilding this on every tap with
 * every number jumping as you go, which is worse to use and would tie the whole
 * index to the current selection. Same call the desktop makes.
 *
 * The display label is the library's MOST COMMON casing of the value, not
 * whichever series happened to be scanned first — sites disagree about whether
 * it is "Slice of Life" or "slice of life".
 */
fun buildFacetIndex(series: List<LibrarySeries>): Map<FacetGroup, List<FacetValue>> {
    class Acc {
        var count = 0
        var category = ""
        var spoiler = false
        val casings = LinkedHashMap<String, Int>()
    }

    val groups = FacetGroup.entries.associateWith { LinkedHashMap<String, Acc>() }

    fun bump(group: FacetGroup, raw: String, category: String = "", spoiler: Boolean = false) {
        val label = raw.trim()
        if (label.isEmpty()) return
        val acc = groups.getValue(group).getOrPut(label.lowercase()) { Acc() }
        if (category.isNotEmpty() && acc.category.isEmpty()) acc.category = category
        // Sticky: a tag flagged as a spoiler by ANY series stays behind the
        // spoiler switch everywhere. Erring the other way would leak it.
        if (spoiler) acc.spoiler = true
        acc.casings[label] = (acc.casings[label] ?: 0) + 1
    }

    for (s in series) {
        // A series listing "Action" twice must count once — dedupe per series,
        // then bump the count from the per-series key set.
        s.genres.forEach { bump(FacetGroup.Genres, it) }
        s.tags.forEach { bump(FacetGroup.Tags, it.name, it.category, it.spoiler) }
        bump(FacetGroup.Status, s.status)
        bump(FacetGroup.Sources, s.site)
        for (group in FacetGroup.entries) {
            for (key in s.facets[group].orEmpty()) {
                groups.getValue(group)[key]?.let { it.count++ }
            }
        }
    }

    return groups.mapValues { (_, map) ->
        map.map { (key, acc) ->
            val label = acc.casings.entries
                .sortedWith(compareByDescending<Map.Entry<String, Int>> { it.value }.thenBy { it.key })
                .first().key
            FacetValue(key, label, acc.count, acc.category, acc.spoiler)
        }.sortedWith(compareByDescending<FacetValue> { it.count }.thenBy { it.label.lowercase() })
    }
}
