package com.aio.downloader.core

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * The verdict on a candidate library folder.
 *
 * [ok] is the only thing the Settings screen is allowed to act on. A granted
 * MANAGE_EXTERNAL_STORAGE is not the same as a writable directory: the grant can
 * be revoked from system settings at any moment, some OEM storage managers
 * revoke it on their own, and under scoped storage the permission bits and what
 * the sandbox actually permits routinely disagree. So the answer comes from
 * `aio_android.probe_library_root`, which decides it by WRITING a file — the
 * same way the downloader will.
 */
data class StorageProbe(
    val ok: Boolean,
    val path: String,
    val freeBytes: Long,
    /** Series already in the folder — how a user tells "the right one" from "an empty one". */
    val seriesCount: Int,
    /** Empty when [ok]; otherwise something the user can act on. */
    val message: String,
)

/**
 * Probe [path] as a library root. Never throws — a failure is a verdict.
 *
 * Runs the check in PYTHON rather than with `File.canWrite()` because Python is
 * the process that will do the writing, and its answer is the one that counts.
 * Kotlin's own pre-flight in [Aio.resolveLibraryDir] is the weaker fallback for
 * startup, where there is nobody to ask.
 */
suspend fun probeLibraryRoot(context: Context, path: String): StorageProbe =
    withContext(Dispatchers.IO) {
        val raw = runCatching {
            Aio.module(context.applicationContext)
                .callAttr("probe_library_root", path).toString()
        }.getOrElse {
            return@withContext StorageProbe(
                ok = false,
                path = path,
                freeBytes = 0,
                seriesCount = 0,
                // Chaquopy surfaces the Python traceback as the message, which
                // for this call is usually the OS error verbatim.
                message = it.message?.take(200) ?: "Couldn't check that folder",
            )
        }

        val o = runCatching { JSONObject(raw) }.getOrNull()
            ?: return@withContext StorageProbe(false, path, 0, 0, "Couldn't check that folder")

        val error = o.optString("error")
        if (error.isNotBlank()) {
            return@withContext StorageProbe(
                ok = false,
                path = path,
                freeBytes = 0,
                seriesCount = 0,
                message = storageProbeMessage(error, o.optString("message")),
            )
        }

        StorageProbe(
            ok = true,
            path = o.optString("path", path),
            freeBytes = o.optLong("freeBytes"),
            seriesCount = o.optInt("seriesCount"),
            message = "",
        )
    }

/**
 * `probe_library_root`'s error vocabulary as prose.
 *
 * Every one of these is reachable by an ordinary user action, so none of them
 * may surface as a code. `not_writable` in particular is the shape a revoked
 * all-files grant takes, and saying so is the difference between a fixable
 * problem and an inexplicable one.
 */
internal fun storageProbeMessage(code: String, detail: String): String = when (code) {
    "empty_path" -> "Type a folder path first"
    "cannot_create" -> "That folder can't be created. Check the path, and that all-files " +
        "access is still granted."
    "not_a_directory" -> "There's a file at that path, not a folder"
    "not_writable" -> "The folder exists but can't be written to — usually all-files access " +
        "being revoked, or a read-only SD card."
    else -> detail.ifBlank { "Couldn't use that folder ($code)" }
}
