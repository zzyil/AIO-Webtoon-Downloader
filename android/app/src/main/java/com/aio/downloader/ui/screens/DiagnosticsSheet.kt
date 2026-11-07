package com.aio.downloader.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.aio.downloader.core.Aio
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.components.Pill
import com.aio.downloader.ui.components.PillTone
import com.aio.downloader.ui.theme.AioText
import com.aio.downloader.ui.theme.aio
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * `aio_android.diagnostics()`, rendered.
 *
 * WHY IT SURVIVED THE M0 SPIKE: it is the single fastest answer to "why did
 * every site fail". The registry counts say whether all 303 domains registered,
 * and the capability probe says which optional native wheels resolved — which is
 * exactly the difference between "this site is down" and "the APK shipped
 * without Pillow". On a phone there is no REPL to ask, so the app has to be able
 * to say it.
 *
 * Reachable from the top bar on every screen. `am start` with no `url` extra
 * still prints the same JSON to logcat; see android/TESTING.md.
 */
@Composable
fun DiagnosticsSheet(onDismiss: () -> Unit) {
    val context = LocalContext.current
    var json by remember { mutableStateOf<String?>(null) }
    var failure by remember { mutableStateOf<String?>(null) }

    // Off the main thread without exception: the first call here starts the
    // interpreter and imports ~65 modules, which is seconds of work.
    LaunchedEffect(Unit) {
        runCatching {
            withContext(Dispatchers.IO) {
                Aio.module(context).callAttr("diagnostics").toString()
            }
        }.onSuccess { json = it }.onFailure { failure = it.message ?: it.javaClass.simpleName }
    }

    Dialog(onDismissRequest = onDismiss) {
        Column(
            Modifier
                .fillMaxWidth()
                .heightIn(max = 560.dp)
                .clip(RoundedCornerShape(14.dp))
                .background(MaterialTheme.aio.card)
                .padding(20.dp),
        ) {
            Text("Diagnostics", style = MaterialTheme.typography.titleLarge)
            Spacer(Modifier.height(4.dp))
            HelpText("Handler registry and native-dependency probe, straight from Python.")
            Spacer(Modifier.height(14.dp))

            Box(Modifier.weight(1f, fill = false)) {
                when {
                    failure != null -> Text(
                        failure.orEmpty(),
                        style = AioText.log,
                        color = MaterialTheme.colorScheme.error,
                    )

                    json == null -> HelpText("Starting Python…")

                    else -> DiagnosticsBody(json.orEmpty())
                }
            }

            Spacer(Modifier.height(16.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = androidx.compose.foundation.layout.Arrangement.End) {
                AioButton(text = "Close", tone = ButtonTone.Outline, compact = true, onClick = onDismiss)
            }
        }
    }
}

/**
 * Pull the two headline numbers out of the payload and show the raw JSON below.
 *
 * Deliberately NOT a typed model: `diagnostics()` grows fields as the port does,
 * and a parser that only renders what it was taught would hide exactly the new
 * field someone added because it mattered.
 */
@Composable
private fun DiagnosticsBody(raw: String) {
    val parsed = remember(raw) { runCatching { JSONObject(raw) }.getOrNull() }
    val registered = parsed?.optInt("registered_handlers", -1) ?: -1
    val base = parsed?.optInt("base_handlers", -1) ?: -1
    // Expected 303/42 as of the tapas handler; a mismatch means a handler failed
    // to import, and `optional_handler_errors` below names it.
    val healthy = registered >= 300 && base >= 40

    Column(Modifier.verticalScroll(rememberScrollState())) {
        Row(horizontalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(6.dp)) {
            Pill(
                "$registered handlers",
                tone = if (healthy) PillTone.Success else PillTone.Danger,
                mono = true,
            )
            Pill("$base base", tone = PillTone.Neutral, mono = true)
        }
        Spacer(Modifier.height(12.dp))
        Text(
            text = prettyOrRaw(raw),
            style = AioText.log,
            color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.85f),
        )
    }
}

private fun prettyOrRaw(raw: String): String =
    runCatching { JSONObject(raw).toString(2) }.getOrDefault(raw)
