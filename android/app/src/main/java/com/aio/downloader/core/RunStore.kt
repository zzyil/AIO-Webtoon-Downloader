package com.aio.downloader.core

import android.content.Context
import android.content.SharedPreferences
import android.util.Log

/**
 * Where the queue snapshot and the run history actually live.
 *
 * The shape is [RunPersistence]'s; this is only the bytes. One reader and one
 * writer: [DownloadRepository], which holds a single instance from
 * `attach(context)`.
 *
 * ── WHY SharedPreferences AND NOT A FILE ───────────────────────────────────
 * The same argument `core/AppSettings.kt` makes at length, and the same one
 * `core/DownloadForm.kt` already followed: this app keeps ONE persistence
 * mechanism. Both blobs here are read synchronously at process start, before
 * anything can enqueue — a coroutine-scoped store would leave a window where a
 * job queued in the first frames is overwritten by a restore landing after it.
 * The desktop's equivalent is three JSON files in userData
 * (`UI-source/electron/history.js`), which is the same design with a different
 * container.
 *
 * ── AND WHY commit() AND NOT apply() ───────────────────────────────────────
 * `apply()` writes on a background thread and only guarantees the write
 * survives an ORDERLY process death. The failure this whole file exists to fix
 * is the disorderly one — the user force-stopping the app, or Android killing
 * it for memory, seconds after queueing something. `commit()` is a few
 * milliseconds for a few KB and is the only version that survives that.
 * AppSettings.save reaches the same conclusion from the same starting point.
 *
 * Every operation swallows its failure: a queue that cannot be persisted must
 * still be a queue that runs.
 */
internal class RunStore(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun loadQueue(): QueueSnapshot =
        decodeQueueSnapshot(runCatching { prefs.getString(KEY_QUEUE, null) }.getOrNull())

    fun saveQueue(snapshot: QueueSnapshot) {
        write(KEY_QUEUE, encodeQueueSnapshot(snapshot), "queue")
    }

    fun loadHistory(): List<RunRecord> =
        decodeRunRecords(runCatching { prefs.getString(KEY_HISTORY, null) }.getOrNull())

    fun saveHistory(records: List<RunRecord>) {
        write(KEY_HISTORY, encodeRunRecords(records), "history")
    }

    private fun write(key: String, value: String, what: String) {
        runCatching { prefs.edit().putString(key, value).commit() }
            .onFailure { Log.w(TAG, "could not persist the $what", it) }
    }

    private companion object {
        const val TAG = "AioRunStore"

        /** The app's one preferences file — `AppSettings.PREFS` names the same
         *  one. Distinct keys, so the two never collide. */
        const val PREFS = "aio_ui"

        const val KEY_QUEUE = "download_queue_v1"
        const val KEY_HISTORY = "download_history_v1"
    }
}
