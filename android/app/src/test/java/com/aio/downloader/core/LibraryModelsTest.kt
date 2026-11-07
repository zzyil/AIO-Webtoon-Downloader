package com.aio.downloader.core

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * Offline coverage for the library's parsing and filtering.
 *
 * WHY THIS IS THE HALF WORTH TESTING ON THE JVM: everything here is a string key
 * crossing from Python. A wrong one does not throw — it yields a default, and
 * the symptom is a grid that renders perfectly with the wrong data in it (no
 * covers, no statuses, a facet sheet with nothing in it). On a device that costs
 * a rebuild-install-scan cycle to notice and gives no clue where to look.
 *
 * The payloads below are shaped like real `aio_android.scan_library` output —
 * `series_meta` nested whole, `anilist_tags` as OBJECTS, chapter counts already
 * flattened by `library_state.to_jsonable`.
 *
 * Cross-file: core/LibraryModels.kt (under test), aio_android.py (produces
 * these payloads), tests/test_android_library.py (the Python half).
 */
class LibraryModelsTest {

    // ── fixtures ──────────────────────────────────────────────────────────

    private fun tag(name: String, category: String = "", spoiler: Boolean = false) =
        JSONObject().apply {
            put("name", name)
            put("category", category)
            put("is_media_spoiler", spoiler)
        }

    private fun entry(
        name: String,
        status: String = "Ongoing",
        site: String = "mangadex",
        genres: List<String> = emptyList(),
        tags: List<JSONObject> = emptyList(),
        spoilerTags: List<JSONObject> = emptyList(),
        chapters: Int = 10,
        size: Long = 1_000,
        cover: String = "",
        url: String = "https://example.test/$name",
        remoteCover: String = "",
    ): JSONObject {
        val meta = JSONObject().apply {
            put("site", site)
            put("anilist_tags", JSONArray(tags))
            put("anilist_spoiler_tags", JSONArray(spoilerTags))
            put("description", "About $name.")
            if (remoteCover.isNotEmpty()) put("cover", remoteCover)
        }
        return JSONObject().apply {
            put("name", name)
            put("folder", "/library/$name")
            put("url", url)
            put("format", "cbz")
            put("language", "en")
            put("status", status)
            put("authors", JSONArray(listOf("Author of $name")))
            put("genres", JSONArray(genres))
            put("chapters", chapters)
            put("latest_chapter", chapters.toString())
            put("next_update", "${chapters + 1}-")
            put("files", 1)
            put("size", size)
            put("cover", cover)
            put("primary_book", "/library/$name/$name.cbz")
            put("series_meta", meta)
        }
    }

    private fun libraryOf(vararg entries: JSONObject): List<LibrarySeries> =
        parseLibrary(JSONArray(entries.toList()).toString())

    /** A real directory, because [firstPageInChapterDir] really lists one. */
    private fun withTempDir(block: (File) -> Unit) {
        val dir = Files.createTempDirectory("aio-chapter").toFile()
        try {
            block(dir)
        } finally {
            dir.deleteRecursively()
        }
    }

    // ── parsing ───────────────────────────────────────────────────────────

    @Test
    fun `parses a scan payload into series`() {
        val series = libraryOf(
            entry("Tekyuu", genres = listOf("Comedy", "Slice of Life"), chapters = 42),
        ).single()

        assertEquals("Tekyuu", series.name)
        assertEquals("/library/Tekyuu", series.folder)
        assertEquals("mangadex", series.site)
        assertEquals("Ongoing", series.status)
        assertEquals(listOf("Author of Tekyuu"), series.authors)
        assertEquals(listOf("Comedy", "Slice of Life"), series.genres)
        assertEquals(42, series.chapters)
        assertEquals("43-", series.nextUpdate)
        assertEquals("About Tekyuu.", series.synopsis)
        assertTrue(series.checkable)
        // Lowercased once at parse time so the search box is a substring test.
        assertEquals("tekyuu", series.searchKey)
    }

    @Test
    fun `a series with no url is not checkable`() {
        val series = libraryOf(entry("Handplaced", url = "")).single()
        assertFalse(series.checkable)
    }

    @Test
    fun `entries missing a name or folder are dropped, not rendered blank`() {
        val nameless = JSONObject().apply { put("folder", "/library/x") }
        val folderless = JSONObject().apply { put("name", "x") }
        assertEquals(0, parseLibrary(JSONArray(listOf(nameless, folderless)).toString()).size)
    }

