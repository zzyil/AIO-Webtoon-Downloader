package com.aio.downloader.ui.screens

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.Public
import androidx.compose.material.icons.filled.Save
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aio.downloader.core.ChapterDirEntry
import com.aio.downloader.core.DownloadRepository
import com.aio.downloader.core.FacetGroup
import com.aio.downloader.core.BookMetadata
import com.aio.downloader.core.LibraryRepository
import com.aio.downloader.core.LibrarySeries
import com.aio.downloader.core.MetadataRepository
import com.aio.downloader.core.SeriesFiles
import com.aio.downloader.core.UpdateResult
import com.aio.downloader.core.facetKey
import com.aio.downloader.core.firstPageInChapterDir
import com.aio.downloader.core.formatSize
import com.aio.downloader.core.metadataErrorMessage
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.AioCard
import com.aio.downloader.ui.components.AioTextField
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.CheckRow
import com.aio.downloader.ui.components.Collapsible
import com.aio.downloader.ui.components.CoverArt
import com.aio.downloader.ui.components.FieldLabel
import com.aio.downloader.ui.components.Hairline
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.components.Pill
import com.aio.downloader.ui.components.PillTone
import com.aio.downloader.ui.components.ProgressTrack
import com.aio.downloader.ui.components.SectionHeader
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.AioText
import com.aio.downloader.ui.theme.aio
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import com.aio.downloader.core.Aio
import java.io.File
import java.text.DateFormat
import java.util.Date

/**
 * One series: what it is, whether it has new chapters, and what is on disk.
 *
 * Port of LibraryTab.jsx's DetailView, including its embedded-metadata editor
 * (added in M7 — see [MetadataSection], which is collapsed by default because
 * it is a form and this screen is a summary).
 *
 * ── THE UPDATE CHECK IS THE POINT OF THIS SCREEN ───────────────────────────
 * so it gets the full-width primary button and the panel underneath, and the
 * destructive action is two taps behind a colour change. The desktop reaches
 * the same arrangement; on a phone the stakes are higher because the whole row
 * is a touch target and Delete has no undo.
 */
@Composable
fun SeriesDetailScreen(
    series: LibrarySeries,
    onBack: () -> Unit,
    /** Queue the given `--chapters` range for this series. */
    onQueueUpdate: (String) -> Unit,
    onFilterByFacet: (FacetGroup, String) -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    BackHandler(onBack = onBack)

    val updates by LibraryRepository.updates.collectAsStateWithLifecycle()
    val checking by LibraryRepository.checking.collectAsStateWithLifecycle()
    val activeRun by DownloadRepository.active.collectAsStateWithLifecycle()

    var files by remember(series.folder) { mutableStateOf(SeriesFiles()) }
    var deleteArmed by remember(series.folder) { mutableStateOf(false) }
    var deleteError by remember(series.folder) { mutableStateOf<String?>(null) }
    var showSpoilers by remember(series.folder) { mutableStateOf(false) }

    LaunchedEffect(series.folder) {
        files = LibraryRepository.files(context, series.folder)
    }

    // Auto-disarm, so a Delete button left armed cannot be hit by a stray tap
    // minutes later. Same 4s window the desktop uses.
    LaunchedEffect(deleteArmed) {
        if (deleteArmed) {
            delay(4000)
            deleteArmed = false
        }
    }

    Column(Modifier.fillMaxSize()) {
        DetailHeader(title = series.name, onBack = onBack)

        Column(
            Modifier
                .weight(1f)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp),
        ) {
            Spacer(Modifier.height(16.dp))
            HeroRow(series, formatBadges(series, files))

            if (series.genres.isNotEmpty() || series.tags.isNotEmpty()) {
                Spacer(Modifier.height(14.dp))
                MetaChips(
                    series = series,
                    showSpoilers = showSpoilers,
                    onRevealSpoilers = { showSpoilers = true },
                    onFilterByFacet = onFilterByFacet,
                )
            }

            Spacer(Modifier.height(16.dp))
            StatsRow(series, files)

            Spacer(Modifier.height(18.dp))
            UpdateSection(
                series = series,
                result = updates[series.folder],
                busy = series.folder in checking,
                downloadRunning = activeRun != null,
                onCheck = { scope.launch { LibraryRepository.checkOne(context, series.folder) } },
                onDownload = { range ->
                    onQueueUpdate(range)
                    LibraryRepository.dismissUpdate(series.folder)
                },
            )

            if (series.synopsis.isNotBlank()) {
                SectionHeader("Synopsis")
                Text(
                    series.synopsis,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.aio.mutedForeground,
                )
            }

            FilesSection(files)

            MetadataSection(files = files, downloadRunning = activeRun != null)

            SectionHeader("Manage")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (series.url.isNotBlank()) {
                    AioButton(
                        text = "Source",
                        icon = Icons.Filled.Public,
                        tone = ButtonTone.Outline,
                        compact = true,
                        onClick = { openExternally(context, Uri.parse(series.url)) },
                    )
                }
                AioButton(
                    text = if (deleteArmed) "Tap again to delete" else "Delete",
                    icon = Icons.Filled.Delete,
                    tone = if (deleteArmed) ButtonTone.Danger else ButtonTone.Outline,
                    compact = true,
                    onClick = {
                        if (!deleteArmed) {
                            deleteArmed = true
                            return@AioButton
                        }
                        deleteArmed = false
                        scope.launch {
                            val failure = LibraryRepository.delete(context, series.folder)
                            if (failure == null) onBack() else deleteError = failure
                        }
                    },
                )
            }
            deleteError?.let {
                Spacer(Modifier.height(8.dp))
                HelpText(it, tone = MaterialTheme.colorScheme.error)
            }

            Spacer(Modifier.height(10.dp))
            FolderPathRow(series.folder)

            Spacer(Modifier.height(28.dp))
        }
    }
}

