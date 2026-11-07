package com.aio.downloader.core

import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * The metadata embedded INSIDE a downloaded book — ComicInfo.xml in a CBZ, the
 * OPF in an EPUB, the document info dictionary in a PDF.
 *
 * WHY EDITING IT MATTERS ON A PHONE: this is what Komikku and Mihon read. A
 * series whose site listed the author as "Great H" (asura does this) or whose
 * title carries a scanlation suffix shows up in the reader exactly that way, and
 * the reader has no way to fix it — the fields live in the archive.
 *
 * ── THE WRITE IS A REWRITE ─────────────────────────────────────────────────
 * `metadata_editor.update_metadata` rebuilds the whole archive member by member
 * and moves it over the original. That is why the Python side refuses while a
 * download holds the engine, and why [MetadataEdit.applyToAll] sends every path
 * in ONE call — per-file calls would let a download start midway and leave a
 * series half-edited.
 *
 * Cross-file: aio_android.read_book_metadata / write_book_metadata,
 * metadata_editor.py (shared with the desktop, which reaches it through
 * metadata_cli.py), ui/screens/SeriesDetailScreen.kt.
 */
data class BookMetadata(
    val title: String = "",
    val writers: String = "",
    val pencillers: String = "",
    val genres: String = "",
    val publisher: String = "",
    val synopsis: String = "",
) {
    fun toJson(): String = JSONObject().apply {
        put("title", title)
        put("writers", writers)
        put("pencillers", pencillers)
        put("genres", genres)
        put("publisher", publisher)
        put("synopsis", synopsis)
    }.toString()
}

/** What a save did. [failed] is per-file, so one bad archive names itself. */
data class MetadataWriteResult(
    val written: Int,
    val failed: List<String>,
    /** Set when NOTHING was attempted — the whole call was refused. */
    val error: String? = null,
)

object MetadataRepository {

    private const val TAG = "AioMetadata"

    /** Extensions `metadata_editor` can actually route. */
    val EDITABLE_EXTS = setOf("cbz", "epub", "pdf")

    /**
     * Read one book's metadata, or null when it cannot be read.
     *
     * Null rather than an empty [BookMetadata]: "this archive has no title" and
     * "we could not open this archive" must not both render as an empty form
     * the user then saves over the top of.
     */
    suspend fun read(context: Context, path: String): BookMetadata? =
        withContext(Dispatchers.IO) {
            val raw = runCatching {
                Aio.module(context.applicationContext)
                    .callAttr("read_book_metadata", path).toString()
            }.getOrElse {
                Log.w(TAG, "read_book_metadata failed for $path", it)
                return@withContext null
            }

            val o = runCatching { JSONObject(raw) }.getOrNull() ?: return@withContext null
            if (o.optString("error").isNotBlank()) return@withContext null
            val m = o.optJSONObject("metadata") ?: return@withContext null
            BookMetadata(
                title = m.optString("title"),
                writers = m.optString("writers"),
                pencillers = m.optString("pencillers"),
                genres = m.optString("genres"),
                publisher = m.optString("publisher"),
                synopsis = m.optString("synopsis"),
            )
        }

    /**
     * Write [metadata] into every path in [paths].
     *
     * ONE call for the whole batch — see the class header. The Python side
     * reports per-file failures rather than aborting, because with 300 chapter
     * archives stopping at the first bad one leaves the user worse off than
     * skipping it.
     */
    suspend fun write(
        context: Context,
        paths: List<String>,
        metadata: BookMetadata,
    ): MetadataWriteResult = withContext(Dispatchers.IO) {
        if (paths.isEmpty()) return@withContext MetadataWriteResult(0, emptyList(), "no_paths")

        val raw = runCatching {
            Aio.module(context.applicationContext).callAttr(
                "write_book_metadata",
                JSONArray(paths).toString(),
                metadata.toJson(),
            ).toString()
        }.getOrElse {
            Log.e(TAG, "write_book_metadata failed", it)
            return@withContext MetadataWriteResult(
                0,
                emptyList(),
                it.message?.take(160) ?: "write_failed",
            )
        }

        val o = runCatching { JSONObject(raw) }.getOrNull()
            ?: return@withContext MetadataWriteResult(0, emptyList(), "bad_response")

        val error = o.optString("error")
        if (error.isNotBlank()) return@withContext MetadataWriteResult(0, emptyList(), error)

        val failures = o.optJSONArray("failed")
        val names = buildList {
            for (i in 0 until (failures?.length() ?: 0)) {
                val entry = failures?.optJSONObject(i) ?: continue
                add(entry.optString("path").substringAfterLast('/').ifBlank { "a file" })
            }
        }
        MetadataWriteResult(written = o.optInt("written"), failed = names)
    }
}

/**
 * `write_book_metadata`'s refusal codes as prose. `engine_busy` is the one a
 * user will actually meet — the app shares one Python interpreter between
 * downloads and everything else, and a download is writing into this same
 * library.
 */
fun metadataErrorMessage(code: String): String = when (code) {
    "engine_busy" -> "A download is running. Metadata can't be rewritten while the " +
        "downloader is writing to the library — try again when it finishes."
    "no_paths", "no_fields" -> "Nothing to save"
    "unsupported_format" -> "That file type has no metadata to edit"
    "outside_library" -> "That file isn't inside the library"
    "not_found" -> "That file is gone"
    "not_configured" -> "The library isn't set up yet"
    "unavailable" -> "The metadata editor isn't available in this build"
    else -> "Couldn't save ($code)"
}