    @Test
    fun `next_update falls back to all rather than empty`() {
        // An empty spec would reach --chapters as "" and select nothing.
        val bare = JSONObject().apply {
            put("name", "Bare")
            put("folder", "/library/Bare")
        }
        assertEquals("all", parseLibrary(JSONArray(listOf(bare)).toString()).single().nextUpdate)
    }

    @Test
    fun `the remote cover comes from series_meta and must be an http url`() {
        // CoverStore's last resort. A blank field is the signal "there is
        // nothing to fetch", so anything that is not fetchable has to land as
        // blank here rather than reaching the downloader — older writers put a
        // local path in this same key.
        assertEquals(
            "https://cdn.test/cover.jpg",
            libraryOf(entry("Remote", remoteCover = "https://cdn.test/cover.jpg"))
                .single().remoteCover,
        )
        assertEquals(
            "",
            libraryOf(entry("Local", remoteCover = "/library/Local/cover.jpg"))
                .single().remoteCover,
        )
        assertEquals("", libraryOf(entry("None")).single().remoteCover)
        // The on-disk `cover` is a different field and must not be confused
        // with it: that one is a path the scan found, this one is a URL.
        assertEquals(
            "",
            libraryOf(entry("Scanned", cover = "/library/Scanned/cover.jpg"))
                .single().remoteCover,
        )
    }

    @Test
    fun `spoiler tags are re-checked, not trusted to their bucket`() {
        val series = libraryOf(
            entry(
                "Flagged",
                // In the NON-spoiler array but self-declaring — a hand-edited or
                // pre-enrichment .aio_series.json can be shaped this way, and a
                // leaked spoiler is the one failure that costs the reader
                // something irreversible.
                tags = listOf(tag("Twist Ending", "Themes", spoiler = true)),
                spoilerTags = listOf(tag("Death of a Major Character")),
            ),
        ).single()

        assertEquals(2, series.tags.size)
        assertTrue(series.tags.all { it.spoiler })
    }

    // ── facets ────────────────────────────────────────────────────────────

    @Test
    fun `facet counts are per-series, not per-mention`() {
        val duplicated = entry("Twice", genres = listOf("Action", "action", "ACTION"))
        val index = buildFacetIndex(libraryOf(duplicated))
        val action = index.getValue(FacetGroup.Genres).single { it.key == "action" }
        assertEquals(1, action.count)
    }

    @Test
    fun `facet label is the library's most common casing`() {
        val index = buildFacetIndex(
            libraryOf(
                entry("A", genres = listOf("Slice of Life")),
                entry("B", genres = listOf("Slice of Life")),
                entry("C", genres = listOf("slice of life")),
            ),
        )
        val value = index.getValue(FacetGroup.Genres).single()
        assertEquals("slice of life", value.key)
        assertEquals("Slice of Life", value.label)
        assertEquals(3, value.count)
    }

    @Test
    fun `spoiler flag is sticky across series`() {
        val index = buildFacetIndex(
            libraryOf(
                entry("A", tags = listOf(tag("Reveal"))),
                entry("B", spoilerTags = listOf(tag("Reveal"))),
            ),
        )
        assertTrue(index.getValue(FacetGroup.Tags).single { it.key == "reveal" }.spoiler)
    }

    @Test
    fun `facet search haystack folds in the category`() {
        val index = buildFacetIndex(
            libraryOf(entry("A", tags = listOf(tag("Time Skip", "Narrative")))),
        )
        assertEquals("time skip narrative", index.getValue(FacetGroup.Tags).single().search)
    }

    // ── filtering ─────────────────────────────────────────────────────────

    private val shonen = libraryOf(
        entry("Alpha", status = "Ongoing", site = "mangadex", genres = listOf("Action", "Comedy")),
        entry("Beta", status = "Completed", site = "asura", genres = listOf("Action")),
        entry("Gamma", status = "Ongoing", site = "asura", genres = listOf("Comedy")),
    )

    @Test
    fun `no selection matches everything`() {
        val filters = LibraryFilters()
        assertTrue(filters.isEmpty)
        assertTrue(shonen.all(filters::matches))
    }

    @Test
    fun `within a group the default is any`() {
        val filters = LibraryFilters()
            .toggle(FacetGroup.Genres, "action")
            .toggle(FacetGroup.Genres, "comedy")
        assertEquals(3, shonen.count(filters::matches))
    }

    @Test
    fun `match-all requires every selected genre`() {
        val filters = LibraryFilters(matchAll = true)
            .toggle(FacetGroup.Genres, "action")
            .toggle(FacetGroup.Genres, "comedy")
        assertEquals(listOf("Alpha"), shonen.filter(filters::matches).map { it.name })
    }

