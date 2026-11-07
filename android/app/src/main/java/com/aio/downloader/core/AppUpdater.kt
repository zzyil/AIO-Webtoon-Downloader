package com.aio.downloader.core

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import androidx.core.content.FileProvider
import com.aio.downloader.BuildConfig
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.time.Instant
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * APP SELF-UPDATE — the Android counterpart of `UI-source/electron/updater.js`.
 *
 * NOT the manga chapter update check (that is `aio_android.check_series_updates`,
 * reached from [LibraryRepository]). This owns the app updating ITSELF.
 *
 * ── WHAT TRANSFERS FROM THE DESKTOP, AND WHAT CANNOT ───────────────────────
 * Transfers verbatim: a background check on a timer, a MATURITY DELAY so a bad
 * release can be pulled before anyone installs it, no downgrades ever, and a
 * status object the Settings screen renders. The state names below deliberately
 * mirror updater.js's so a reader of one file recognizes the other.
 *
 * Does NOT transfer: `autoInstallOnAppQuit`. The desktop's entire design is a
 * silent install with no dialog; Android has no equivalent and will not get one.
 * Replacing an installed package requires either a user-confirmed
 * PackageInstaller prompt or a device-owner/privileged app, and this is neither.
 * So the honest shape here is CHECK -> DOWNLOAD -> NOTIFY -> the user taps and
 * confirms the system installer. Anything that claimed otherwise would be
 * describing a feature Android does not offer.
 *
 * Two further deliberate divergences, both because "silent" is not on the table:
 *
 *   * **Opt-IN, where the desktop is opt-OUT.** updater.js's pref is
 *     absent-means-ON because its whole flow is invisible. This one ends in a
 *     notification and a system dialog, and it spends the user's data on a
 *     ~50 MB APK, so it defaults OFF.
 *   * **Wi-Fi gating.** An eligible update on a METERED connection parks in
 *     [State.AWAITING_WIFI] rather than downloading. [downloadNow] overrides it,
 *     because an explicit tap is consent.
 *
 * ── THE FEED ───────────────────────────────────────────────────────────────
 * `https://github.com/<BuildConfig.UPDATE_FEED_REPO>/releases/latest/download/android-latest.json`
 *
 * Same rule as the desktop's `app-update.yml`: the feed is whichever repo BUILT
 * this APK, stamped in at build time (grep `aioUpdateFeedRepo` in
 * app/build.gradle.kts, `AIO_UPDATE_FEED_REPO` in .github/workflows/). The file
 * is the Android analogue of electron-builder's `latest.yml` and is written by
 * the `android` job in release.yml — read that job before changing this parser;
 * the two are one contract. Schema (`schema: 1`):
 *
 *     versionCode    Int     what [BuildConfig.VERSION_CODE] is compared against
 *     versionName    String  display only
 *     releaseDate    String  ISO-8601 UTC, stamped at BUILD time — the maturity clock
 *     apk            String  asset filename, resolved against the same /latest/download/ base
 *     size           Long    expected byte count
 *     sha256         String  integrity, see below
 *     minSdk         Int     applicability
 *     abis           Array   applicability
 *     signingSha256  String  optional; certificate digest, see below
 *
 * `/releases/latest/download/` rather than the REST API on purpose: it needs no
 * token, is not subject to the 60-requests/hour unauthenticated API limit that a
 * carrier-NAT'd device could plausibly hit, and — because the APK is fetched
 * through the SAME base — feed and binary come from one release. A draft release
 * is not "latest", so drafts stay invisible to installed apps exactly as they do
 * on the desktop. Publishing the draft is still the go-live act.
 *
 * ── WHAT sha256 IS AND IS NOT FOR ──────────────────────────────────────────
 * It catches a truncated download and the narrow race where GitHub's "latest"
 * moves between the feed fetch and the APK fetch. It is NOT the security
 * boundary: Android verifies the APK's own signature against the installed app's
 * certificate before replacing anything, so a tampered or re-signed APK is
 * rejected by the OS whatever this file thinks. Saying so plainly matters
 * because the download lands in external app-scoped storage (see [updateDir]).
 *
 * ── SIDELOAD REALITY ───────────────────────────────────────────────────────
 * This app is sideloaded, and three things can make an update impossible. All
 * three are detected BEFORE anything is downloaded, and each reports the actual
 * reason instead of a generic failure:
 *
 *   1. **Wrong key.** A CI build with no release keystore is signed with a
 *      per-machine debug key, and Android refuses to replace an app with one
 *      signed differently (INSTALL_FAILED_UPDATE_INCOMPATIBLE). Handled twice:
 *      `BuildConfig.RELEASE_SIGNED` is false for such builds, and the feed's
 *      optional `signingSha256` is compared against this install's own
 *      certificate digest, which catches "installed from a DIFFERENT source than
 *      the feed" — e.g. an APK someone rebuilt and re-signed themselves.
 *   2. **A store owns the app.** If the installing package is Play/F-Droid/etc.,
 *      that store is responsible for updates and we stay out of the way.
 *   3. **No install permission.** `REQUEST_INSTALL_PACKAGES` must be DECLARED in
 *      the manifest and then GRANTED by the user. Those are different failures
 *      and get different messages — an undeclared permission cannot be granted
 *      from Settings at all, so telling the user to go there would be a lie.
 *
 * ── WIRING ─────────────────────────────────────────────────────────────────
 * ONE entry point: [start], from `MainActivity.onCreate`. Everything else is
 * driven from the returned [status] flow. Self-contained on purpose —
 * MainActivity.kt and ui/screens/SettingsScreen.kt are owned elsewhere, so this
 * file brings its own persistence ([PREFS] / [PREFS_KEY]) and its own coroutine
 * scope rather than extending [AppSettings].
 *
 * REQUIRES in AndroidManifest.xml:
 *     <uses-permission android:name="android.permission.REQUEST_INSTALL_PACKAGES" />
 * Without it [Support.canDeclareInstall] is false and the updater reports
 * itself unsupported rather than downloading an APK it can never hand over.
 */
