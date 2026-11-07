package com.aio.downloader.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Verified
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aio.downloader.core.SearchCandidate
import com.aio.downloader.core.SearchOutcome
import com.aio.downloader.core.SearchRepository
import com.aio.downloader.core.SearchSource
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import coil3.request.crossfade
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.AioCard
import com.aio.downloader.ui.components.AioTextField
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.components.CalloutTone
import com.aio.downloader.ui.components.Pill
import com.aio.downloader.ui.components.StatusCallout
import com.aio.downloader.ui.theme.aio
import kotlin.math.roundToInt

/**
 * Cross-site search: type a title, get one card per SERIES, expand to choose
 * which SITE to download it from.
 *
 * ── WHY THE RESULT IS GROUPED BY SERIES, NOT BY SITE ───────────────────────
 * The orchestrator already merges hits across ~40 sites into one candidate per
 * series and ranks the sources inside it (`sites/search_orchestrator.py`). A
 * flat list of 200 rows would throw that work away and make the user do the
 * deduplication by eye. So the card is the series; the sources are a detail you
 * open only when you disagree with the ranking.
 *
 * ── ONLY A MEASURED RATING IS A NUMBER ─────────────────────────────────────
 * A source's rating is measured only when its pages were compared with other
 * sites' copies of the same pages ([SearchSource.measuredRating]); otherwise
 * [RatingLine] shows the reason ("not compared", "unreachable", …) where the
 * number would be (user decision 2026-10-21, the desktop's rule in
 * lib/qualityDisplay.js). Presenting a seed prior as a measurement is the one
 * thing this screen must not do.
 *
 * ── SEARCH IS SLOW AND THAT IS NORMAL ──────────────────────────────────────
 * 40-100 seconds, because it fans out to every site and then fetches sample
 * pages to rate them. The progress state says what it is doing rather than
 * showing a bare spinner, because a silent minute reads as a hang.
 *
 * Cross-file: SearchRepository.kt (state), SearchModels.kt (shapes),
 * aio_android.search (the engine call), UI-source/src/components/
 * SearchSourceCard.jsx + SearchTab.jsx (the behaviour being ported).
 */
@Composable
fun SearchScreen(
    onDownload: (String) -> Unit,
    settingsJson: () -> String?,
) {
    val context = LocalContext.current
    val outcome by SearchRepository.outcome.collectAsStateWithLifecycle()
    val searching by SearchRepository.searching.collectAsStateWithLifecycle()
    val pending by SearchRepository.pendingQuery.collectAsStateWithLifecycle()

    var query by rememberSaveable { mutableStateOf("") }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.weight(1f)) {
                AioTextField(
                    value = query,
                    onValueChange = { query = it },
                    placeholder = "Series title",
                    enabled = !searching,
                    singleLine = true,
                )
            }
            Spacer(Modifier.width(8.dp))
            AioButton(
                text = if (searching) "Searching" else "Search",
                onClick = { SearchRepository.search(context, query, settingsJson()) },
                enabled = !searching && query.isNotBlank(),
            )
        }

        // The soft half of the engine guard: say it before the tap, not after.
        // Losing the race here is harmless — Python still returns engine_busy.
        if (!searching && SearchRepository.busyWithDownload()) {
            Spacer(Modifier.height(12.dp))
            StatusCallout(
                title = "A download is running",
                body = "Search and downloads share one engine, so searching now " +
                    "will be refused until the download finishes.",
                icon = Icons.Filled.Warning,
                tone = CalloutTone.Warning,
            )
        }

        Spacer(Modifier.height(16.dp))

        when {
            searching -> SearchingState(pending)
            outcome == null -> IdleState()
            else -> ResultsState(outcome!!, onDownload)
        }
    }
}

@Composable
private fun IdleState() {
    Column(
        Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(48.dp))
        Icon(
            Icons.Filled.Search,
            contentDescription = null,
            tint = MaterialTheme.aio.mutedForeground,
            modifier = Modifier.size(40.dp),
        )
        Spacer(Modifier.height(12.dp))
        Text(
            "Search every supported site at once",
            style = MaterialTheme.typography.titleSmall,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Spacer(Modifier.height(6.dp))
        HelpText(
            "Results are grouped by series, with each site rated so you can pick " +
                "the best copy. Expect this to take up to a minute.",
        )
    }
}

@Composable
private fun SearchingState(query: String?) {
    Column(
        Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(48.dp))
        CircularProgressIndicator(
            modifier = Modifier.size(28.dp),
            color = MaterialTheme.colorScheme.primary,
            strokeWidth = 3.dp,
        )
        Spacer(Modifier.height(14.dp))
        Text(
            if (query.isNullOrBlank()) "Searching" else "Searching for “$query”",
            style = MaterialTheme.typography.titleSmall,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Spacer(Modifier.height(6.dp))
        // Naming the two phases is what keeps a 60-second wait from reading as
        // a hang; the Logs tab carries the per-site detail.
        HelpText("Asking every site, then sampling pages to rate image quality.")
    }
}