// ── header + hero ──────────────────────────────────────────────────────────

@Composable
private fun DetailHeader(title: String, onBack: () -> Unit) {
    Column {
        Row(
            Modifier
                .fillMaxWidth()
                .background(MaterialTheme.aio.card.copy(alpha = 0.3f))
                .padding(start = 6.dp, end = 20.dp, top = 6.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                Icons.AutoMirrored.Filled.ArrowBack,
                contentDescription = "Back to the library",
                tint = MaterialTheme.colorScheme.onBackground,
                modifier = Modifier
                    .clip(CircleShape)
                    .clickable(onClick = onBack)
                    .padding(12.dp)
                    .size(19.dp),
            )
            Spacer(Modifier.width(4.dp))
            Text(
                title,
                style = MaterialTheme.typography.titleMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
        Hairline()
    }
}

/**
 * What formats this series is actually STORED in, derived from the files on
 * disk rather than from the recorded `format` param.
 *
 * Port of LibraryTab.jsx:getEntryFormats, and the reason it works that way is
 * worth keeping: `format` records what the last RUN was asked for, which is not
 * the same as what is in the folder. A download that was cancelled, changed
 * format, or ran twice leaves the two disagreeing — and the folder is the one
 * telling the truth about what you can open. Falls back to the recorded value
 * only while the file list is still loading.
 */
private fun formatBadges(series: LibrarySeries, files: SeriesFiles): List<String> {
    val onDisk = files.files.map { it.ext.uppercase() }.filter { it.isNotEmpty() }.distinct()
    if (onDisk.isNotEmpty()) return onDisk
    if (files.chapterDirs.isNotEmpty()) return listOf("IMAGES")
    return listOfNotNull(series.format.uppercase().takeIf { series.format.isNotBlank() && series.format != "?" })
}

@Composable
private fun HeroRow(series: LibrarySeries, formats: List<String>) {
    Row {
        CoverArt(
            series = series,
            modifier = Modifier.width(124.dp).aspectRatio(3f / 4f),
            monogramSize = 34.sp,
        )
        Spacer(Modifier.width(16.dp))
        Column(Modifier.weight(1f)) {
            Text(
                series.name,
                style = MaterialTheme.typography.titleLarge,
                maxLines = 4,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                if (series.status.isNotBlank()) {
                    // Sites disagree about case — MangaDex says "ongoing",
                    // AniList "RELEASING", Madara "Completed". The grid card
                    // uppercases; here it sits beside a format pill, so a
                    // sentence-case word is what reads as deliberate.
                    Pill(
                        series.status.replaceFirstChar { it.uppercase() },
                        tone = statusTone(series.status),
                    )
                }
                formats.forEach { Pill(it) }
            }
            if (series.authors.isNotEmpty()) {
                Spacer(Modifier.height(9.dp))
                HelpText(series.authors.joinToString(", "))
            }
            if (series.site.isNotBlank()) {
                Spacer(Modifier.height(5.dp))
                Text(
                    series.site,
                    style = AioText.numericSmall,
                    color = MaterialTheme.aio.mutedForeground,
                )
            }
        }
    }
}

/**
 * Ongoing reads blue, finished reads green — the desktop's STATUS_COLORS, which
 * covers both the AniList spellings and the site ones because handlers disagree
 * ("Releasing" vs "Ongoing", "Finished" vs "Completed").
 */
@Composable
private fun statusTone(status: String): PillTone = when (status.lowercase()) {
    "ongoing", "releasing" -> PillTone.Info
    "completed", "finished" -> PillTone.Success
    "hiatus" -> PillTone.Warning
    "cancelled", "canceled" -> PillTone.Danger
    else -> PillTone.Neutral
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun MetaChips(
    series: LibrarySeries,
    showSpoilers: Boolean,
    onRevealSpoilers: () -> Unit,
    onFilterByFacet: (FacetGroup, String) -> Unit,
) {
    val hiddenSpoilers = series.tags.count { it.spoiler && !showSpoilers }

    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        series.genres.forEach { genre ->
            MetaChip(genre) { onFilterByFacet(FacetGroup.Genres, facetKey(genre)) }
        }
        series.tags.filter { showSpoilers || !it.spoiler }.forEach { tag ->
            MetaChip(tag.name, muted = true) { onFilterByFacet(FacetGroup.Tags, facetKey(tag.name)) }
        }
        if (hiddenSpoilers > 0) {
            Text(
                "$hiddenSpoilers spoiler tag${if (hiddenSpoilers == 1) "" else "s"}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.aio.mutedForeground,
                modifier = Modifier
                    .clip(CircleShape)
                    .border(1.dp, MaterialTheme.aio.border, CircleShape)
                    .clickable(onClick = onRevealSpoilers)
                    .padding(horizontal = 10.dp, vertical = 6.dp),
            )
        }
    }
}

/**
 * A tappable metadata chip. Tapping applies it as a library filter and returns
 * to the grid — so there is deliberately no selected state; this chip is never
 * drawn as on.
 */
@Composable
private fun MetaChip(label: String, muted: Boolean = false, onClick: () -> Unit) {
    Text(
        label,
        style = MaterialTheme.typography.labelSmall,
        color = if (muted) {
            MaterialTheme.aio.mutedForeground
        } else {
            MaterialTheme.colorScheme.onBackground
        },
        maxLines = 1,
        modifier = Modifier
            .clip(CircleShape)
            .background(MaterialTheme.aio.secondary.copy(alpha = 0.5f))
            .border(1.dp, MaterialTheme.aio.border.copy(alpha = 0.7f), CircleShape)
            .clickable(onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 6.dp),
    )
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun StatsRow(series: LibrarySeries, files: SeriesFiles) {
    val items = buildList {
        if (series.chapters > 0) add("${series.chapters} chapters")
        if (series.latestChapter.isNotBlank()) add("latest ${series.latestChapter}")
        val count = files.files.size.takeIf { it > 0 } ?: series.files
        if (count > 0) add("$count file${if (count == 1) "" else "s"}")
        if (series.size > 0) add(formatSize(series.size))
        if (series.modifiedAt > 0) {
            add(
                DateFormat.getDateInstance(DateFormat.MEDIUM)
                    .format(Date(series.modifiedAt)),
            )
        }
    }
    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        items.forEach {
            Text(
                it,
                style = AioText.numericSmall,
                color = MaterialTheme.aio.mutedForeground,
            )
        }
    }
}

// ── updates ────────────────────────────────────────────────────────────────

@Composable
private fun UpdateSection(
    series: LibrarySeries,
    result: UpdateResult?,
    busy: Boolean,
    downloadRunning: Boolean,
    onCheck: () -> Unit,
    onDownload: (String) -> Unit,
) {
    SectionHeader("Updates")

    if (!series.checkable) {
        // The desktop offers a field to type the URL in. Skipped here on
        // purpose: a series with no saved metadata was placed by hand, and
        // typing a URL on a phone to repair one is worse than re-downloading it
        // — which also restores every other field this one is missing.
        HelpText(
            "No source URL was saved for this series, so there's nothing to check " +
                "against. Downloading it again from the New tab will record one.",
        )
        return
    }

    AioButton(
        text = when {
            busy -> "Checking…"
            result != null -> "Check again"
            else -> "Check for new chapters"
        },
        icon = Icons.Filled.Refresh,
        enabled = !busy && !downloadRunning,
        onClick = onCheck,
        modifier = Modifier.fillMaxWidth(),
    )

    if (downloadRunning) {
        Spacer(Modifier.height(6.dp))
        // Pre-empting the engine lock. Python would answer `engine_busy` anyway,
        // but a disabled button with a reason beats a button that fails.
        HelpText("A download is running — checks share the same engine and wait their turn.")
    }

    AnimatedVisibility(busy, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
        Column {
            Spacer(Modifier.height(10.dp))
            ProgressTrack(fraction = null)
            Spacer(Modifier.height(6.dp))
            HelpText("Asking ${series.site.ifBlank { "the site" }} for its chapter list…")
        }
    }

    AnimatedVisibility(
        visible = result != null && !busy,
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        val outcome = remember(result) { result } ?: return@AnimatedVisibility
        Column {
            Spacer(Modifier.height(10.dp))
            when {
                !outcome.ok -> AioCard(
                    borderColor = MaterialTheme.colorScheme.error.copy(alpha = 0.35f),
                    contentPadding = PaddingValues(12.dp),
                ) {
                    HelpText(outcome.errorText, tone = MaterialTheme.colorScheme.error)
                }

                outcome.hasUpdates -> AioCard(
                    borderColor = MaterialTheme.aio.warning.copy(alpha = 0.45f),
                    contentPadding = PaddingValues(12.dp),
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "${outcome.newChapters.size} new " +
                                "chapter${if (outcome.newChapters.size == 1) "" else "s"}",
                            style = MaterialTheme.typography.titleSmall,
                            color = MaterialTheme.aio.warning,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            "${outcome.downloaded} / ${outcome.total}",
                            style = AioText.numericSmall,
                            color = MaterialTheme.aio.mutedForeground,
                        )
                    }
                    Spacer(Modifier.height(5.dp))
                    // The exact spec that will be passed to --chapters. Showing
                    // it means a surprising range is visible BEFORE the download
                    // starts rather than afterwards in the log.
                    Text(
                        outcome.range,
                        style = AioText.numeric,
                        color = MaterialTheme.colorScheme.onBackground,
                    )
                    Spacer(Modifier.height(11.dp))
                    AioButton(
                        text = "Download ${outcome.newChapters.size}",
                        icon = Icons.Filled.Download,
                        compact = true,
                        onClick = { onDownload(outcome.range) },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }

                else -> Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(
                        Icons.Filled.Check,
                        contentDescription = null,
                        tint = MaterialTheme.aio.success,
                        modifier = Modifier.size(15.dp),
                    )
                    Spacer(Modifier.width(7.dp))
                    HelpText(
                        "Up to date — ${outcome.total} on the site, " +
                            "${outcome.downloaded} here.",
                        tone = MaterialTheme.aio.success,
                    )
                }
            }
        }
    }
}