object AppUpdater {

    // ── tunables ──────────────────────────────────────────────────────────

    /** Mirrors updater.js: let the launch rush finish before touching the network. */
    private const val INITIAL_DELAY_MS = 30_000L

    /** Mirrors updater.js's 4h re-check, for the rare long-lived process. */
    private const val RECHECK_INTERVAL_MS = 4L * 60 * 60 * 1000

    /**
     * Floor between two network checks, ACROSS process deaths.
     *
     * No desktop analogue and none needed there: Electron's process lives for
     * days, so its 4h interval IS the rate limit. An Android process is killed
     * constantly, so the 30s post-launch check would otherwise fire on every
     * cold start — ten launches, ten fetches.
     */
    private const val MIN_CHECK_INTERVAL_MS = 6L * 60 * 60 * 1000

    private const val DAY_MS = 24L * 60 * 60 * 1000
    const val DEFAULT_DELAY_DAYS = 5      // mirror of updater.js DEFAULT_DELAY_DAYS
    const val MAX_DELAY_DAYS = 60         // mirror of updater.js MAX_DELAY_DAYS

    private const val CONNECT_TIMEOUT_MS = 15_000
    private const val READ_TIMEOUT_MS = 30_000

    /** A feed that is not a few hundred bytes is an error page, not a feed. */
    private const val MAX_FEED_BYTES = 64L * 1024

    /** Sanity ceiling on the APK. The real one measures ~50 MB. */
    private const val MAX_APK_BYTES = 400L * 1024 * 1024

    private const val FEED_NAME = "android-latest.json"

    /**
     * Shared with [AppSettings] — same file, different key, so a settings reset
     * cannot wipe the update prefs and vice versa. The literal is repeated
     * rather than reaching for AppSettings' `internal` accessor because that
     * file is owned by another work stream; grep "aio_ui" if it ever moves.
     */
    private const val PREFS = "aio_ui"
    private const val PREFS_KEY = "app_update_v1"

    private const val CHANNEL_ID = "app-updates"

    /** DownloadService owns 1001; keep them distinct or one replaces the other. */
    private const val NOTIFICATION_ID = 1002

    private const val TAG = "AioUpdate"

    // ── public surface ────────────────────────────────────────────────────

    enum class State {
        /** Structurally impossible on this install. [UpdateStatus.reason] says why. */
        UNSUPPORTED,
        DISABLED,
        IDLE,
        CHECKING,
        /** A newer release exists but is younger than the maturity delay. */
        DEFERRED,
        /** Mature and wanted, but the radio is metered. [downloadNow] overrides. */
        AWAITING_WIFI,
        DOWNLOADING,
        /** APK verified on disk. Android cannot install it without the user. */
        DOWNLOADED,
        UP_TO_DATE,
        /** No `android-latest.json` on the newest release — the benign case. */
        NO_FEED,
        ERROR,
    }

