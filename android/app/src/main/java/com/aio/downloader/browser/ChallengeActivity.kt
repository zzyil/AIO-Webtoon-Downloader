package com.aio.downloader.browser

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Shield
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.CalloutTone
import com.aio.downloader.ui.components.Hairline
import com.aio.downloader.ui.components.Pill
import com.aio.downloader.ui.components.PillTone
import com.aio.downloader.ui.components.StatusCallout
import com.aio.downloader.ui.theme.AioTheme
import com.aio.downloader.ui.theme.aio
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject
import kotlin.coroutines.resume

/**
 * The visible half of the browser backend: a real Chromium the user can tap.
 *
 * WHY THIS IS THE GOOD PART OF THE ANDROID PORT. On the desktop a Cloudflare
 * check is fought — zendriver drives a headless browser at a test designed to
 * reject exactly that, and it loses often enough to need retries. Here the
 * challenge is simply shown to the person holding the device, in a browser that
 * is genuinely a browser, and they tap it.
 *
 * ── ITS OWN WEBVIEW, NOT THE BRIDGE'S ─────────────────────────────────────
 * Reparenting [ProfileBridge]'s offscreen WebView into this Activity would tie
 * a Python-held object's lifetime to an Activity that can be destroyed at any
 * moment. Unnecessary, too: `CookieManager` is process-global, so a clearance
 * earned in THIS WebView is immediately visible to every other one. Cookie
 * sharing is the whole mechanism — nothing is handed back except a header
 * string and a UA.
 *
 * The UA matches for the same reason: `settings.userAgentString` is derived from
 * the WebView provider and the device, so every instance in the process reports
 * the identical string. That is what makes the clearance this Activity earns
 * usable from the `requests` session `get_cf_session` builds.
 */
class ChallengeActivity : ComponentActivity() {

    /** Set once the user (or the detector) resolved this; stops onDestroy from failing it. */
    private var settled = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Prefer the live request over the intent extra: a notification tapped
        // long after the fact carries a stale URL, while ChallengeSolver knows
        // what is actually being waited on.
        val url = ChallengeSolver.pendingUrl()
            ?: intent.getStringExtra(ChallengeSolver.EXTRA_URL).orEmpty()
        if (url.isBlank()) {
            // Nothing pending — the solve completed or timed out before the user
            // got here. Closing silently is right; an error screen for a race
            // they cannot act on would be noise.
            finish()
            return
        }

        setContent {
            AioTheme {
                ChallengeScreen(
                    url = url,
                    onSolved = { userAgent -> settle { ChallengeSolver.complete(url, userAgent) } },
                    onCancel = { settle { ChallengeSolver.fail("verification cancelled") } },
                )
            }
        }
    }

    private inline fun settle(report: () -> Unit) {
        if (settled) return
        settled = true
        report()
        finish()
    }

    override fun onDestroy() {
        super.onDestroy()
        // Swiped away from Recents, or killed by the system. Without this the
        // Python thread waits out its full timeout for an Activity that no
        // longer exists.
        if (!settled) {
            settled = true
            ChallengeSolver.fail("verification window closed")
        }
    }
}

private enum class Phase { Loading, Waiting, Passed }