// ── files ──────────────────────────────────────────────────────────────────

@Composable
private fun FilesSection(files: SeriesFiles) {
    if (files.isEmpty) return
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var failure by remember { mutableStateOf<String?>(null) }

    SectionHeader(if (files.files.isEmpty()) "Chapters" else "Files")
    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
        files.files.forEach { file ->
            FileRow(
                badge = file.ext.uppercase(),
                name = file.name,
                detail = formatSize(file.size),
                onClick = { failure = openBook(context, file.path, file.ext) },
            )
        }
        files.chapterDirs.forEach { dir ->
            FileRow(
                badge = "IMG",
                name = dir.name.replace('_', ' '),
                detail = "${dir.images} img · ${formatSize(dir.size)}",
                onClick = { scope.launch { failure = openChapterFolder(context, dir) } },
            )
        }
    }
    failure?.let {
        Spacer(Modifier.height(8.dp))
        HelpText(it, tone = MaterialTheme.aio.warning)
    }
}

/**
 * The literal series path, and a way to take it with you.
 *
 * The path is printed because the library can be anywhere — the app's own
 * external-files folder by default, wherever Settings points otherwise — and
 * "where did my files go" is otherwise unanswerable from inside the app.
 *
 * Tapping COPIES it rather than opening it. There is no reliable "reveal in a
 * file manager" intent on Android: `ACTION_VIEW` on a directory has no MIME any
 * app registers for, and the `DocumentsContract` trick that sometimes opens
 * Files only works for primary shared storage and silently does nothing
 * elsewhere. A path on the clipboard works in every file manager, in Komikku's
 * own folder picker, and in adb — so it is the affordance that always pays off
 * instead of the one that usually does not.
 */