    data class UpdateStatus(
        val supported: Boolean = false,
        val reason: String? = null,
        val enabled: Boolean = false,
        val state: State = State.DISABLED,
        val currentVersion: String = BuildConfig.VERSION_NAME,
        val currentVersionCode: Int = BuildConfig.VERSION_CODE,
        val latestVersion: String? = null,
        val latestVersionCode: Int? = null,
        val percent: Int? = null,
        val eligibleAtMillis: Long? = null,
        val error: String? = null,
        /**
         * True when the only thing standing between the user and an install is
         * the "install unknown apps" grant. Distinct from !supported: this one
         * the user can fix, and [openInstallPermissionSettings] takes them there.
         */
        val needsInstallPermission: Boolean = false,
        val delayDays: Int = DEFAULT_DELAY_DAYS,
    )

    private val _status = MutableStateFlow(UpdateStatus())

    /** Single source of truth for a Settings screen. */
    val status: StateFlow<UpdateStatus> = _status.asStateFlow()

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val started = AtomicBoolean(false)
    private val busy = AtomicBoolean(false)

    /** Set once by [start]; every later entry point uses it, so no Activity leaks. */
    @Volatile
    private var appContext: Context? = null

    @Volatile
    private var readyApk: File? = null

    /**
     * THE entry point. Call once from `MainActivity.onCreate`:
     *
     *     AppUpdater.start(this)
     *
     * Idempotent — a relaunch through the singleTop path re-enters onCreate, and
     * a second scheduler would double every check. Returns immediately; the
     * first check is [INITIAL_DELAY_MS] later so it never competes with startup.
     */
    fun start(context: Context) {
        val ctx = context.applicationContext
        appContext = ctx
        refreshStatus(ctx)
        if (!started.compareAndSet(false, true)) return

        scope.launch {
            // Stale APKs from versions already installed are dead weight in
            // external storage; clearing them here (not on install, which we
            // never observe completing) is the only reliable moment.
            runCatching { pruneOldDownloads(ctx) }
            delay(INITIAL_DELAY_MS)
            while (true) {
                if (prefs(ctx).enabled) runCheck(ctx, force = false)
                delay(RECHECK_INTERVAL_MS)
            }
        }
    }

    /** Settings toggle. Enabling arms a check immediately — the user just asked. */
    fun setEnabled(context: Context, enabled: Boolean) {
        val ctx = context.applicationContext
        savePrefs(ctx, prefs(ctx).copy(enabled = enabled))
        refreshStatus(ctx)
        if (enabled) scope.launch { runCheck(ctx, force = true) }
    }

    /**
     * Settings maturity-delay field. Clamped to 0..[MAX_DELAY_DAYS] like the
     * desktop's. Lowering it while a release sits DEFERRED re-evaluates at once,
     * which is the "update me now" escape hatch updater.js also documents.
     */
    fun setDelayDays(context: Context, days: Int) {
        val ctx = context.applicationContext
        val clamped = days.coerceIn(0, MAX_DELAY_DAYS)
        val current = prefs(ctx)
        if (clamped == current.delayDays) return
        savePrefs(ctx, current.copy(delayDays = clamped))
        refreshStatus(ctx)
        if (current.enabled && _status.value.state == State.DEFERRED) {
            scope.launch { runCheck(ctx, force = true) }
        }
    }

    /** Settings "Check now". Bypasses [MIN_CHECK_INTERVAL_MS], not the enable gate. */
    fun checkNow(context: Context) {
        val ctx = context.applicationContext
        appContext = ctx
        scope.launch { runCheck(ctx, force = true) }
    }

    /** Settings "Download now" — explicit consent, so metered is fine. */
    fun downloadNow(context: Context) {
        val ctx = context.applicationContext
        appContext = ctx
        scope.launch { runCheck(ctx, force = true, allowMetered = true) }
    }

    /**
     * Hand the verified APK to the system installer. Returns false when there is
     * nothing to install or the grant is missing — the caller should then send
     * the user to [openInstallPermissionSettings].
     */
    fun installNow(context: Context): Boolean {
        val ctx = context.applicationContext
        val apk = readyApk?.takeIf { it.isFile } ?: return false
        val support = Support.of(ctx)
        if (!support.canRequestInstall) {
            _status.update { it.copy(needsInstallPermission = true) }
            return false
        }
        return runCatching { ctx.startActivity(installIntent(ctx, apk)) }
            .onFailure { Log.w(TAG, "install intent failed", it) }
            .isSuccess
    }