    @Test
    fun `single-valued groups stay OR even under match-all`() {
        // A series has exactly one status, so ANDing two could never match —
        // applying match-all there would silently empty the grid.
        val filters = LibraryFilters(matchAll = true)
            .toggle(FacetGroup.Status, "ongoing")
            .toggle(FacetGroup.Status, "completed")
        assertEquals(3, shonen.count(filters::matches))
    }

    @Test
    fun `groups AND with each other`() {
        val filters = LibraryFilters()
            .toggle(FacetGroup.Genres, "action")
            .toggle(FacetGroup.Sources, "asura")
        assertEquals(listOf("Beta"), shonen.filter(filters::matches).map { it.name })
    }

    @Test
    fun `toggle removes and add never does`() {
        val once = LibraryFilters().toggle(FacetGroup.Genres, "action")
        assertEquals(0, once.toggle(FacetGroup.Genres, "action").activeCount)
        assertEquals(1, once.add(FacetGroup.Genres, "action").activeCount)
        assertEquals(0, once.cleared().activeCount)
    }

    @Test
    fun `clearing keeps the spoiler gate open once opened`() {
        // Re-hiding tags the user deliberately revealed, as a side effect of
        // clearing an unrelated genre filter, would read as a bug.
        val filters = LibraryFilters(showSpoilerTags = true).toggle(FacetGroup.Genres, "action")
        assertTrue(filters.cleared().showSpoilerTags)
    }

    // ── sorting ───────────────────────────────────────────────────────────

    @Test
    fun `natural compare is numeric-aware`() {
        val names = listOf("Vol 10", "Vol 2", "Vol 100", "Vol 1")
        assertEquals(
            listOf("Vol 1", "Vol 2", "Vol 10", "Vol 100"),
            names.sortedWith(::naturalCompare),
        )
    }

    @Test
    fun `natural compare ignores case and leading zeros`() {
        assertEquals(0, naturalCompare("chapter 07", "Chapter 7"))
        assertTrue(naturalCompare("apple", "Banana") < 0)
    }

    @Test
    fun `sorts by title, size and date`() {
        val list = libraryOf(
            entry("Beta", size = 30),
            entry("Alpha", size = 10),
            entry("Gamma", size = 20),
        )
        assertEquals(
            listOf("Alpha", "Beta", "Gamma"),
            list.sortedForLibrary(LibrarySort.TitleAsc).map { it.name },
        )
        assertEquals(
            listOf("Gamma", "Beta", "Alpha"),
            list.sortedForLibrary(LibrarySort.TitleDesc).map { it.name },
        )
        assertEquals(
            listOf("Beta", "Gamma", "Alpha"),
            list.sortedForLibrary(LibrarySort.Largest).map { it.name },
        )
        assertEquals(
            listOf("Alpha", "Gamma", "Beta"),
            list.sortedForLibrary(LibrarySort.Smallest).map { it.name },
        )
    }

    // ── update results ────────────────────────────────────────────────────

    @Test
    fun `parses a successful check`() {
        val json = JSONObject().apply {
            put("ok", true)
            put("newChapters", JSONArray(listOf("51", "52")))
            put("range", "51-52")
            put("total", 52)
            put("downloaded", 50)
            put("status", "Releasing")
        }.toString()

        val result = parseUpdateResult("/library/X", json)
        assertTrue(result.ok)
        assertTrue(result.hasUpdates)
        assertEquals(listOf("51", "52"), result.newChapters)
        assertEquals("51-52", result.range)
        assertEquals("", result.errorText)
    }

    @Test
    fun `up to date is ok but has no updates`() {
        val json = JSONObject().apply {
            put("ok", true)
            put("newChapters", JSONArray())
            put("total", 52)
            put("downloaded", 52)
        }.toString()
        val result = parseUpdateResult("/library/X", json)
        assertTrue(result.ok)
        assertFalse(result.hasUpdates)
    }

    @Test
    fun `every python error code has human wording`() {
        // The vocabulary is aio_android's; if a code is added there without a
        // case here it falls through to the generic branch, which is fine —
        // what must not happen is a raw `engine_busy` reaching the screen.
        for (code in listOf("engine_busy", "no_metadata", "invalid_metadata", "no_url")) {
            val json = JSONObject().apply { put("error", code) }.toString()
            val result = parseUpdateResult("/library/X", json)
            assertFalse(result.ok)
            assertFalse("$code leaked its raw code", result.errorText.contains(code))
            assertTrue(result.errorText.isNotBlank())
        }
    }

