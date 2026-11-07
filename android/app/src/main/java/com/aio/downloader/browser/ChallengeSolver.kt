package com.aio.downloader.browser

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import android.webkit.CookieManager
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * Hands an anti-bot check to the person holding the phone, and blocks the Python
 * thread that asked until they are through it.
 *
 * Who calls this: [ProfileBridge.solveChallenge] (from `get_cf_session`, via
 * `sites/crawlee_utils.py`) and [ProfileBridge.goto] when a navigation lands on
 * an interstitial. Who answers it: [ChallengeActivity].
 *
 * ── WHY A NOTIFICATION AND NOT JUST startActivity ─────────────────────────
 * Downloads run in a foreground service, so the app is frequently in the
 * background when a challenge appears — and from API 29 on, a background
 * `startActivity` is silently DROPPED (a foreground service does not earn the
 * exemption; only a visible Activity or a full-screen-intent grant does). Trying
 * anyway and stopping there would look exactly like a hung download. So both
 * happen: the direct start, which works when the user is in the app, and a
 * high-priority notification, which is the only reliable route back when they
 * are not.
 *
 * ── ONE AT A TIME ────────────────────────────────────────────────────────
 * [lock] serializes solves, so two handlers hitting challenges at once queue
 * rather than racing for the single Activity. The lock is held for the whole
 * wait, which is minutes — acceptable, because a second prompt while the first
 * is on screen is worse than waiting.
 */
object ChallengeSolver {

    private const val TAG = WebViewBridge.TAG
    private const val CHANNEL_ID = "verification"
    private const val NOTIFICATION_ID = 1002

    const val EXTRA_URL = "challenge_url"

    /** What a completed solve hands back to Python. */
    class Solved(val cookies: String, val userAgent: String)

    private class Request(
        val url: String,
        val queue: ArrayBlockingQueue<Result<Solved>> = ArrayBlockingQueue(1),
    )

    private val lock = ReentrantLock()

    @Volatile
    private var current: Request? = null

    /**
     * Block until the user clears whatever is guarding [url].
     *
     * @throws RuntimeException when they decline, or when [timeoutMs] expires.
     * Failing loudly is deliberate: a caller that got no clearance is about to
     * scrape an interstitial, and "site verification was not completed" is a far
     * more actionable message than whatever it would report about the resulting
     * empty page.
     */
    fun solve(context: Context, url: String, timeoutMs: Long): Solved = lock.withLock {
        val app = context.applicationContext
        val request = Request(url)
        current = request
        try {
            notifyUser(app, url)
            openActivity(app, url)

            val outcome = request.queue.poll(timeoutMs, TimeUnit.MILLISECONDS)
                ?: throw RuntimeException(
                    "site verification was not completed within ${timeoutMs / 1000}s (${hostOf(url)})",
                )
            return outcome.getOrThrow()
        } finally {
            current = null
            clearNotification(app)
        }
    }

    // ── the Activity's side of the conversation ───────────────────────────

    /** The URL awaiting a solve, or null when nothing is pending. */
    fun pendingUrl(): String? = current?.url

    /**
     * Report success. Cookies are read HERE rather than in the Activity so the
     * flush and the capture cannot drift apart.
     */
    fun complete(url: String, userAgent: String) {
        val request = current ?: return
        // Persist to disk now: without a flush the clearance lives only in
        // memory, and a process death before Chromium's own periodic write
        // throws away a solve the user actually performed.
        runCatching { CookieManager.getInstance().flush() }
        val cookies = runCatching { CookieManager.getInstance().getCookie(url) ?: "" }
            .getOrDefault("")
        Log.i(TAG, "challenge cleared for ${hostOf(url)} (${cookies.length} cookie chars)")
        request.queue.offer(Result.success(Solved(cookies, userAgent)))
    }

    fun fail(reason: String) {
        val request = current ?: return
        Log.i(TAG, "challenge abandoned: $reason")
        request.queue.offer(Result.failure(RuntimeException(reason)))
    }

    // ── getting the user there ────────────────────────────────────────────

    private fun openActivity(context: Context, url: String) {
        // NEW_TASK is mandatory when starting from a non-Activity context.
        // Silently dropped when the app is backgrounded on API 29+, which is
        // what the notification above is for.
        runCatching {
            context.startActivity(
                Intent(context, ChallengeActivity::class.java)
                    .putExtra(EXTRA_URL, url)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            )
        }.onFailure { Log.i(TAG, "direct activity start refused (backgrounded); notification posted") }
    }

    private fun notifyUser(context: Context, url: String) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                // HIGH, unlike the download channel's LOW: this one is a request
                // for the user to act, and a silent entry in the shade would
                // leave the download looking stalled for no visible reason.
                NotificationChannel(CHANNEL_ID, "Site verification", NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = "Asks you to complete a site's bot check" },
            )
        }
        val open = PendingIntent.getActivity(
            context,
            2,
            Intent(context, ChallengeActivity::class.java)
                .putExtra(EXTRA_URL, url)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = Notification.Builder(context, CHANNEL_ID)
            .setContentTitle("${hostOf(url)} needs a quick check")
            .setContentText("Tap to confirm you're human — the download is waiting")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(open)
            .setAutoCancel(true)
            .setOngoing(true)
            .build()
        // Silently unavailable when POST_NOTIFICATIONS was denied (API 33+). The
        // solve then depends on the direct start, i.e. on the user being in the
        // app — worth a log line, not worth failing over.
        runCatching { manager.notify(NOTIFICATION_ID, notification) }
            .onFailure { Log.w(TAG, "could not post the verification notification", it) }
    }

    private fun clearNotification(context: Context) {
        runCatching {
            context.getSystemService(NotificationManager::class.java)?.cancel(NOTIFICATION_ID)
        }
    }
}