@Composable
private fun FolderPathRow(folder: String) {
    val context = LocalContext.current
    var copied by remember(folder) { mutableStateOf(false) }

    LaunchedEffect(copied) {
        if (copied) {
            delay(2500)
            copied = false
        }
    }

    Row(
        Modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.small)
            .clickable { copied = copyToClipboard(context, "Series folder", folder) }
            .heightIn(min = 40.dp)
            .padding(horizontal = 6.dp, vertical = 8.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Icon(
            Icons.Filled.Folder,
            contentDescription = null,
            tint = MaterialTheme.aio.mutedForeground,
            modifier = Modifier.size(13.dp).padding(top = 1.dp),
        )
        Spacer(Modifier.width(6.dp))
        Text(
            folder,
            style = AioText.numericSmall,
            color = MaterialTheme.aio.mutedForeground,
            modifier = Modifier.weight(1f),
        )
        AnimatedVisibility(copied, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
            Text(
                "copied",
                style = AioText.numericSmall,
                color = MaterialTheme.colorScheme.primary,
            )
        }
    }
}

@Composable
private fun FileRow(badge: String, name: String, detail: String, onClick: (() -> Unit)?) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.small)
            .background(MaterialTheme.aio.card.copy(alpha = 0.5f))
            .border(1.dp, MaterialTheme.aio.border.copy(alpha = 0.6f), MaterialTheme.shapes.small)
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
            .heightIn(min = 46.dp)
            .padding(horizontal = 10.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Pill(badge, mono = true)
        Spacer(Modifier.width(9.dp))
        Text(
            name,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Spacer(Modifier.width(8.dp))
        Text(
            detail,
            style = AioText.numericSmall,
            color = MaterialTheme.aio.mutedForeground,
            maxLines = 1,
        )
    }
}