    /**
     * Open the per-app "install unknown apps" screen. Only meaningful when the
     * permission is DECLARED — an undeclared app is not listed there at all,
     * which is why [Support] separates the two cases.
     */
    fun openInstallPermissionSettings(context: Context): Boolean {
        val ctx = context.applicationContext
        val intent = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES)
            .setData(Uri.parse("package:${ctx.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return runCatching { ctx.startActivity(intent) }
            .onFailure { Log.w(TAG, "unknown-sources settings failed", it) }
            .isSuccess
    }

    // ── the check ─────────────────────────────────────────────────────────

    /**
     * One full pass: fetch feed -> applicability -> maturity -> download -> notify.
     *
     * Serialized by [busy] rather than a mutex: overlapping passes would fight
     * over the same partial file, and a check that arrives while one is running
     * has nothing to add. Never throws — every failure lands in the status.
     */
    private suspend fun runCheck(context: Context, force: Boolean, allowMetered: Boolean = false) {
        val support = Support.of(context)
        if (!support.supported) {
            refreshStatus(context)
            return
        }
        val stored = prefs(context)
        if (!stored.enabled) {
            refreshStatus(context)
            return
        }
        val sinceLast = System.currentTimeMillis() - stored.lastCheckAtMillis
        if (!force && sinceLast in 0 until MIN_CHECK_INTERVAL_MS) return
        if (!busy.compareAndSet(false, true)) return

        try {
            setState(State.CHECKING, error = null)
            val raw = runCatching { fetchText(feedUrl(), MAX_FEED_BYTES) }.getOrElse { err ->
                // A 404 is the NORMAL state of a repo whose newest release
                // predates the Android job, or which has no releases at all.
                // updater.js maps the same situation to "no-feed" so Settings
                // does not show a scary error for a benign one.
                if (err is FeedMissing) setState(State.NO_FEED, error = null)
                else setState(State.ERROR, error = err.message ?: err.javaClass.simpleName)
                return
            }
            savePrefs(context, prefs(context).copy(lastCheckAtMillis = System.currentTimeMillis()))

            val feed = UpdateFeed.parse(raw) ?: run {
                setState(State.ERROR, error = "Update feed is malformed")
                return
            }

            when (val verdict = UpdateFeed.applicability(feed, support.signingSha256)) {
                is Applicability.NotNewer -> {
                    readyApk = null
                    // The only moment we KNOW nothing is pending, so it is the
                    // only safe moment to drop a previously-downloaded APK — the
                    // one the user just installed, most often. [busy] guarantees
                    // no download is in flight here.
                    runCatching { updateDir(context)?.listFiles()?.forEach { it.delete() } }
                    setState(State.UP_TO_DATE, error = null, latest = null)
                    return
                }
                is Applicability.Blocked -> {
                    _status.update {
                        it.copy(
                            supported = false,
                            reason = verdict.reason,
                            state = State.UNSUPPORTED,
                            latestVersion = feed.versionName,
                            latestVersionCode = feed.versionCode,
                        )
                    }
                    return
                }
                Applicability.Applicable -> Unit
            }

            val waitMs = maturityWaitMs(feed, stored.delayDays)
            if (waitMs > 0) {
                _status.update {
                    it.copy(
                        state = State.DEFERRED,
                        latestVersion = feed.versionName,
                        latestVersionCode = feed.versionCode,
                        eligibleAtMillis = System.currentTimeMillis() + waitMs,
                        percent = null,
                        error = null,
                    )
                }
                return
            }

            // Already fetched and verified on an earlier pass: skip straight to
            // the notification rather than re-downloading 50 MB.
            val target = File(updateDir(context) ?: run {
                setState(State.ERROR, error = "External storage is unavailable")
                return
            }, feed.apk)
            if (target.isFile && target.length() == feed.size && sha256(target) == feed.sha256) {
                onApkReady(context, feed, target)
                return
            }

            if (!allowMetered && isMetered(context)) {
                _status.update {
                    it.copy(
                        state = State.AWAITING_WIFI,
                        latestVersion = feed.versionName,
                        latestVersionCode = feed.versionCode,
                        percent = null,
                        error = null,
                    )
                }
                return
            }

            _status.update {
                it.copy(
                    state = State.DOWNLOADING,
                    latestVersion = feed.versionName,
                    latestVersionCode = feed.versionCode,
                    percent = 0,
                    eligibleAtMillis = null,
                    error = null,
                )
            }
            runCatching { downloadApk(feed, target) }
                .onSuccess { onApkReady(context, feed, target) }
                .onFailure { err ->
                    setState(State.ERROR, error = err.message ?: err.javaClass.simpleName)
                }
        } finally {
            busy.set(false)
        }
    }

    private fun onApkReady(context: Context, feed: UpdateFeed.Feed, apk: File) {
        readyApk = apk
        val support = Support.of(context)
        _status.update {
            it.copy(
                state = State.DOWNLOADED,
                latestVersion = feed.versionName,
                latestVersionCode = feed.versionCode,
                percent = 100,
                eligibleAtMillis = null,
                error = null,
                needsInstallPermission = !support.canRequestInstall,
            )
        }
        notifyReady(context, feed, apk)
    }

    /**
     * Maturity gate, evaluated statelessly on every pass exactly as updater.js
     * does. A missing or unparseable `releaseDate` fails OPEN — the delay is a
     * convenience buffer, not a security control.
     *
     * The clock is BUILD time, same as electron-builder's `releaseDate`, and it
     * inherits the same caveat: a draft left unpublished for days eats into the
     * window. release.yml's header already says to publish drafts promptly, and
     * matching the desktop is worth more here than a marginally better clock —
     * one maturity rule to reason about across both apps.
     */
    private fun maturityWaitMs(feed: UpdateFeed.Feed, delayDays: Int): Long {
        val released = feed.releaseDateMillis ?: return 0
        return released + delayDays.coerceIn(0, MAX_DELAY_DAYS) * DAY_MS - System.currentTimeMillis()
    }

    // ── network ───────────────────────────────────────────────────────────

    private fun feedUrl(): String =
        "https://github.com/${BuildConfig.UPDATE_FEED_REPO}/releases/latest/download/$FEED_NAME"

    private fun assetUrl(name: String): String =
        "https://github.com/${BuildConfig.UPDATE_FEED_REPO}/releases/latest/download/$name"

    /** A 404 on the feed is benign and typed, so the caller can tell it apart. */
    private class FeedMissing : Exception("No update feed on the latest release")

    private fun open(url: String): HttpURLConnection =
        (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            // GitHub 302s /releases/latest/download/ to objects.githubusercontent.com.
            // Same scheme both ends, so HttpURLConnection's own follower handles
            // it; it refuses only cross-PROTOCOL redirects.
            instanceFollowRedirects = true
            setRequestProperty("Accept", "*/*")
            setRequestProperty(
                "User-Agent",
                "AIO-Downloader/${BuildConfig.VERSION_NAME} (Android ${Build.VERSION.RELEASE})",
            )
        }

    private fun fetchText(url: String, maxBytes: Long): String {
        val conn = open(url)
        try {
            val code = conn.responseCode
            if (code == HttpURLConnection.HTTP_NOT_FOUND) throw FeedMissing()
            if (code !in 200..299) throw Exception("Update check failed: HTTP $code")
            val bytes = conn.inputStream.use { input ->
                val buf = ByteArray(8 * 1024)
                val out = java.io.ByteArrayOutputStream()
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    if (out.size() > maxBytes) throw Exception("Update feed is implausibly large")
                }
                out.toByteArray()
            }
            return String(bytes, Charsets.UTF_8)
        } finally {
            conn.disconnect()
        }
    }

