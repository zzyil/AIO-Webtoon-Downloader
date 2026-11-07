package com.aio.downloader.core

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Cross-site search state.
 *
 * An `object` for the same reason [LibraryRepository] is one: a search takes
 * 40-100 seconds and must survive the Activity being destroyed, and results
 * should still be there when the user comes back to the tab rather than costing
 * another minute of network.
 *
 * ── ONE SEARCH AT A TIME, AND IT SHARES THE ENGINE WITH DOWNLOADS ──────────
 * A search is a full aio-dl.py run, so it takes `aio_android._ENGINE_LOCK` —
 * the same lock a download holds and a library update check try-acquires. The
 * Python side fails fast with `engine_busy` rather than blocking, which is what
 * keeps a tap from hanging behind a 40-minute download. [busyWithDownload] is
 * the soft, pre-tap half of that guard; losing the race to it is harmless
 * precisely because the hard half exists.
 *
 * ── WHY THERE IS NO CANCEL ─────────────────────────────────────────────────
 * `--search` has no cancellation path (unlike a download, which has
 * `aio_android.cancel`), and the fan-out already stops WAITING at its own soft
 * barrier. [clear] drops the RESULT and lets the in-flight run finish into
 * nothing, which is honest about what actually happens rather than showing a
 * Cancel button that does not cancel.
 *
 * Cross-file: aio_android.search / build_search_argv (the Python side),
 * SearchModels.kt (the shapes), SearchScreen.kt (the UI),
 * UI-source/electron/searcher.js (the behaviour being ported).
 */
object SearchRepository {

    private const val TAG = "AioSearch"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** null until the first search runs — distinct from "searched, found nothing". */
    private val _outcome = MutableStateFlow<SearchOutcome?>(null)
    val outcome: StateFlow<SearchOutcome?> = _outcome.asStateFlow()

    private val _searching = MutableStateFlow(false)
    val searching: StateFlow<Boolean> = _searching.asStateFlow()

    /** The query currently in flight, so the UI can name what it is waiting on. */
    private val _pendingQuery = MutableStateFlow<String?>(null)
    val pendingQuery: StateFlow<String?> = _pendingQuery.asStateFlow()

    @Volatile
    private var job: Job? = null

    /** True while a download holds the engine. The soft half of the guard above. */
    fun busyWithDownload(): Boolean = DownloadRepository.active.value != null

    /**
     * Run one search. A search already in flight wins and the call is dropped,
     * so a double-tap cannot stack two minute-long fan-outs.
     *
     * [settingsJson] is the same flat settings blob the download path uses;
     * `build_search_argv` reads the search knobs and the Resource Limits level
     * out of it. Passing null is fine and means "mobile defaults".
     */
    fun search(context: Context, query: String, settingsJson: String? = null) {
        val trimmed = query.trim()
        if (trimmed.isEmpty() || _searching.value) return
        val app = context.applicationContext
        _searching.value = true
        _pendingQuery.value = trimmed
        job = scope.launch {
            try {
                val module = Aio.module(app)
                val json = if (settingsJson == null) {
                    module.callAttr("search", trimmed).toString()
                } else {
                    module.callAttr("search", trimmed, settingsJson).toString()
                }
                _outcome.value = parseSearchOutcome(trimmed, json)
            } catch (e: Exception) {
                // Anything that escapes Python entirely (a JNI-level failure, an
                // OOM during the fan-out) still has to reach the user as words.
                Log.e(TAG, "search failed", e)
                _outcome.value = SearchOutcome(
                    trimmed,
                    emptyList(),
                    "The search could not run: ${e.message ?: e::class.java.simpleName}",
                )
            } finally {
                _searching.value = false
                _pendingQuery.value = null
            }
        }
    }

    /** Drop the current results. Does not stop an in-flight run — see the header. */
    fun clear() {
        _outcome.value = null
    }
}
