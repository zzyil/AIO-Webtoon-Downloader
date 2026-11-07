package com.aio.downloader.core

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The one holder of [AppSettings].
 *
 * WHY A SINGLETON RATHER THAN A ViewModel: the settings are read from places
 * with no composition and no Activity — [com.aio.downloader.DownloadService]'s
 * worker thread when it resolves an argv, and MainActivity's intent harness.
 * A ViewModel-scoped copy would leave those reading the file directly, and then
 * a change made in Settings would not reach a download queued a second later.
 *
 * TWO READ SHAPES, both needed:
 *   * [current] — synchronous, memoized, for the argv/enqueue paths. Costs one
 *     SharedPreferences read on first call and nothing after.
 *   * [settings] — a StateFlow, for the Settings screen to render and for
 *     anything that should re-compose when a value changes.
 *
 * Writes go through [update], which persists and emits in that order, so a
 * caller that reads [current] straight after a write cannot observe the old
 * value.
 */
object AppSettingsStore {

    private val _settings = MutableStateFlow<AppSettings?>(null)

    /**
     * Never null once anything has read it; starts null so a composable can
     * tell "not loaded yet" from "loaded and default". Use [flow] to observe.
     */
    val settings: StateFlow<AppSettings?> = _settings.asStateFlow()

    private val lock = Any()

    /** The current settings, loading them on first use. Safe from any thread. */
    fun current(context: Context): AppSettings = synchronized(lock) {
        _settings.value ?: AppSettings.load(context).also { _settings.value = it }
    }

    /** Apply [transform], persist it, and publish. Returns the new value. */
    fun update(context: Context, transform: (AppSettings) -> AppSettings): AppSettings =
        synchronized(lock) {
            val next = transform(current(context))
            AppSettings.save(context, next)
            _settings.value = next
            next
        }

    /**
     * Reset every field to the shipped default.
     *
     * Keeps [AppSettings.libraryPath] — a reset is "put the behaviour back",
     * and silently repointing the library at a different folder would strand
     * the user's whole collection behind a restart they did not ask for.
     */
    fun resetBehaviour(context: Context): AppSettings =
        update(context) { AppSettings(libraryPath = it.libraryPath) }
}
