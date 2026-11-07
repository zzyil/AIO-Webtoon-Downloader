package com.aio.downloader.ui.components

import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
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
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import coil3.request.crossfade
import com.aio.downloader.core.CoverStore
import com.aio.downloader.core.LibrarySeries
import com.aio.downloader.core.initialsOf
import com.aio.downloader.ui.theme.aio
import java.io.File

/**
 * A series cover, with the monogram fallback the desktop uses.
 *
 * ── THE FALLBACK IS ALSO THE PLACEHOLDER ───────────────────────────────────
 * Resolving a cover can mean unzipping an archive or rasterizing a PDF, so it is
 * never instant on first sight of a card. Rather than hold an empty rectangle
 * and pop, the tinted monogram is drawn IMMEDIATELY and the real cover
 * crossfades in over it. A grid that has not finished loading therefore looks
 * composed rather than broken, and a series that genuinely has no cover looks
 * identical to one still working — which is the honest reading, since from the
 * user's side those two states are the same until they aren't.
 *
 * The gradient is the desktop's `bg-gradient-to-br from-primary/20 to-primary/5`
 * and is the app's ONLY gradient. It earns that by being the one place where
 * something must fill space with no content to show.
 *
 * ── WHAT IT LOADS IS ALWAYS A LOCAL FILE ───────────────────────────────────
 * even for a series whose only cover is a URL in `.aio_series.json`: CoverStore
 * downloads that into the cover cache (with the hotlink `Referer` those CDNs
 * need) and hands back a path. Coil 3 keeps network support in a separate
 * `coil-network-*` artifact this app does not depend on, so an http URL handed
 * to AsyncImage here would have no fetcher and fail silently — do not "simplify"
 * this by passing the URL through.
 *
 * Cross-file: core/CoverStore.kt (resolution, the PDF renderer, the remote
 * fetch), aio_android.library_cover (archive extraction).
 */
/** Long enough to read as a reveal over the monogram, short enough not to lag a scroll. */
private const val CROSSFADE_MS = 220

@Composable
fun CoverArt(
    series: LibrarySeries,
    modifier: Modifier = Modifier,
    shape: Shape = MaterialTheme.shapes.medium,
    monogramSize: TextUnit = 26.sp,
) {
    val context = LocalContext.current
    var path by remember(series.folder, series.modifiedAt) { mutableStateOf<String?>(null) }

    LaunchedEffect(series.folder, series.modifiedAt) {
        path = CoverStore.resolve(context, series)
    }

    val primary = MaterialTheme.colorScheme.primary
    Box(
        modifier
            .clip(shape)
            .background(
                Brush.linearGradient(
                    listOf(primary.copy(alpha = 0.20f), primary.copy(alpha = 0.05f)),
                ),
            )
            .border(1.dp, MaterialTheme.aio.border.copy(alpha = 0.6f), shape),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            initialsOf(series.name),
            style = MaterialTheme.typography.titleLarge.copy(
                fontSize = monogramSize,
                fontWeight = FontWeight.Bold,
            ),
            color = primary.copy(alpha = 0.6f),
        )

        path?.let { resolved ->
            AsyncImage(
                model = ImageRequest.Builder(context)
                    // Uri.fromFile, not string concatenation: series folders are
                    // named after their titles, so the path routinely contains
                    // spaces and punctuation that a raw "file://$path" would
                    // leave unencoded and Coil would fail to open.
                    .data(Uri.fromFile(File(resolved)).toString())
                    // Milliseconds, NOT an AnimationSpec — Coil's crossfade
                    // extension takes an Int, and handing it a tween() silently
                    // resolves to a different overload that will not build.
                    .crossfade(CROSSFADE_MS)
                    .build(),
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize().clip(shape),
            )
        }
    }
}
