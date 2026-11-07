package com.aio.downloader.browser

/**
 * The pure half of the WebView browser backend: the JavaScript this app injects
 * into pages, and the "is this a bot check?" rule.
 *
 * NO ANDROID IMPORTS, on purpose — the same reason core/LogFilter.kt has none.
 * The fiddly parts of a browser bridge are string construction and pattern
 * matching, and both are testable with `gradlew :app:testDebugUnitTest` and no
 * device in the loop. Everything that genuinely needs a WebView lives next door
 * in WebViewBridge.kt.
 *
 * Cross-file: sites/browser_backend.py's module header states the evaluate()
 * contract this file implements; sites/crawlee_utils.py owns the desktop copy of
 * the challenge phrase list (grep _CF_CHALLENGE_PHRASES).
 */

/**
 * Wrap a caller's script into something `WebView.evaluateJavascript` can drive,
 * and route the result back through the `AioBridge` JavaScript interface.
 *
 * THE WHOLE REASON THIS EXISTS: Playwright's `page.evaluate` awaits a returned
 * promise before handing the value back. `WebView.evaluateJavascript` does NOT
 * — its callback fires with the SYNCHRONOUS result, so an async function comes
 * back as the string `{}` (a stringified un-awaited Promise). MangaFire's
 * `_BOOTSTRAP_JS` returns a promise and `_SIGN_BATCH_JS` is an async function,
 * so without this wrapper every signature comes back null and the failure looks
 * like a broken signer rather than a broken bridge.
 *
 * @param callbackId  correlates the async settle back to a waiting caller.
 * @param script      a JS **function expression** ("() => …", "async (a) => …"),
 *                    per the browser_backend contract. A non-function value is
 *                    passed through as-is, which keeps a plain expression usable.
 * @param argJson     the single argument as JSON, or "" for "no argument".
 *                    Empty string rather than "null" because `null` is itself a
 *                    legal argument value — the same distinction
 *                    sites/browser_backend.py draws with its NOARG sentinel.
 */
fun buildEvalScript(callbackId: String, script: String, argJson: String): String {
    val arg = if (argJson.isEmpty()) "" else "JSON.parse(${jsQuote(argJson)})"
    // `__settle` decides ok/failure INSIDE the page, because a result that
    // cannot be JSON-serialized has to come back as an error rather than as a
    // silent null — otherwise a caller sees "the signer returned nothing" and
    // goes looking at the site.
    return """
(function () {
  var __id = ${jsQuote(callbackId)};
  var __settle = function (ok, value) {
    var payload;
    if (ok) {
      try {
        payload = JSON.stringify(value);
      } catch (e) {
        ok = false;
        payload = 'result is not JSON-serializable: ' + e;
      }
      // JSON.stringify(undefined) evaluates to undefined, not to a string.
      if (ok && payload === undefined) payload = 'null';
    } else {
      payload = String(value);
    }
    AioBridge.settle(__id, ok, payload);
  };
  try {
    var __fn = ($script);
    var __out = (typeof __fn === 'function') ? __fn($arg) : __fn;
    Promise.resolve(__out).then(
      function (v) { __settle(true, v); },
      function (e) { __settle(false, (e && e.message) || e); }
    );
  } catch (e) {
    __settle(false, (e && e.message) || e);
  }
})();
"""
}

/**
 * Page state the challenge rule needs, as one round-trip.
 *
 * Bounded text slice on purpose: a manga chapter page's innerText can be
 * hundreds of KB, all of it crossing the JNI boundary as a string for a check
 * that only ever looks at a handful of phrases.
 */
const val CHALLENGE_PROBE_JS: String = """
() => {
  const d = document;
  const body = d.body ? (d.body.innerText || '') : '';
  const marker = !!d.querySelector(
    '#challenge-form, #challenge-running, #cf-chl-widget, [id^="cf-chl"], script[src*="challenge-platform"]'
  );
  return { title: d.title || '', text: body.slice(0, 4000), marker: marker, url: location.href };
}
"""

/**
 * Kept in sync with sites/crawlee_utils.py's `_CF_CHALLENGE_PHRASES` — that file
 * is the reference. Lowercase; matching lowercases the haystack.
 */
val CHALLENGE_PHRASES: List<String> = listOf(
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "verifying you are human",
    "cf-browser-verification",
    "cloudflare ray id",
    "cf_chl_opt",
    "challenge-platform",
)

/**
 * True when the loaded page is an anti-bot interstitial rather than content.
 *
 * WHY THE LENGTH GATE: WebView gives us no status code at `onPageFinished`, so
 * the desktop's "403/429/503 plus one phrase" branch is unavailable here and a
 * bare phrase match would fire on any series page whose synopsis happens to
 * contain "just a moment". A real interstitial is a handful of words; a real
 * page is not. So a phrase only counts when the page is also SHORT — which is
 * the same shape as the desktop's other branch (200 + `len(text) < 15_000`),
 * tightened because innerText excludes the markup the desktop was measuring.
 *
 * A challenge DOM node is trusted on its own: it is unambiguous, and the widget
 * can finish rendering before the surrounding text does.
 */
fun looksLikeChallenge(title: String, text: String, marker: Boolean): Boolean {
    if (marker) return true
    if (text.length >= CHALLENGE_MAX_BODY_CHARS) return false
    val haystack = (title + "\n" + text).lowercase()
    return CHALLENGE_PHRASES.any { it in haystack }
}

private const val CHALLENGE_MAX_BODY_CHARS = 2_000

/**
 * Quote [raw] as a JavaScript string literal.
 *
 * Hand-rolled rather than `JSONObject.quote` so this file stays Android-free and
 * unit-testable — org.json's desktop stub throws "Stub!" under a plain JVM test.
 *
 * Two escapes beyond the obvious ones, both of which turn a valid payload into a
 * syntax error at injection time if omitted: U+2028/U+2029 are legal inside a
 * JSON string but are literal line terminators in JS SOURCE, and `<` is escaped
 * so no payload can ever spell `</script`.
 */
fun jsQuote(raw: String): String {
    val sb = StringBuilder(raw.length + 16)
    sb.append('"')
    for (ch in raw) {
        when (ch) {
            '\\' -> sb.append("\\\\")
            '"' -> sb.append("\\\"")
            '\n' -> sb.append("\\n")
            '\r' -> sb.append("\\r")
            '\t' -> sb.append("\\t")
            '\b' -> sb.append("\\b")
            '\u000C' -> sb.append("\\f")
            '\u2028' -> sb.append("\\u2028")
            '\u2029' -> sb.append("\\u2029")
            '<' -> sb.append("\\u003C")
            else -> if (ch < ' ') sb.append("\\u%04x".format(ch.code)) else sb.append(ch)
        }
    }
    sb.append('"')
    return sb.toString()
}

/**
 * Host of [url]: scheme, userinfo, port, path and query stripped. A bare
 * authority with no scheme works too. Empty input comes back empty.
 *
 * Only ever used for identity — keying the "already asked this host" set and
 * titling the verification notification — so it does not need to be a URL
 * parser, and deliberately is not one.
 */
fun hostOf(url: String): String {
    val afterScheme = url.substringAfter("://", url)
    val authority = afterScheme.substringBefore('/').substringBefore('?')
    return authority.substringAfter('@').substringBefore(':').ifEmpty { url }
}
