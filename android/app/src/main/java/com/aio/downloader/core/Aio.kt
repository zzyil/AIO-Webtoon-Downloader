package com.aio.downloader.core

import android.content.Context
import android.util.Log
import com.aio.downloader.browser.WebViewBridge
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/**
 * The single entry point to the Python side.
 *
 * WHY IT IS CENTRALIZED: `Python.start` must happen exactly once per process
 * and `aio_android.configure` must happen before anything else calls in — it
 * sets the working directory and the library root that every downstream path
 * resolves against. Scattering those two calls means the first component to
 * touch Python decides the configuration, which is a rule nobody can follow.
 *
 * EVERY CALL HERE BLOCKS. Importing the handler registry pulls in ~65 modules
 * and a download blocks for minutes; either one on the main thread is an ANR.
 * Callers run on a background thread, and [moduleOrNull] exists so a UI caller
 * can ask "is Python up yet?" without being the one to boot it.
 *
 * JNI RULE FOR EVERYTHING BELOW: the boundary is positional and primitives-only.
 * A `java.util.List` does NOT arrive as a Python iterable — Chaquopy wraps it in
 * an opaque proxy and Python raises `TypeError: 'ArrayList' object is not
 * iterable`. Java ARRAYS convert; `List` does not. So anything structured
 * crosses as a JSON string, which is why the module exposes `run_download_json`
 * rather than `run_download`.
 */
object Aio {

    private const val TAG = "AioCore"

    /**
     * Where downloads land.
     *
     * Default: app-scoped external storage — no permission, visible over
     * USB/MTP, and Python can `open()` it directly (which is the whole reason
     * SAF was rejected: it hands back `content://` URIs that `open()` cannot
     * take, and adopting it would mean rewriting every path in a 40k-line
     * codebase). Removed with the app, which is the cost of needing no grant.
     *
     * Custom: whatever [AppSettings.libraryPath] names, once the user has
     * granted MANAGE_EXTERNAL_STORAGE. That is the Komikku/Mihon case — those
     * readers cannot see into another app's scoped directory.
     */
    fun libraryDir(context: Context): File = resolveLibraryDir(context)

    /** The no-permission default, regardless of what is configured. */
    fun defaultLibraryDir(context: Context): File =
        File(context.getExternalFilesDir(null), "manga")

    /**
     * Why the configured custom path was not used, or null when it was (or when
     * there is none). Read by the Settings screen so a silently-revoked
     * permission is reported rather than presenting as an empty library.
     */
    @Volatile
    var libraryFallbackReason: String? = null
        private set

    /**
     * The library root actually in force.
     *
     * FAILS SAFE. `aio_android.configure` does `makedirs` on whatever it is
     * given, so an unusable custom path would throw out of [module] and take
     * the entire app down — every screen, not just the library. A revoked
     * MANAGE_EXTERNAL_STORAGE grant is a completely ordinary way for that to
     * happen (the user can revoke it in system settings at any time, and some
     * OEM "storage cleaners" do it for them). So a custom path that cannot be
     * created or written falls back to the default and records why.
     *
     * This is a PRE-FLIGHT check only. `aio_android.probe_library_root` is the
     * authoritative one and runs when the user picks the folder — it proves
     * writability by actually writing, because under scoped storage the
     * permission bits and the sandbox's answer routinely disagree.
     */
    fun resolveLibraryDir(context: Context): File {
        val app = context.applicationContext
        val configuredPath = AppSettingsStore.current(app).libraryPath.trim()
        if (configuredPath.isEmpty()) {
            libraryFallbackReason = null
            return defaultLibraryDir(app)
        }

        val custom = File(configuredPath)
        val usable = runCatching { custom.isDirectory || custom.mkdirs() }.getOrDefault(false) &&
            runCatching { custom.canWrite() }.getOrDefault(false)
        if (usable) {
            libraryFallbackReason = null
            return custom
        }

        libraryFallbackReason =
            "Can't write to $configuredPath — using the app's own folder instead. " +
                "Grant all-files access again, or pick a different folder."
        return defaultLibraryDir(app)
    }

    @Volatile
    private var configured = false

    private val lock = Any()

    /**
     * Start Python if needed, configure it once, and return the `aio_android`
     * module. Blocking — never call from the main thread.
     */
    fun module(context: Context): PyObject = synchronized(lock) {
        val app = context.applicationContext
        if (!Python.isStarted()) Python.start(AndroidPlatform(app))
        val module = Python.getInstance().getModule("aio_android")
        if (!configured) {
            // ONCE per process, and that is a hard constraint, not tidiness:
            // configure() sets the process CWD and the AIO_OUTPUT_DIR /
            // XDG_CACHE_HOME environment, and aio-dl.py bakes several
            // module-level constants in at import time. So changing the library
            // root takes effect on the next app START — which is what the
            // Settings screen tells the user, rather than pretending otherwise
            // and leaving half the run pointed at the old root.
            module.callAttr(
                "configure",
                resolveLibraryDir(app).absolutePath,
                app.cacheDir.absolutePath,
            )

            // The browser. Without this, sites that sign or render through a
            // real Chromium (MangaFire's vrf tokens, any Cloudflare-challenged
            // Madara site) cannot work at all — Patchright does not run here.
            // Installed AFTER configure so the profile dirs it sets are already
            // in the environment, and BEFORE any handler runs, which is the
            // contract sites/browser_backend.set_backend_factory documents.
            WebViewBridge.attach(app)
            module.callAttr("set_browser_bridge", WebViewBridge)

            // The image codec. Chaquopy's Pillow ships no WebP codec at all, so
            // without this `--webtoon-recompress` silently does nothing and
            // EPUB/PDF/width/scaling/quality silently drop every WebP page
            // (android/PARITY.md D3, D4). The platform has had the codec since
            // API 14; this hands it to Pillow. Returns the capability snapshot
            // rather than Unit so the log says what the install actually bought
            // — on a build whose Pillow DOES have a codec it correctly buys
            // nothing and registers nothing.
            Log.i(TAG, "image codec: ${module.callAttr("set_image_codec_bridge", ImageCodecBridge)}")

            configured = true
        }
        module
    }

    /**
     * The module if Python is ALREADY up, else null. For callers that want to
     * read state without paying the ~2s import cost or risking an ANR — the
     * diagnostics sheet uses this to decide whether it can answer immediately.
     */
    fun moduleOrNull(): PyObject? =
        if (Python.isStarted()) Python.getInstance().getModule("aio_android") else null
}
