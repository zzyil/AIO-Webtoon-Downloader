package com.aio.downloader.browser

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.ApplicationInfo
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import org.json.JSONObject
import java.util.Collections
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutionException
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * Android's answer to Patchright: a WebView driving the same five operations
 * every browser-dependent site handler needs.
 *
 * WHO CALLS THIS: nothing in Kotlin. `aio_android.set_browser_bridge` installs
 * it as the process-wide backend factory, and Python reaches it from
 * `aio_android._WebViewBackend`, which adapts this deliberately dumb POSITIONAL,
 * PRIMITIVES-ONLY surface to `sites/browser_backend.py`'s richer Protocol. Read
 * that module's header before changing a signature here — the two are one
 * contract split across a JNI boundary, and everything structured crosses as a
 * JSON string because a Kotlin `List`/`Map` does not survive the trip.
 *
 * WHAT IT UNBLOCKS: MangaFire (every `/api/…` call needs a `vrf=` signature that
 * only the site's own obfuscated JS can mint) and any Cloudflare-challenged
 * Madara/MangaThemesia site. Both were hard failures before this existed.
 *
 * (That path is spelled with an ellipsis on purpose: Kotlin block comments
 * NEST, so a literal slash-star inside a KDoc opens a comment the closing
 * delimiter then only half-closes, and the rest of the file silently becomes a
 * comment. Java does not do this; the resulting error points at EOF.)
 *
 * ── ONE WEBVIEW PER PROFILE, AND WHY THAT MATTERS ─────────────────────────
 * `sites/browser_backend.py` hands out backends by profile name ("mangafire",
 * "fetch", "cf"), and it would be tempting to collapse them all onto one WebView
 * since Android has only one Chromium. That would be a bug: MangaFire's signer
 * bootstraps `window.__aioMfSign` into a live page and REUSES it for the rest of
 * the run, so an unrelated `fetch_html_playwright` navigation on the same
 * WebView would wipe it. The symptom — "signer missing after bootstrap" — would
 * be intermittent and would depend on which sites the run happened to touch.
 * Profiles are already the isolation unit at the seam, so they are the isolation
 * unit here. Cookies stay shared regardless (CookieManager is process-global),
 * which is exactly what we want: a challenge solved anywhere counts everywhere.
 *
 * ── THREAD RULES ──────────────────────────────────────────────────────────
 * WebView is main-thread-only; Python runs on the download worker (and on
 * MangaFire's own `mangafire-pw` daemon thread). So every method here marshals
 * onto the main looper and BLOCKS the caller. Calling in from the main thread
 * would deadlock instantly, so it throws instead — see [onMain].
 *
 * A [ReentrantLock] per profile serializes callers, because a browser session is
 * stateful: a `goto` followed by an `evaluate` is one logical operation, and a
 * second thread navigating in between would silently evaluate against the wrong
 * page.
 */
object WebViewBridge {

    const val TAG = "AioWeb"

    @Volatile
    private var appContext: Context? = null

    private val profiles = ConcurrentHashMap<String, ProfileBridge>()

    /**
     * Hosts we have already asked the user to verify, so a site that keeps
     * serving an interstitial cannot re-prompt in a loop. Process-wide rather
     * than per-profile because the clearance cookie is process-wide too.
     */
    private val challengeOffered: MutableSet<String> = Collections.synchronizedSet(mutableSetOf())

    /** Call once, before handing this object to Python. */
    fun attach(context: Context) {
        appContext = context.applicationContext
    }

    /**
     * The bridge for one browser profile. **This is what Python calls** — see
     * `aio_android.set_browser_bridge`.
     */
    fun forProfile(profile: String): ProfileBridge {
        val ctx = appContext ?: error("WebViewBridge.attach was never called")
        return profiles.getOrPut(profile) { ProfileBridge(ctx, profile) }
    }

    /** False when [host] has already been handed to the user once. */
    internal fun markChallengeOffered(host: String): Boolean = challengeOffered.add(host)

    @Volatile
    private var forcedChallenge = false

    /**
     * Make the NEXT navigation report a bot check, whether or not there is one.
     *
     * Debug seam, modelled on `AIO_COMIX_FORCE_WAF` (grep it in sites/comix.py):
     * the interactive handoff is the piece hardest to reach on purpose — it
     * needs a site that happens to be challenging right now — and a path nobody
     * can exercise is a path that rots. Reached from the intent harness with
     * `--ez forcechallenge true`; see android/TESTING.md.
     *
     * CONSUMED, not just read, so one run tests exactly one challenge. What it
     * proves is the plumbing: the notification, the Activity, the cookie
     * capture, and the caller's error path when the user declines. What it
     * canNOT prove is that a real clearance satisfies the origin afterwards —
     * that needs a genuinely challenged site.
     */
    fun forceNextChallenge() {
        forcedChallenge = true
        // Clear the per-host verdicts too, or the SECOND forced challenge in a
        // process is silently swallowed by the loop guard and the seam looks
        // broken. Found by using it twice in a row.
        challengeOffered.clear()
    }

    internal fun consumeForcedChallenge(): Boolean {
        if (!forcedChallenge) return false
        forcedChallenge = false
        Log.i(TAG, "forced challenge consumed (debug seam)")
        return true
    }

    /**
     * Forget the "already offered" verdicts.
     *
     * Called by DownloadService at the START of every run (grep
     * resetChallengeOffers). The guard exists to stop a LOOP inside one run, not
     * to make a site unsolvable for the life of the process — and the process
     * here outlives many downloads, so without this a clearance that expired
     * hours ago could never be renewed.
     */
    fun resetChallengeOffers() {
        challengeOffered.clear()
    }
}

/** Result of one async hop back from JavaScript or from a page load. */
private class Settled(val ok: Boolean, val payload: String)

/**
 * The `AioBridge` object injected into every page.
 *
 * SECURITY NOTE: `addJavascriptInterface` exposes this to EVERY page the WebView
 * loads, remote ones included. It is kept to exactly one method taking three
 * primitives for that reason — there is no object graph to walk and (since API
 * 17) no reflection surface without the annotation. The worst a hostile page can
 * do is settle a pending evaluation with garbage, which fails that one call.
 * Do not add methods here that touch files, the library, or the download queue.
 */
private object JsResolver {

    val pending = ConcurrentHashMap<String, ArrayBlockingQueue<Settled>>()

    @JavascriptInterface
    fun settle(id: String, ok: Boolean, payload: String) {
        // remove(), not get(): a page that calls settle twice for one id must not
        // be able to poison the NEXT evaluation that reuses the queue.
        pending.remove(id)?.offer(Settled(ok, payload))
    }
}

class ProfileBridge internal constructor(
    private val appContext: Context,
    private val profile: String,
) {

    private val main = Handler(Looper.getMainLooper())
    private val lock = ReentrantLock()

    // Main thread only.
    private var webView: WebView? = null
    private var pendingNav: ArrayBlockingQueue<Settled>? = null
    private var awaitingStart = false

    /** True once at least one navigation has COMPLETED — see [ensurePrimed]. */
    @Volatile
    private var primed = false

    // ── the Python-facing surface ─────────────────────────────────────────
    // Signatures are pinned by aio_android._WebViewBackend. Positional,
    // primitives only, structured data as JSON.

    /**
     * Navigate and wait for the load to finish, then hand an interstitial to the
     * user if one is in the way.
     *
     * `wait_until` is dropped at the Python adapter: WebView reports
     * `onPageFinished` and has no networkidle equivalent, and every caller in
     * the tree passes "domcontentloaded" anyway.
     */
    fun goto(url: String, timeoutMs: Int) = lock.withLock {
        navigate(url, timeoutMs.toLong())

        if (!isShowingChallenge()) return@withLock
        val host = hostOf(url)
        if (!WebViewBridge.markChallengeOffered(host)) {
            // Already burned one human interaction on this host. Letting the
            // caller proceed against the interstitial is better than prompting
            // forever; it will fail with its own domain-specific message.
            Log.w(TAG, "$profile: $host still challenged after a solve — continuing anyway")
            return@withLock
        }
        Log.i(TAG, "$profile: $host is showing a bot check; handing it to the user")
        // Throws when the user declines or the wait expires. That propagates as a
        // Python exception, which is the honest outcome: MangaFire's bootstrap
        // then reports "could not load … for signing: site verification was not
        // completed" instead of the maximally confusing "no vrf signer among 0
        // module candidates" it would print after silently scraping an
        // interstitial.
        ChallengeSolver.solve(appContext, url, CHALLENGE_SOLVE_TIMEOUT_MS)
        navigate(url, timeoutMs.toLong())
    }

    /**
     * Run a JS function expression and return its resolved value AS JSON.
     *
     * The promise-awaiting wrapper lives here rather than in Python because only
     * this side can hold the `evaluateJavascript` callback. See
     * [buildEvalScript]'s doc for what goes wrong without it.
     */
    fun evaluate(script: String, argJson: String): String = lock.withLock {
        evaluateJson(script, argJson)
    }

    /** Fully-rendered document HTML, post-hydration. */
    fun content(): String = lock.withLock {
        // Python's adapter returns this string unchanged, so the JSON the page
        // hands back has to be decoded here.
        decodeJsonString(evaluateJson(CONTENT_JS, ""))
    }

    /** True if [selector] appeared within the budget. Never throws on timeout. */
    fun waitForSelector(selector: String, timeoutMs: Int): Boolean = lock.withLock {
        val deadline = System.currentTimeMillis() + timeoutMs
        val script = "() => !!document.querySelector(${jsQuote(selector)})"
        var hit: Boolean
        do {
            hit = runCatching { evaluateJson(script, "") == "true" }.getOrDefault(false)
            if (hit) break
            Thread.sleep(SELECTOR_POLL_MS)
        } while (System.currentTimeMillis() < deadline)
        hit
    }

    /**
     * The User-Agent this WebView actually sends.
     *
     * Read off the live settings object rather than `navigator.userAgent`,
     * because the two diverge the moment anything overrides the UA and the page
     * then reports the override straight back — a wrong value that looks
     * self-consistent forever. sites/comix.py learned this the expensive way
     * (grep `_probe_true_user_agent`). We set no override at all, so this is
     * both the wire value and the value the CF clearance is bound to.
     */
    fun userAgent(): String = lock.withLock {
        onMain { ensureWebView().settings.userAgentString ?: "" }
    }

    /**
     * Cookies for [url] as the raw `a=b; c=d` header string.
     *
     * That is all `CookieManager` will give us — no domain, path, or HttpOnly
     * metadata. It is enough: the only consumer, `sites/crawlee_utils.get_cf_session`,
     * does `session.cookies.set(name, value, domain=…)` and synthesizes the
     * domain from the URL, which is the scope the browser applied anyway.
     */
    fun cookies(url: String): String =
        runCatching { CookieManager.getInstance().getCookie(url) ?: "" }.getOrDefault("")

    /**
     * Show [url] to the user until whatever is guarding it lets them through,
     * then return `{"cookies": "<header string>", "userAgent": "…"}`.
     *
     * This is the one place Android beats the desktop. Zendriver has to drive a
     * headless browser at a challenge designed to reject exactly that; here a
     * human taps a checkbox in a real Chromium with a real UA.
     */
    fun solveChallenge(url: String, timeoutMs: Int): String {
        // Deliberately NOT holding `lock`: this navigates a DIFFERENT WebView
        // (the Activity's), and the wait is minutes long. Blocking this
        // profile's session for the duration would buy nothing.
        val solved = ChallengeSolver.solve(appContext, url, timeoutMs.toLong())
        return JSONObject()
            .put("cookies", solved.cookies)
            .put("userAgent", solved.userAgent)
            .toString()
    }

    // ── internals (all assume `lock` is held) ─────────────────────────────

    private fun navigate(url: String, timeoutMs: Long) {
        val queue = ArrayBlockingQueue<Settled>(1)
        onMain {
            val view = ensureWebView()
            pendingNav = queue
            // Set BEFORE loadUrl so the flag is already true when the first
            // callback lands. See the field's role in [Client.onPageFinished].
            awaitingStart = true
            view.loadUrl(url)
        }
        val settled = queue.poll(timeoutMs, TimeUnit.MILLISECONDS)
        if (settled == null) {
            // Drop the registration so a late callback cannot settle the NEXT
            // navigation's queue.
            onMain { if (pendingNav === queue) pendingNav = null }
            throw RuntimeException("WebView[$profile]: $url did not finish loading within ${timeoutMs}ms")
        }
        // Set even on a failed load: the interface is injected at document start,
        // so an error page is still a document that carries AioBridge.
        primed = true
        if (!settled.ok) {
            throw RuntimeException("WebView[$profile]: could not load $url — ${settled.payload}")
        }
    }

    /**
     * Guarantee the WebView has completed at least one navigation before any
     * script is injected into it.
     *
     * NOT optional, and not cosmetic. `addJavascriptInterface` only takes effect
     * on the NEXT page load, so a script injected into a brand-new WebView can
     * reference an undefined `AioBridge` — and when that throws, the wrapper's
     * own error path calls `AioBridge.settle` too, so NOTHING settles and the
     * caller blocks for the full [EVAL_TIMEOUT_MS]. This is a live path, not a
     * hypothetical: sites/mangafire_vrf.py's `_alive()` probe runs before that
     * session's first goto. It happened to win the race on the test device
     * because evaluateJavascript is queued behind the pending commit — which is
     * exactly the kind of thing that stops being true on another device.
     */
    private fun ensurePrimed() {
        if (primed) return
        navigate(ABOUT_BLANK, PRIME_TIMEOUT_MS)
    }

    private fun evaluateJson(script: String, argJson: String): String {
        ensurePrimed()
        val id = UUID.randomUUID().toString()
        val queue = ArrayBlockingQueue<Settled>(1)
        JsResolver.pending[id] = queue
        try {
            val wrapped = buildEvalScript(id, script, argJson)
            onMain { ensureWebView().evaluateJavascript(wrapped, null) }
            val settled = queue.poll(EVAL_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                ?: throw RuntimeException(
                    "WebView[$profile]: script did not settle within ${EVAL_TIMEOUT_MS}ms",
                )
            if (!settled.ok) throw RuntimeException("WebView[$profile]: ${settled.payload}")
            return settled.payload
        } finally {
            JsResolver.pending.remove(id)
        }
    }

    /** Cheap "am I looking at an interstitial?" check. Never throws. */
    private fun isShowingChallenge(): Boolean {
        if (WebViewBridge.consumeForcedChallenge()) return true
        return probeShowsChallenge()
    }

    private fun probeShowsChallenge(): Boolean = runCatching {
        val probe = JSONObject(evaluateJson(CHALLENGE_PROBE_JS, ""))
        looksLikeChallenge(
            title = probe.optString("title"),
            text = probe.optString("text"),
            marker = probe.optBoolean("marker"),
        )
    }.getOrDefault(false)

    // ── main-thread plumbing ──────────────────────────────────────────────

    /**
     * Run [block] on the main looper and wait for it.
     *
     * Throws rather than deadlocking when called FROM the main thread: every
     * method here blocks on a main-thread hop, so that mistake would hang the UI
     * with no stack pointing at the cause.
     */
    private fun <T> onMain(block: () -> T): T {
        check(Looper.myLooper() != Looper.getMainLooper()) {
            "WebViewBridge must not be called from the main thread — it blocks on it"
        }
        val task = FutureTask(Callable { block() })
        main.post(task)
        return try {
            task.get(MAIN_POST_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (e: ExecutionException) {
            throw e.cause ?: e
        }
    }

    /**
     * The profile's WebView, created on first use. MAIN THREAD ONLY.
     *
     * Deliberately never attached to a window: nothing here needs pixels, only
     * DOM and JS, and a service that runs with no Activity alive still has to be
     * able to sign a request. Known consequence — an unattached WebView is
     * `document.visibilityState === "hidden"` and its rAF is throttled, so a page
     * that defers work to an animation frame would stall. Nothing we drive does
     * (MangaFire reads load-time module URLs), but that is the first thing to
     * suspect if a page renders in the Activity and not here.
     */
    @SuppressLint("SetJavaScriptEnabled")
    private fun ensureWebView(): WebView {
        webView?.let { return it }

        // Debuggable builds only: lets a future session inspect these pages from
        // desktop Chrome at chrome://inspect, which is the difference between
        // debugging a bootstrap failure in minutes and in hours.
        if (appContext.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) {
            runCatching { WebView.setWebContentsDebuggingEnabled(true) }
        }

        val view = WebView(appContext)
        view.settings.apply {
            javaScriptEnabled = true
            // Cloudflare's challenge stores state here; without it the check
            // can never complete.
            domStorageEnabled = true
            // Left at the platform default ON PURPOSE. Pinning a UA here would
            // desync the string we report from the one the clearance is bound
            // to — see [userAgent].
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(view, true)
        // Must be attached BEFORE any load: addJavascriptInterface only applies
        // from the next navigation onward. [ensurePrimed] then guarantees that
        // navigation has actually completed before a script is injected.
        view.addJavascriptInterface(JsResolver, "AioBridge")
        view.webViewClient = Client()
        webView = view
        return view
    }

    private inner class Client : WebViewClient() {

        override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
            // The load WE asked for has begun, so the next finish belongs to us.
            awaitingStart = false
        }

        override fun onPageFinished(view: WebView?, url: String?) {
            // Still awaiting our own start means this finish belongs to the
            // PREVIOUS navigation (one that timed out, most likely) and arrived
            // after we registered a new waiter. Settling on it would return a
            // caller to the old page's DOM.
            if (awaitingStart) return
            pendingNav?.offer(Settled(true, url ?: ""))
            pendingNav = null
        }

        override fun onReceivedError(
            view: WebView?,
            request: WebResourceRequest?,
            error: WebResourceError?,
        ) {
            // Sub-resource failures (a missing font, a blocked tracker) are
            // noise; only a dead main frame means the navigation failed. Note
            // that an HTTP 403 challenge page is NOT this — it arrives through
            // onReceivedHttpError with a real body, which is what we want.
            if (request?.isForMainFrame != true) return
            if (awaitingStart) return
            pendingNav?.offer(Settled(false, "${error?.errorCode} ${error?.description}"))
            pendingNav = null
        }
    }

    private companion object {
        const val TAG = WebViewBridge.TAG

        const val CONTENT_JS = "() => document.documentElement.outerHTML"

        const val ABOUT_BLANK = "about:blank"

        /** A local, empty document. If this is slow, something is very wrong. */
        const val PRIME_TIMEOUT_MS = 10_000L

        /**
         * Ceiling on ONE script settling. Sits just under
         * `sites/mangafire_vrf.py:_BOOTSTRAP_TIMEOUT_S` (120s) on purpose: if
         * this outlived that, the signer's Future would give up while this
         * thread stayed blocked, wedging its worker for every later call.
         */
        const val EVAL_TIMEOUT_MS = 110_000L

        /** A busy main thread should never take anywhere near this to run a post. */
        const val MAIN_POST_TIMEOUT_MS = 15_000L

        const val SELECTOR_POLL_MS = 200L

        /** How long the user has to tap through a check found mid-navigation. */
        const val CHALLENGE_SOLVE_TIMEOUT_MS = 180_000L
    }
}

/**
 * Decode a JSON-encoded string value back to its raw text.
 *
 * org.json has no top-level scalar parser, so the value is wrapped in an object
 * to reach `getString`. Only used for [ProfileBridge.content], whose Python
 * caller expects raw HTML rather than JSON.
 */
private fun decodeJsonString(json: String): String {
    if (json.isEmpty() || json == "null") return ""
    return runCatching { JSONObject("{\"v\":$json}").getString("v") }.getOrDefault("")
}