// ── embedded metadata ──────────────────────────────────────────────────────

/**
 * Edit the metadata stored INSIDE the archives.
 *
 * ── WHY IT IS WORTH HAVING ON A PHONE ──────────────────────────────────────
 * These fields are what Komikku and Mihon display. A junk site author ("Great
 * H" is a real asura credit), a title carrying a scanlation suffix, a missing
 * publisher — the reader shows all of it verbatim and has no way to change it,
 * because the values live in the archive rather than in any app's database.
 *
 * ── THE TWO CHOICES THAT MATTER ────────────────────────────────────────────
 * 1. It loads from the FIRST editable file and, by default, writes only there.
 *    A komikku series is one archive per chapter and each carries its own
 *    ComicInfo.xml, so "apply to all" is a separate, explicit opt-in — with 300
 *    chapters it rewrites 300 archives, which is minutes of work and a lot of
 *    bytes moved.
 * 2. There is no cover picker, where the desktop has one. Choosing an image
 *    needs a system file picker returning a `content://` URI, and the Python
 *    that would consume it cannot open one (the same constraint that ruled out
 *    SAF for the library root). The series cover on this platform comes from
 *    `cover.jpg` and CoverStore, which is the picture the grid actually shows.
 *
 * Cross-file: core/MetadataRepository.kt, aio_android.write_book_metadata,
 * UI-source/src/components/LibraryTab.jsx:MetadataEditorPanel (the same fields).
 */