    @Test
    fun `check_failed surfaces the python message`() {
        val json = JSONObject().apply {
            put("error", "check_failed")
            put("message", "Failed to resolve 'api.mangadex.org'")
        }.toString()
        assertEquals(
            "Failed to resolve 'api.mangadex.org'",
            parseUpdateResult("/library/X", json).errorText,
        )
    }

    // ── files ─────────────────────────────────────────────────────────────

    @Test
    fun `parses the file listing`() {
        val json = JSONObject().apply {
            put(
                "files",
                JSONArray(
                    listOf(
                        JSONObject().apply {
                            put("name", "X Ch 1.cbz")
                            put("path", "/library/X/X Ch 1.cbz")
                            put("size", 2048)
                            put("ext", "cbz")
                        },
                    ),
                ),
            )
            put(
                "chapter_dirs",
                JSONArray(
                    listOf(
                        JSONObject().apply {
                            put("name", "Chapter_2")
                            put("path", "/library/X/Chapter_2")
                            put("images", 18)
                            put("size", 4096)
                        },
                    ),
                ),
            )
        }.toString()

        val files = parseSeriesFiles(json)
        assertFalse(files.isEmpty)
        assertEquals("cbz", files.files.single().ext)
        assertEquals(18, files.chapterDirs.single().images)
    }

    @Test
    fun `an empty listing is empty, not an error`() {
        assertTrue(parseSeriesFiles("{}").isEmpty)
    }

    @Test
    fun `the first page of a chapter folder is natural-sorted, not lexical`() {
        withTempDir { dir ->
            // Written in an order that would make BOTH a lexical sort and
            // "whatever listFiles returns" pick the wrong file.
            listOf("10.jpg", "2.jpg", "1.jpg", "ComicInfo.xml").forEach {
                File(dir, it).writeText("x")
            }
            assertEquals("1.jpg", firstPageInChapterDir(dir.absolutePath)?.name)
        }
    }

    @Test
    fun `zero-padded page names still resolve`() {
        withTempDir { dir ->
            listOf("0002.png", "0001.PNG").forEach { File(dir, it).writeText("x") }
            assertEquals("0001.PNG", firstPageInChapterDir(dir.absolutePath)?.name)
        }
    }

    @Test
    fun `a folder with no images resolves to null rather than a non-page`() {
        // The row's honest "no image pages in it" message depends on this: a
        // ComicInfo.xml handed to a gallery is a dead-end chooser.
        withTempDir { dir ->
            File(dir, "ComicInfo.xml").writeText("x")
            assertNull(firstPageInChapterDir(dir.absolutePath))
        }
        assertNull(firstPageInChapterDir("/nope/not/a/folder"))
    }

    // ── the updates strip ─────────────────────────────────────────────────

    private fun found(vararg chapters: String) = UpdateResult(
        folder = "",
        newChapters = chapters.toList(),
        range = chapters.joinToString(","),
        total = 100,
        downloaded = 100 - chapters.size,
    )

    @Test
    fun `groups split by outcome and sort found by size`() {
        val library = libraryOf(
            entry("Alpha"), entry("Beta"), entry("Gamma"), entry("Delta"), entry("Epsilon"),
        )
        val groups = buildUpdateGroups(
            series = library,
            updates = mapOf(
                "/library/Alpha" to found("1"),
                "/library/Beta" to found("1", "2", "3"),
                "/library/Gamma" to UpdateResult(folder = "", total = 10, downloaded = 10),
                "/library/Delta" to UpdateResult(folder = "", error = "check_failed", message = "boom"),
            ),
            checking = setOf("/library/Epsilon"),
            sweepRunning = true,
        )

        assertEquals(listOf("Beta", "Alpha"), groups.found.map { it.name })
        assertEquals(listOf("Gamma"), groups.upToDate.map { it.name })
        assertEquals(listOf("Delta"), groups.failed.map { it.name })
        assertEquals(listOf("Epsilon"), groups.checking.map { it.name })
        assertEquals("boom", groups.failed.single().problem)
        assertEquals(3, groups.found.first().newChapters)
        assertFalse(groups.isEmpty)
    }