@Composable
private fun ChallengeScreen(
    url: String,
    onSolved: (userAgent: String) -> Unit,
    onCancel: () -> Unit,
) {
    var webView by remember { mutableStateOf<WebView?>(null) }
    var phase by remember { mutableStateOf(Phase.Loading) }

    BackHandler { onCancel() }

    /**
     * Watch the page until the check clears.
     *
     * The rule has one non-obvious guard: it will not report success until it
     * has SEEN a challenge, or until [GRACE_MS] has passed. Probing starts
     * immediately, and for the first moment a loading page looks exactly like a
     * page with no challenge on it — settling there would hand back a session
     * with no clearance and the download would fail anyway, one layer further
     * from the cause.
     */
    LaunchedEffect(webView) {
        val view = webView ?: return@LaunchedEffect
        val startedAt = System.currentTimeMillis()
        var sawChallenge = false
        while (isActive) {
            delay(POLL_MS)
            val probe = view.probeChallenge() ?: continue
            val title = probe.optString("title")
            val text = probe.optString("text")
            val current = probe.optString("url")
            if (current.isEmpty() || current == "about:blank") continue

            if (looksLikeChallenge(title, text, probe.optBoolean("marker"))) {
                sawChallenge = true
                phase = Phase.Waiting
                continue
            }
            val hasContent = title.isNotBlank() || text.isNotBlank()
            if (hasContent && (sawChallenge || System.currentTimeMillis() - startedAt > GRACE_MS)) {
                phase = Phase.Passed
                // A beat on the "you're through" state, so the window does not
                // vanish the instant the user taps the checkbox — that reads as
                // a crash rather than as success.
                delay(PASS_DWELL_MS)
                onSolved(view.settings.userAgentString.orEmpty())
                return@LaunchedEffect
            }
        }
    }

    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .windowInsetsPadding(WindowInsets.safeDrawing),
    ) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("Site verification", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.width(10.dp))
            Pill(hostOf(url), mono = true, tone = PillTone.Neutral)
        }
        Hairline()

        Box(Modifier.padding(16.dp)) {
            when (phase) {
                Phase.Loading -> StatusCallout(
                    title = "Opening the site",
                    body = "Waiting to see whether this site wants a check. " +
                        "If it does, it will appear below.",
                    icon = Icons.Filled.Shield,
                    tone = CalloutTone.Info,
                )
                Phase.Waiting -> StatusCallout(
                    title = "Complete the check below",
                    body = "This site is asking for proof you're human. Tap it, and the " +
                        "download picks up on its own — nothing else to do here.",
                    icon = Icons.Filled.Shield,
                    tone = CalloutTone.Warning,
                )
                Phase.Passed -> StatusCallout(
                    title = "You're through",
                    body = "Clearance saved. Returning to the download.",
                    icon = Icons.Filled.Check,
                    tone = CalloutTone.Success,
                )
            }
        }

        AndroidView(
            factory = { context ->
                WebView(context).apply {
                    @SuppressLint("SetJavaScriptEnabled")
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    // No UA override, for the same reason the offscreen bridge
                    // sets none: the clearance is bound to the identity that
                    // earned it, so the string we report has to be the one that
                    // went on the wire.
                    CookieManager.getInstance().setAcceptCookie(true)
                    CookieManager.getInstance().setAcceptThirdPartyCookies(this, true)
                    // Plain WebViewClient so navigations stay INSIDE this window;
                    // without one, Chromium hands http(s) links to an external
                    // browser and the clearance lands in a cookie jar we cannot
                    // read.
                    webViewClient = WebViewClient()
                    loadUrl(url)
                    webView = this
                }
            },
            modifier = Modifier.fillMaxWidth().weight(1f),
        )

        Hairline()
        Row(
            Modifier.fillMaxWidth().padding(16.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            AioButton(text = "Cancel", tone = ButtonTone.Ghost, onClick = onCancel)
            Spacer(Modifier.weight(1f))
            // The manual escape hatch. Detection is a heuristic over page text,
            // and the person looking at the page is a better judge than it is.
            AioButton(
                text = "I'm through",
                icon = Icons.Filled.Check,
                onClick = { webView?.let { onSolved(it.settings.userAgentString.orEmpty()) } },
            )
        }
        Spacer(Modifier.height(4.dp))
    }
}

/** One [CHALLENGE_PROBE_JS] round-trip. Null when the page could not answer. */
private suspend fun WebView.probeChallenge(): JSONObject? =
    suspendCancellableCoroutine { cont ->
        // evaluateJavascript hands back the JSON ENCODING of the value, and the
        // probe is synchronous, so no promise wrapper is needed here — unlike
        // ProfileBridge.evaluate, which has to await one.
        evaluateJavascript("($CHALLENGE_PROBE_JS)()") { json ->
            cont.resume(runCatching { JSONObject(json) }.getOrNull())
        }
    }

private const val POLL_MS = 700L
private const val PASS_DWELL_MS = 550L

/**
 * How long a never-challenged page is watched before it counts as passed. Long
 * enough for Cloudflare to serve its interstitial after the first paint, short
 * enough that a site with no check at all does not feel stuck.
 */
private const val GRACE_MS = 10_000L