@Composable
private fun MetadataSection(files: SeriesFiles, downloadRunning: Boolean) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    val editable = remember(files) {
        files.files.filter { it.ext.lowercase() in MetadataRepository.EDITABLE_EXTS }
    }
    if (editable.isEmpty()) return
    val primary = editable.first()

    var loaded by remember(primary.path) { mutableStateOf<BookMetadata?>(null) }
    var form by remember(primary.path) { mutableStateOf(BookMetadata()) }
    var loadFailed by remember(primary.path) { mutableStateOf(false) }
    var applyToAll by remember(primary.path) { mutableStateOf(false) }
    var saving by remember(primary.path) { mutableStateOf(false) }
    var notice by remember(primary.path) { mutableStateOf<String?>(null) }
    var noticeIsError by remember(primary.path) { mutableStateOf(false) }

    LaunchedEffect(primary.path) {
        val read = MetadataRepository.read(context, primary.path)
        loaded = read
        form = read ?: BookMetadata()
        loadFailed = read == null
    }
    LaunchedEffect(notice) {
        if (notice != null) {
            delay(5000)
            notice = null
        }
    }

    // Compared against what was READ, not against blank: the interesting
    // question is "has the user changed anything", and a series that already
    // had a title would otherwise look dirty the moment it loaded.
    val dirty = loaded != null && form != loaded

    SectionHeader("Metadata")
    Collapsible(
        title = "Edit embedded metadata",
        subtitle = "What Komikku and Mihon show for this series",
    ) {
        if (loadFailed) {
            HelpText(
                "Couldn't read ${primary.name}. It may be an archive written by another " +
                    "tool, or still being downloaded.",
                tone = MaterialTheme.aio.warning,
            )
            return@Collapsible
        }

        MetadataField("Title", form.title) { form = form.copy(title = it) }
        MetadataField("Writers", form.writers, "Comma-separated") {
            form = form.copy(writers = it)
        }
        MetadataField("Artists", form.pencillers, "Comma-separated") {
            form = form.copy(pencillers = it)
        }
        MetadataField("Genres", form.genres, "Comma-separated") { form = form.copy(genres = it) }
        MetadataField("Publisher", form.publisher) { form = form.copy(publisher = it) }
        MetadataField("Synopsis", form.synopsis, multiline = true) {
            form = form.copy(synopsis = it)
        }

        if (editable.size > 1) {
            Spacer(Modifier.height(6.dp))
            CheckRow(
                label = "Apply to all ${editable.size} files",
                checked = applyToAll,
                onCheckedChange = { applyToAll = it },
            )
            HelpText(
                if (applyToAll) {
                    "Rewrites every archive in place — a few seconds each on a big series."
                } else {
                    "Only ${primary.name} is changed."
                },
            )
        }

        Spacer(Modifier.height(12.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            AioButton(
                text = when {
                    saving -> "Saving…"
                    applyToAll && editable.size > 1 -> "Save to ${editable.size} files"
                    else -> "Save"
                },
                icon = Icons.Filled.Save,
                compact = true,
                // Blocked while a download runs, and said so BEFORE the tap:
                // Python refuses this outright (the downloader is writing into
                // the same library), and a button that only fails on press is
                // worse than one that explains itself.
                enabled = dirty && !saving && !downloadRunning,
                onClick = {
                    saving = true
                    scope.launch {
                        val targets =
                            if (applyToAll) editable.map { it.path } else listOf(primary.path)
                        val result = MetadataRepository.write(context, targets, form)
                        saving = false
                        when {
                            result.error != null -> {
                                notice = metadataErrorMessage(result.error)
                                noticeIsError = true
                            }
                            result.failed.isNotEmpty() -> {
                                notice = "Saved ${result.written}, couldn't write " +
                                    "${result.failed.size} (${result.failed.first()}…)"
                                noticeIsError = true
                            }
                            else -> {
                                notice = if (result.written > 1) {
                                    "Saved to ${result.written} files"
                                } else {
                                    "Saved"
                                }
                                noticeIsError = false
                                // The saved form becomes the new baseline, so
                                // the button goes clean instead of inviting the
                                // same write again.
                                loaded = form
                            }
                        }
                    }
                },
            )
            if (dirty && !saving) {
                Spacer(Modifier.width(8.dp))
                AioButton(
                    text = "Revert",
                    tone = ButtonTone.Ghost,
                    compact = true,
                    onClick = { form = loaded ?: BookMetadata() },
                )
            }
        }

        if (downloadRunning) {
            Spacer(Modifier.height(8.dp))
            HelpText(
                "A download is running. Saving is blocked until it finishes — the " +
                    "downloader is writing archives into this same library.",
                tone = MaterialTheme.aio.warning,
            )
        }

        notice?.let {
            Spacer(Modifier.height(8.dp))
            HelpText(
                it,
                tone = if (noticeIsError) {
                    MaterialTheme.aio.warning
                } else {
                    MaterialTheme.colorScheme.primary
                },
            )
        }
    }
}