    @Test
    fun `a check in flight outranks the result it is replacing`() {
        // Otherwise a re-check of a series that already had updates would keep
        // offering a Download button for a range that is being recomputed.
        val groups = buildUpdateGroups(
            series = libraryOf(entry("Alpha")),
            updates = mapOf("/library/Alpha" to found("7")),
            checking = setOf("/library/Alpha"),
            sweepRunning = true,
        )
        assertTrue(groups.found.isEmpty())
        assertEquals(UpdateRowState.Checking, groups.checking.single().state)
    }

    @Test
    fun `waiting counts only checkable series, and only during a sweep`() {
        val library = libraryOf(entry("Alpha"), entry("Beta"), entry("Handplaced", url = ""))
        val mid = buildUpdateGroups(library, emptyMap(), setOf("/library/Alpha"), true)
        // Alpha is in flight, Beta is waiting, Handplaced has no URL to check.
        assertEquals(1, mid.waiting)

        // Outside a sweep "not checked" is just the resting state.
        assertEquals(0, buildUpdateGroups(library, emptyMap(), emptySet(), false).waiting)
    }

    @Test
    fun `no state at all is empty, which is what hides the strip`() {
        val groups = buildUpdateGroups(libraryOf(entry("Alpha")), emptyMap(), emptySet(), false)
        assertTrue(groups.isEmpty)
        assertEquals(0, groups.waiting)
    }

    @Test
    fun `a result for a folder no longer in the library is dropped`() {
        // A delete splices the series out of the list but the update map is
        // keyed separately; a stale row would tap through to nothing.
        val groups = buildUpdateGroups(
            series = libraryOf(entry("Alpha")),
            updates = mapOf("/library/Deleted" to found("1")),
            checking = emptySet(),
            sweepRunning = false,
        )
        assertTrue(groups.isEmpty)
    }

    // ── formatting ────────────────────────────────────────────────────────

    @Test
    fun `formats sizes like the desktop`() {
        assertEquals("0 B", formatSize(0))
        assertEquals("512 B", formatSize(512))
        assertEquals("1.0 KB", formatSize(1024))
        assertEquals("1.0 MB", formatSize(1_048_576))
        assertEquals("1.50 GB", formatSize(1_610_612_736))
    }

    @Test
    fun `initials take the first letters of the first two words`() {
        assertEquals("AS", initialsOf("Anne Shirley"))
        assertEquals("TB", initialsOf("The Beginning After The End"))
        assertEquals("SL", initialsOf("solo-leveling"))
        assertEquals("?", initialsOf("   "))
    }

    // ── the update download's settings ────────────────────────────────────

    @Test
    fun `library update lets the series override format, language and site`() {
        val form = DownloadForm(format = "cbz", language = "en", chapters = "all")
        val settings = JSONObject(
            form.libraryUpdateSettingsJson(
                url = "https://example.test/x",
                chapters = "51-52",
                seriesFormat = "pdf",
                seriesLanguage = "ja",
                site = "mangadex",
            ),
        )

        assertEquals("51-52", settings.getString("chapters"))
        assertEquals("pdf", settings.getString("format"))
        assertEquals("ja", settings.getString("language"))
        assertEquals("mangadex", settings.getString("site"))
        assertEquals("https://example.test/x", settings.getString("url"))
    }

    @Test
    fun `an unknown series format does not override the users choice`() {
        // library_state writes "?" when no format was recorded, and a settings
        // blob from an older build can carry anything. `--format ?` is a hard
        // argparse error, so it must never reach the command line.
        val settings = JSONObject(
            DownloadForm(format = "cbz").libraryUpdateSettingsJson(
                url = "u", chapters = "1", seriesFormat = "?", seriesLanguage = "", site = "",
            ),
        )
        assertEquals("cbz", settings.getString("format"))
        assertFalse(settings.has("site"))
    }

    @Test
    fun `library update forces lazy multi-source over a global opt-out`() {
        // An update is a one-or-two chapter delta; eager cross-site discovery
        // costs 30-80s before the first byte. build_argv emits
        // --multi-source-lazy unless the key is an explicit false, so removing
        // it is what "force on" means here.
        val optedOut = DownloadForm(multiSource = true, multiSourceLazy = false)
        assertTrue(JSONObject(optedOut.toSettingsJson("u")).has("multiSourceLazy"))

        val settings = JSONObject(
            optedOut.libraryUpdateSettingsJson(
                url = "u", chapters = "51", seriesFormat = "cbz", seriesLanguage = "en", site = "",
            ),
        )
        assertTrue(settings.getBoolean("multiSource"))
        assertFalse(settings.has("multiSourceLazy"))
        assertNull(settings.opt("multiSourceLazy"))
    }
}