    /**
     * Stream to `<name>.part`, verify, then rename. The rename is what makes a
     * half-written file impossible to mistake for a finished one after a process
     * death mid-download.
     */
    private fun downloadApk(feed: UpdateFeed.Feed, target: File) {
        require(feed.size in 1..MAX_APK_BYTES) { "Update APK size ${feed.size} is implausible" }
        target.parentFile?.mkdirs()
        val part = File(target.parentFile, "${target.name}.part")
        part.delete()

        val conn = open(assetUrl(feed.apk))
        try {
            val code = conn.responseCode
            if (code !in 200..299) throw Exception("Update download failed: HTTP $code")
            val digest = MessageDigest.getInstance("SHA-256")
            var written = 0L
            var lastPercent = -1
            conn.inputStream.use { input ->
                part.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        digest.update(buf, 0, n)
                        written += n
                        if (written > MAX_APK_BYTES) throw Exception("Update APK exceeded the size cap")
                        val percent = ((written * 100) / feed.size).toInt().coerceIn(0, 100)
                        if (percent != lastPercent) {
                            lastPercent = percent
                            _status.update { it.copy(percent = percent) }
                        }
                    }
                }
            }
            if (written != feed.size) {
                throw Exception("Update download was $written bytes, expected ${feed.size}")
            }
            val actual = digest.digest().joinToString("") { "%02x".format(it) }
            if (!actual.equals(feed.sha256, ignoreCase = true)) {
                // Also the "latest moved between the two fetches" case, which is
                // why this retries on the next pass instead of being fatal.
                throw Exception("Update download failed its checksum — it will be retried")
            }
            target.delete()
            if (!part.renameTo(target)) throw Exception("Could not finalize the downloaded update")
        } finally {
            conn.disconnect()
            part.delete()
        }
    }

    private fun isMetered(context: Context): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)
    }

    // ── storage ───────────────────────────────────────────────────────────

    /**
     * `getExternalFilesDir(null)/app-updates`.
     *
     * NOT cacheDir, and the reason is res/xml/file_paths.xml: the FileProvider
     * declares `external-files-path manga/` and `root-path /storage/`, and it
     * explicitly refuses to name anything under /data. An APK in internal
     * storage therefore cannot be given to the system installer at all —
     * getUriForFile throws IllegalArgumentException. This directory is under
     * /storage/emulated/0/Android/data/<pkg>/files/, which the root-path entry
     * covers, so no change to that file is needed.
     *
     * Sibling of the default library (`…/files/manga`), never inside it.
     */
    private fun updateDir(context: Context): File? =
        context.getExternalFilesDir(null)?.let { File(it, "app-updates") }

    /** Anything that is not for a strictly newer build is dead weight. */
    private fun pruneOldDownloads(context: Context) {
        val dir = updateDir(context) ?: return
        dir.listFiles()?.forEach { file ->
            if (file.isFile && (file.name.endsWith(".part") || file.lastModified() < System.currentTimeMillis() - 30L * DAY_MS)) {
                file.delete()
            }
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                digest.update(buf, 0, n)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun installIntent(context: Context, apk: File): Intent {
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", apk)
        return Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    }

    // ── notification ──────────────────────────────────────────────────────

    /**
     * The whole difference from the desktop in one method: there, the update is
     * already on disk and installs itself on quit. Here the user has to be told,
     * and tapping has to reach the system installer.
     *
     * The install Intent IS the content intent. FLAG_GRANT_READ_URI_PERMISSION
     * rides inside the PendingIntent, so the grant is issued from this app's uid
     * when the system sends it. Silent if POST_NOTIFICATIONS was denied — the
     * Settings screen still shows DOWNLOADED, which is the backstop.
     */
    private fun notifyReady(context: Context, feed: UpdateFeed.Feed, apk: File) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        // Unguarded: notification channels are API 26 and minSdk IS 26. (The
        // `SDK_INT >= O` check around the equivalent call in DownloadService.kt
        // is dead for the same reason.) Re-creating a channel that exists is a
        // documented no-op, so this runs on every notify.
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "App updates", NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = "A new version of AIO Downloader is ready to install" },
        )
        val tap = PendingIntent.getActivity(
            context,
            2,
            installIntent(context, apk),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val note = Notification.Builder(context, CHANNEL_ID)
            .setContentTitle("Update ready: ${feed.versionName}")
            .setContentText("Tap to install. You will be asked to confirm.")
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentIntent(tap)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .build()
        runCatching { manager.notify(NOTIFICATION_ID, note) }
    }

    // ── support probe ─────────────────────────────────────────────────────

    /**
     * Everything that decides "can this install ever update itself", answered
     * once per call from PackageManager. Cheap enough to recompute rather than
     * cache: the install-unknown-apps grant can be revoked at any moment, so a
     * cached "yes" would outlive the truth.
     */
    private data class Support(
        val supported: Boolean,
        val reason: String?,
        val canDeclareInstall: Boolean,
        val canRequestInstall: Boolean,
        val signingSha256: String?,
    ) {
        companion object {
            /** Package names that own their apps' updates; we defer to them. */
            private val STORES = setOf(
                "com.android.vending",
                "com.google.android.feedback",
                "org.fdroid.fdroid",
                "org.fdroid.basic",
                "com.aurora.store",
                "com.amazon.venezia",
                "com.huawei.appmarket",
                "com.sec.android.app.samsungapps",
            )

            fun of(context: Context): Support {
                val pm = context.packageManager
                val pkg = context.packageName

                if (!BuildConfig.RELEASE_SIGNED) {
                    return unsupported(
                        "This build was not signed with the release key, so an update " +
                            "downloaded from GitHub could never install over it. Install a " +
                            "release build to get automatic updates.",
                    )
                }

                val installer = installerOf(pm, pkg)
                if (installer != null && installer in STORES) {
                    return unsupported("Installed from $installer — update through that app instead.")
                }

                val declared = runCatching {
                    pm.getPackageInfo(pkg, PackageManager.GET_PERMISSIONS)
                        .requestedPermissions
                        ?.contains("android.permission.REQUEST_INSTALL_PACKAGES") == true
                }.getOrDefault(false)
                if (!declared) {
                    return unsupported(
                        "This build cannot install APKs: AndroidManifest.xml is missing " +
                            "REQUEST_INSTALL_PACKAGES. That is a build fix, not a setting.",
                    )
                }

                // No SDK guard: canRequestPackageInstalls and
                // ACTION_MANAGE_UNKNOWN_APP_SOURCES are both API 26, and minSdk
                // IS 26 (app/build.gradle.kts).
                val granted = runCatching { pm.canRequestPackageInstalls() }.getOrDefault(false)

                return Support(
                    supported = true,
                    reason = null,
                    canDeclareInstall = true,
                    canRequestInstall = granted,
                    signingSha256 = signingDigest(pm, pkg),
                )
            }

            private fun unsupported(reason: String) =
                Support(false, reason, canDeclareInstall = false, canRequestInstall = false, signingSha256 = null)

            private fun installerOf(pm: PackageManager, pkg: String): String? = runCatching {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    pm.getInstallSourceInfo(pkg).installingPackageName
                } else {
                    @Suppress("DEPRECATION")
                    pm.getInstallerPackageName(pkg)
                }
            }.getOrNull()

            /**
             * SHA-256 of this install's signing CERTIFICATE, lowercase hex.
             *
             * Identical by construction to what release.yml writes into the
             * feed's `signingSha256`: Signature.toByteArray() IS the DER-encoded
             * certificate, and `apksigner verify --print-certs` reports the
             * SHA-256 of that same DER. Verified against a throwaway keystore —
             * `keytool -exportcert | sha256sum` and apksigner agree to the byte.
             *
             * NOT keytool -printcert -jarfile: AGP turns v1 (JAR) signing off at
             * minSdk >= 24, so there is no `META-INF` signature block for keytool
             * to read and it answers "Not a signed jar file" while exiting 0.
             */
            private fun signingDigest(pm: PackageManager, pkg: String): String? = runCatching {
                val signatures = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    val info = pm.getPackageInfo(pkg, PackageManager.GET_SIGNING_CERTIFICATES).signingInfo
                    info?.apkContentsSigners
                } else {
                    @Suppress("DEPRECATION")
                    pm.getPackageInfo(pkg, PackageManager.GET_SIGNATURES).signatures
                }
                val first = signatures?.firstOrNull() ?: return@runCatching null
                MessageDigest.getInstance("SHA-256")
                    .digest(first.toByteArray())
                    .joinToString("") { "%02x".format(it) }
            }.getOrNull()
        }
    }

    // ── feed parsing + applicability (pure; no Android APIs) ──────────────

    // internal, not private: UpdateFeed.applicability returns it, and that
    // object is internal so a unit test can reach it.
    internal sealed interface Applicability {
        /** Same or older than what is installed. Never a downgrade, ever. */
        data object NotNewer : Applicability

        /** Newer, but this device or this install could not use it. */
        data class Blocked(val reason: String) : Applicability
        data object Applicable : Applicability
    }

    /**
     * Everything about the feed that can be decided without a device.
     *
     * Split out as pure functions on purpose: this is the layer where a wrong
     * key name produces a silently-never-updating app rather than an error, and
     * it is the only part of this file a JVM unit test can reach. See
     * core/LibraryModels.kt for the same argument about JSON parsers, and note
     * that app/build.gradle.kts already puts the real org.json on the test
     * classpath for exactly this.
     */
    internal object UpdateFeed {

        data class Feed(
            val schema: Int,
            val versionCode: Int,
            val versionName: String,
            val releaseDateMillis: Long?,
            val apk: String,
            val size: Long,
            val sha256: String,
            val minSdk: Int,
            val abis: List<String>,
            val signingSha256: String?,
        )

        /** Null when the payload is not a feed at all. Never throws. */
        fun parse(raw: String): Feed? = runCatching {
            val o = JSONObject(raw)
            val apk = o.optString("apk")
            val sha = o.optString("sha256")
            val code = o.optInt("versionCode", 0)
            // An asset name with a path separator would let a feed write outside
            // the update directory. The feed is ours, but the check is one line.
            if (apk.isEmpty() || apk.contains('/') || apk.contains('\\') || apk == "..") return null
            if (sha.length != 64 || code <= 0) return null
            Feed(
                schema = o.optInt("schema", 1),
                versionCode = code,
                versionName = o.optString("versionName", code.toString()),
                releaseDateMillis = parseIso(o.optString("releaseDate")),
                apk = apk,
                size = o.optLong("size", 0L),
                sha256 = sha,
                minSdk = o.optInt("minSdk", 0),
                abis = o.optJSONArray("abis")
                    ?.let { arr -> (0 until arr.length()).mapNotNull { arr.optString(it).takeIf(String::isNotEmpty) } }
                    .orEmpty(),
                signingSha256 = o.optString("signingSha256").takeIf { it.length == 64 },
            )
        }.getOrNull()

        /** Millis, or null when absent/unparseable — the maturity gate fails open. */
        fun parseIso(value: String?): Long? {
            if (value.isNullOrBlank()) return null
            return runCatching { Instant.parse(value).toEpochMilli() }.getOrNull()
        }

        /**
         * [installedSigningSha256] is this install's own certificate digest. A
         * mismatch is the "sideloaded from somewhere other than the feed" case:
         * the download would complete and then be refused by the package
         * manager, so it is caught here instead. Absent on either side => skip,
         * because the OS still enforces it.
         */
        fun applicability(
            feed: Feed,
            installedSigningSha256: String?,
            installedVersionCode: Int = BuildConfig.VERSION_CODE,
            deviceSdk: Int = Build.VERSION.SDK_INT,
            deviceAbis: List<String> = Build.SUPPORTED_ABIS.toList(),
        ): Applicability {
            if (feed.schema != 1) {
                return Applicability.Blocked(
                    "The update feed uses format ${feed.schema}, which this version does not " +
                        "understand. Update manually from GitHub Releases.",
                )
            }
            if (feed.versionCode <= installedVersionCode) return Applicability.NotNewer
            if (feed.minSdk > deviceSdk) {
                return Applicability.Blocked(
                    "Version ${feed.versionName} needs Android API ${feed.minSdk}; this device is $deviceSdk.",
                )
            }
            if (feed.abis.isNotEmpty() && deviceAbis.none { it in feed.abis }) {
                return Applicability.Blocked(
                    "Version ${feed.versionName} ships ${feed.abis.joinToString("/")} only, and this " +
                        "device is ${deviceAbis.firstOrNull() ?: "unknown"}.",
                )
            }
            val expected = feed.signingSha256
            if (expected != null && installedSigningSha256 != null &&
                !expected.equals(installedSigningSha256, ignoreCase = true)
            ) {
                return Applicability.Blocked(
                    "This copy was signed with a different key than the releases at " +
                        "${BuildConfig.UPDATE_FEED_REPO}, so Android would refuse the update. " +
                        "Reinstall from that repo's releases to get automatic updates.",
                )
            }
            return Applicability.Applicable
        }
    }

    // ── prefs ─────────────────────────────────────────────────────────────

    private data class Prefs(
        val enabled: Boolean = false,
        val delayDays: Int = DEFAULT_DELAY_DAYS,
        val lastCheckAtMillis: Long = 0L,
    )

    private fun prefs(context: Context): Prefs {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(PREFS_KEY, null) ?: return Prefs()
        return runCatching {
            val o = JSONObject(raw)
            Prefs(
                enabled = o.optBoolean("enabled", false),
                delayDays = o.optInt("delayDays", DEFAULT_DELAY_DAYS).coerceIn(0, MAX_DELAY_DAYS),
                lastCheckAtMillis = o.optLong("lastCheckAtMillis", 0L),
            )
        }.getOrElse { Prefs() }
    }

    private fun savePrefs(context: Context, value: Prefs) {
        val json = JSONObject()
            .put("enabled", value.enabled)
            .put("delayDays", value.delayDays)
            .put("lastCheckAtMillis", value.lastCheckAtMillis)
            .toString()
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(PREFS_KEY, json).apply()
    }

    // ── status plumbing ───────────────────────────────────────────────────

    private fun setState(state: State, error: String?, latest: String? = _status.value.latestVersion) {
        _status.update {
            it.copy(
                state = state,
                error = error,
                latestVersion = latest,
                percent = if (state == State.DOWNLOADING) it.percent else null,
            )
        }
    }

    /** Recompute the parts that come from the platform rather than the network. */
    private fun refreshStatus(context: Context) {
        val support = Support.of(context)
        val stored = prefs(context)
        _status.update { previous ->
            previous.copy(
                supported = support.supported,
                reason = support.reason,
                enabled = stored.enabled,
                state = when {
                    !support.supported -> State.UNSUPPORTED
                    !stored.enabled -> State.DISABLED
                    // Coming out of an off state, land on IDLE; otherwise leave
                    // an in-flight state (CHECKING/DOWNLOADING/DOWNLOADED) alone
                    // — this runs on every start() and every settings write, and
                    // resetting it would drop a finished download's status.
                    previous.state == State.DISABLED ||
                        previous.state == State.UNSUPPORTED -> State.IDLE
                    else -> previous.state
                },
                delayDays = stored.delayDays,
                needsInstallPermission = support.supported && !support.canRequestInstall,
            )
        }
    }
}