@Composable
private fun MetadataField(
    label: String,
    value: String,
    hint: String? = null,
    multiline: Boolean = false,
    onChange: (String) -> Unit,
) {
    Spacer(Modifier.height(10.dp))
    FieldLabel(label)
    Spacer(Modifier.height(5.dp))
    AioTextField(
        value = value,
        onValueChange = onChange,
        singleLine = !multiline,
        minHeight = if (multiline) 84 else 44,
    )
    hint?.let {
        Spacer(Modifier.height(3.dp))
        HelpText(it)
    }
}

// ── external handoff ───────────────────────────────────────────────────────

/**
 * Hand a downloaded book to whatever can read it. Returns null on success, or a
 * message to show.
 *
 * Another app cannot open the path directly — the app-scoped default library is
 * private to us, and even a shared-storage one is easier to pass as a
 * `content://` URI with a one-shot read grant than as a path the other app may
 * have no permission for. Minting that URI is what the manifest's FileProvider
 * is for; which roots it may mint inside is `res/xml/file_paths.xml`, and a
 * library root outside all of them is the ONLY realistic cause of the throw
 * below (it was every custom root until that file gained its `root-path`).
 *
 * MIME matters more than it looks: `application/vnd.comicbook+zip` is the
 * registered CBZ type but plenty of readers only advertise `application/zip` or
 * a wildcard, so a chooser is offered and a miss is reported rather than
 * throwing.
 */