@Composable
private fun ResultsState(outcome: SearchOutcome, onDownload: (String) -> Unit) {
    outcome.error?.let { message ->
        StatusCallout(
            title = "Search failed",
            body = message,
            icon = Icons.Filled.Warning,
            tone = CalloutTone.Danger,
        )
        return
    }

    if (outcome.isEmpty) {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(40.dp))
            Text(
                "No results for “${outcome.query}”",
                style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Spacer(Modifier.height(6.dp))
            HelpText("Try the romanized title, or a shorter part of it.")
        }
        return
    }

    LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item {
            HelpText(
                "${outcome.candidates.size} " +
                    (if (outcome.candidates.size == 1) "series" else "series") +
                    " found for “${outcome.query}”",
            )
        }
        items(outcome.candidates, key = { it.canonicalTitle + it.sources.first().url }) { candidate ->
            CandidateCard(candidate, onDownload)
        }
    }
}

@Composable
private fun CandidateCard(candidate: SearchCandidate, onDownload: (String) -> Unit) {
    var expanded by rememberSaveable(candidate.canonicalTitle) { mutableStateOf(false) }
    val best = candidate.best ?: return

    AioCard {
        Column(Modifier.fillMaxWidth()) {
            Row(Modifier.fillMaxWidth().clickable { expanded = !expanded }) {
                RemoteCover(
                    url = candidate.cover,
                    title = candidate.canonicalTitle,
                    modifier = Modifier.width(56.dp).height(80.dp),
                )
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(
                        candidate.canonicalTitle,
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                    candidate.canonicalYear?.let {
                        Text(
                            it.toString(),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.aio.mutedForeground,
                        )
                    }
                    Spacer(Modifier.height(6.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Pill(
                            text = "${candidate.siteCount} " +
                                if (candidate.siteCount == 1) "site" else "sites",
                        )
                        candidate.maxChapters?.let { Pill(text = "$it ch") }
                        if (best.isOfficial) Pill(text = "Official")
                    }
                }
            }

            Spacer(Modifier.height(10.dp))

            if (expanded) {
                // Sources are already ranked by the orchestrator; the list is
                // shown in that order so "first" means "our pick".
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    candidate.sources.forEach { source ->
                        SourceRow(source, onDownload)
                    }
                }
            } else {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(
                            best.site,
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurface,
                        )
                        RatingLine(best)
                    }
                    AioButton(
                        text = "Download",
                        onClick = { onDownload(best.url) },
                    )
                }
                Spacer(Modifier.height(4.dp))
                Text(
                    if (candidate.siteCount > 1) {
                        "Tap to compare ${candidate.siteCount} sources"
                    } else {
                        "Tap for details"
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.aio.mutedForeground,
                )
            }
        }
    }
}

@Composable
private fun SourceRow(source: SearchSource, onDownload: (String) -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(MaterialTheme.aio.secondary)
            .padding(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    source.site,
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                if (source.isOfficial) {
                    Spacer(Modifier.width(4.dp))
                    Icon(
                        Icons.Filled.Verified,
                        contentDescription = "Official source",
                        tint = MaterialTheme.aio.success,
                        modifier = Modifier.size(14.dp),
                    )
                }
            }
            RatingLine(source)
            source.sameNote?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.aio.mutedForeground,
                )
            }
            source.editionNote?.let {
                Text(
                    "Other edition: $it",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.aio.warning,
                )
            }
            if (source.dmcaLikely) {
                Text(
                    "May be missing chapters (DMCA)",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.aio.warning,
                )
            }
        }
        AioButton(text = "Get", onClick = { onDownload(source.url) })
    }
}

/**
 * A cover straight off the site, with a monogram underneath while it loads or
 * if it never does.
 *
 * Separate from [com.aio.downloader.ui.components.CoverArt], which takes a
 * LibrarySeries and resolves a LOCAL file through CoverStore. Nothing is on
 * disk yet here — the series has not been downloaded — so this is a plain
 * remote fetch, and many sites simply do not return a usable cover in search
 * results, which is why the monogram is the base layer rather than a fallback
 * branch.
 */
@Composable
private fun RemoteCover(url: String?, title: String, modifier: Modifier = Modifier) {
    val shape = RoundedCornerShape(6.dp)
    val primary = MaterialTheme.colorScheme.primary
    Box(
        modifier
            .clip(shape)
            .background(primary.copy(alpha = 0.14f)),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            title.trim().take(1).uppercase().ifBlank { "?" },
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold,
            color = primary.copy(alpha = 0.55f),
        )
        if (!url.isNullOrBlank()) {
            AsyncImage(
                model = ImageRequest.Builder(LocalContext.current)
                    .data(url)
                    // Milliseconds, NOT an AnimationSpec — Coil's crossfade
                    // extension takes an Int and a tween() resolves to a
                    // different overload that will not build. Same trap
                    // CoverArt.kt documents.
                    .crossfade(220)
                    .build(),
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize().clip(shape),
            )
        }
    }
}

/**
 * "Quality 92%", or "Quality: not compared" when the rating was not measured
 * (see the screen header for why a seed number is never shown), then chapters
 * and title match.
 */
@Composable
private fun RatingLine(source: SearchSource) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            buildString {
                val r = source.measuredRating
                if (r != null) {
                    append("Quality ")
                    append((r * 100).roundToInt())
                    append("%")
                } else {
                    append("Quality: ")
                    append(source.qualityLabel)
                }
                source.chapterCount?.let { append("  ·  $it ch") }
                append("  ·  ")
                append((source.titleMatch * 100).roundToInt())
                append("% match")
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.aio.mutedForeground,
        )
    }
}