private fun openBook(
    context: Context,
    path: String,
    ext: String,
    chooserTitle: String = "Open with",
): String? {
    val file = File(path)
    if (!file.isFile) return "That file is no longer on disk."

    // CONTAINMENT, checked here because this is the last layer before a file is
    // handed to a THIRD-PARTY app.
    //
    // res/xml/file_paths.xml had to widen to a <root-path> covering /storage/
    // so a user-chosen library root could be opened at all — there is no
    // narrower element (<external-path> resolves to /storage/emulated/0, which
    // still contains this app's own Android/data tree). That widening is static
    // and cannot be narrowed per-request, so the runtime check has to live at
    // the mint site. Without it, openBook would mint a shareable URI for ANY
    // path under /storage/ — including other apps' Android/data trees and other
    // users' /storage/emulated/<N>, all readable under MANAGE_EXTERNAL_STORAGE.
    //
    // Not currently exploitable: paths reach here only from series_files /
    // firstPageInChapterDir over the scanned library. This is the same
    // defence-in-depth the codebase already standardises on rather than relying
    // on a caller invariant — aio_android._book_path_error guards the metadata
    // entry points with os.path.commonpath, and LibraryRepository states the
    // principle outright ("the containment guard lives in Python … because that
    // is the last layer before shutil.rmtree").
    //
    // canonicalFile on BOTH sides so `..` and symlinks are resolved before the
    // comparison, and the separator suffix so a sibling like `<root>-evil`
    // cannot pass a bare startsWith.
    val contained = runCatching {
        val root = Aio.resolveLibraryDir(context).canonicalFile.path + File.separator
        file.canonicalFile.path.startsWith(root)
    }.getOrDefault(false)
    if (!contained) {
        return "That file is outside the library folder, so it can't be opened from here."
    }

    val uri = runCatching {
        FileProvider.getUriForFile(context, "${context.packageName}.files", file)
    }.getOrElse {
        // Names the cause instead of the exception class, which told nobody
        // anything: the folder is outside every root the provider declares.
        return "Couldn't share that file — ${file.parent} isn't somewhere this " +
            "app is allowed to hand files to other apps."
    }

    val mime = when (ext.lowercase()) {
        "cbz" -> "application/vnd.comicbook+zip"
        "epub" -> "application/epub+zip"
        "pdf" -> "application/pdf"
        "jpg", "jpeg" -> "image/jpeg"
        "png" -> "image/png"
        "webp" -> "image/webp"
        "gif" -> "image/gif"
        else -> "*/*"
    }
    val view = Intent(Intent.ACTION_VIEW)
        .setDataAndType(uri, mime)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)

    return try {
        context.startActivity(
            Intent.createChooser(view, chooserTitle).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        null
    } catch (_: ActivityNotFoundException) {
        "No app on this device can open a .$ext file."
    }
}

/**
 * Open the first page of a raw `Chapter_N/` folder. Returns null on success, or
 * a message to show.
 *
 * ── WHY A PAGE AND NOT THE FOLDER ──────────────────────────────────────────
 * `ACTION_VIEW` on a directory is not a thing on Android: there is no MIME type
 * for "a folder of images" that a viewer can register for, and
 * `FileProvider.getUriForFile` on a directory produces a URI whose `openFile`
 * throws. So the row used to be inert with the reason spelled out beside it.
 * Handing over page 1 is the one thing that DOES work end to end — every
 * gallery on the device can take an image, and it is also how the user checks
 * "did this chapter download properly".
 *
 * What it is NOT is a reader: the grant covers exactly the URI passed, so the
 * viewer shows that page and cannot page forward to its siblings. The chooser
 * title says "first page" so nobody taps it expecting the chapter. Building an
 * actual page-through viewer is a different feature, deliberately not started
 * here.
 *
 * Cross-file: core/LibraryModels.kt (grep `firstPageInChapterDir` — the pick,
 * natural-sorted so `2.jpg` beats `10.jpg`).
 */
private suspend fun openChapterFolder(context: Context, dir: ChapterDirEntry): String? {
    val page = withContext(Dispatchers.IO) { firstPageInChapterDir(dir.path) }
        // Named the way the row above names it, not the way it is on disk.
        ?: return "${dir.name.replace('_', ' ')} has no image pages in it."
    return openBook(
        context = context,
        path = page.absolutePath,
        ext = page.extension,
        chooserTitle = "Open first page",
    )
}

/**
 * Put [text] on the clipboard. True when the caller should show its OWN
 * confirmation.
 *
 * From Android 13 the system draws a copy confirmation of its own, so a second
 * one from us is duplicate chrome for the same event. Below that there is no
 * system feedback at all and a silent tap reads as a dead control.
 */
private fun copyToClipboard(context: Context, label: String, text: String): Boolean {
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
        ?: return false
    clipboard.setPrimaryClip(ClipData.newPlainText(label, text))
    return Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
}

private fun openExternally(context: Context, uri: Uri) {
    runCatching {
        context.startActivity(
            Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    }
}
