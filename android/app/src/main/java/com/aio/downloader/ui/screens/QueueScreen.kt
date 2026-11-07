package com.aio.downloader.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.PauseCircle
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Schedule
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
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
import com.aio.downloader.DownloadService
import com.aio.downloader.core.ActiveRun
import com.aio.downloader.core.DownloadJob
import com.aio.downloader.core.DownloadRepository
import com.aio.downloader.core.ResumableRun
import com.aio.downloader.core.ResumeRepository
import com.aio.downloader.core.RunOutcome
import com.aio.downloader.core.RunRecord
import com.aio.downloader.core.finalFileDetail
import com.aio.downloader.core.formatElapsed
import com.aio.downloader.core.formatEta
import com.aio.downloader.core.phaseLabel
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.AioCard
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.ChoiceOption
import com.aio.downloader.ui.components.FieldLabel
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.components.Pill
import com.aio.downloader.ui.components.PillTone
import com.aio.downloader.ui.components.ProgressTrack
import com.aio.downloader.ui.components.SectionHeader
import com.aio.downloader.ui.components.SegmentedChoice
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.AioText
import com.aio.downloader.ui.theme.aio
import kotlinx.coroutines.launch

/**
 * What is running, what is waiting, what just finished.
 *
 * ── THE NUMBERS HERE ARE NOT COMPUTED HERE ─────────────────────────────────
 * `processed`, `total` and `etaMs` are stamped by Python at the emit site
 * (`aio_android._EtaEstimator`) and travel through DownloadService's poll loop
 * into `DownloadRepository`. This screen is a pure function of the last event
 * received. Recomputing an ETA from what arrives at THIS layer would be
 * measuring the 700ms poll timer, not the download: two chapters finishing
 * inside one interval would read as one ~0ms sample plus one full-interval
 * sample. The desktop has the same note on downloader.js:applyChapterEta.
 */
@Composable
fun QueueScreen(
    active: ActiveRun?,
    queue: List<DownloadJob>,
    history: List<RunRecord>,
    resumable: List<ResumableRun>,
    onCancelActive: () -> Unit,
    onRemoveQueued: (String) -> Unit,
    onClearHistory: () -> Unit,
    onNewDownload: () -> Unit,
    /**
     * Jobs that came back from the previous session, so their cards can say so.
     *
     * Defaulted rather than threaded through the shell: it is written once in
     * `DownloadRepository.attach` before any composition and never changes, so
     * there is nothing to observe and nothing for AioApp to hold.
     */
    restoredIds: Set<String> = DownloadRepository.restoredJobIds,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var notice by remember { mutableStateOf<String?>(null) }

    // Scan on entry. The folders are written by a process that may have died
    // without telling anyone, so there is no event to react to — arriving here
    // is the only reliable moment to look.
    LaunchedEffect(Unit) { ResumeRepository.refresh(context) }
    LaunchedEffect(notice) {
        if (notice != null) {
            kotlinx.coroutines.delay(4000)
            notice = null
        }
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        // ── unfinished, FIRST ──
        // Above Active on purpose. This is the only section describing work the
        // app is not going to do unless asked, so burying it under the parts
        // that take care of themselves is how a half-downloaded series gets
        // silently abandoned.
        if (resumable.isNotEmpty()) {
            item { SectionHeader("Unfinished (${resumable.size})") }
            items(resumable, key = { it.tmpDir }) { run ->
                ResumableCard(
                    run = run,
                    onResume = { format ->
                        scope.launch {
                            notice = if (ResumeRepository.resume(context, run, format)) {
                                "Resuming ${run.label}"
                            } else {
                                "Couldn't resume that one"
                            }
                        }
                    },
                    onDiscard = {
                        scope.launch {
                            notice = ResumeRepository.discard(context, run)
                                ?: "Discarded ${run.sizeLabel}"
                        }
                    },
                )
            }
        }

        notice?.let {
            item {
                HelpText(it, tone = MaterialTheme.colorScheme.primary)
            }
        }

        item { SectionHeader("Active") }

        if (active == null && queue.isEmpty()) {
            item { EmptyState(onNewDownload) }
        }

        active?.let { run ->
            item(key = "active-${run.job.id}") {
                ActiveCard(run = run, queuedBehind = queue.size, onCancel = onCancelActive)
            }
        }

        // Queued work with nothing running. Normally impossible — every
        // enqueue wakes the service — but it is exactly the state the app comes
        // back in after being killed, because the restored queue deliberately
        // does NOT auto-start. Without this card the Active section would be a
        // header over nothing while three jobs sat below it, unexplained and
        // with no way to start them.
        if (active == null && queue.isNotEmpty()) {
            item(key = "idle-queue") {
                IdleQueueCard(
                    waiting = queue.size,
                    restored = queue.count { it.id in restoredIds },
                    onStart = { DownloadService.ensureRunning(context) },
                )
            }
        }

        if (queue.isNotEmpty()) {
            item { SectionHeader("Waiting (${queue.size})") }
            items(queue, key = { it.id }) { job ->
                QueuedCard(
                    job = job,
                    restored = job.id in restoredIds,
                    onRemove = { onRemoveQueued(job.id) },
                )
            }
        }

        if (history.isNotEmpty()) {
            item {
                Row(
                    Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Box(Modifier.weight(1f)) { SectionHeader("Recent") }
                    AioButton(
                        text = "Clear",
                        tone = ButtonTone.Ghost,
                        compact = true,
                        onClick = onClearHistory,
                    )
                }
            }
            // The store keeps far more than this (see HISTORY_LIMIT) because the
            // resumable back-fill reads it, but a wall of finished downloads
            // under the work that still needs attention is not a feature. The
            // desktop shows none of its 200 at all.
            items(history.take(HISTORY_VISIBLE), key = { it.finished.id }) { record ->
                HistoryCard(record)
            }
            if (history.size > HISTORY_VISIBLE) {
                item {
                    HelpText("Showing the last $HISTORY_VISIBLE of ${history.size} runs.")
                }
            }
        }
    }
}

/** How many finished runs the Recent list shows. */
private const val HISTORY_VISIBLE = 20

// ── unfinished ─────────────────────────────────────────────────────────────

/**
 * One resumable download.
 *
 * ── WHY THE FORMAT PICKER IS HERE AND NOT BURIED ───────────────────────────
 * `--restore-parameters` restores every persisted setting off disk, with ONE
 * deliberate exception: aio-dl.py leaves `--format` out of run_params.json
 * precisely so it can be changed on resume. Downloading 80 chapters and then
 * realizing you wanted CBZ rather than PDF is a real situation, and the pages
 * are already on disk — so the picker is the feature, not a decoration. The
 * desktop's ResumeBar has the same dropdown for the same reason.
 *
 * ── AND WHY DISCARD IS TWO TAPS ────────────────────────────────────────────
 * It deletes finished chapters that cost real bandwidth to fetch. Two taps and
 * a size on the button, matching how the library's delete behaves.
 */
@Composable
private fun ResumableCard(
    run: ResumableRun,
    onResume: (String) -> Unit,
    onDiscard: () -> Unit,
) {
    var format by remember(run.tmpDir) { mutableStateOf(run.effectiveFormat) }
    var confirmDiscard by remember(run.tmpDir) { mutableStateOf(false) }

    LaunchedEffect(confirmDiscard) {
        if (confirmDiscard) {
            kotlinx.coroutines.delay(4000)
            confirmDiscard = false
        }
    }

    AioCard(borderColor = MaterialTheme.aio.warning.copy(alpha = 0.35f)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.Filled.PauseCircle,
                contentDescription = null,
                tint = MaterialTheme.aio.warning,
                modifier = Modifier.size(16.dp),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                run.label,
                style = MaterialTheme.typography.titleSmall,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
        }

        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            if (run.cachedChapters > 0) {
                Pill("${run.cachedChapters} ch kept", tone = PillTone.Success)
            }
            Pill(run.sizeLabel, mono = true)
            run.quality?.takeIf { it != 100 }?.let { Pill("Q$it", mono = true) }
            run.scaling?.takeIf { it != 100 }?.let { Pill("$it%", mono = true) }
        }

        // A tmp folder old enough to predate run_meta.json has no URL, and
        // aio-dl.py takes the URL positionally — so there is nothing to resume
        // with. Said plainly, because the alternative is a Resume button that
        // fails at the Python boundary for reasons the user cannot see.
        if (!run.canResume) {
            Spacer(Modifier.height(8.dp))
            HelpText(
                "This one predates the app recording its source URL, so it can't be " +
                    "resumed — start it again from the New tab and the finished chapters " +
                    "here will still be reused.",
                tone = MaterialTheme.aio.warning,
            )
        }

        Spacer(Modifier.height(12.dp))
        FieldLabel("Resume as")
        Spacer(Modifier.height(6.dp))
        SegmentedChoice(
            options = RESUME_FORMATS,
            selected = format,
            onSelect = { format = it },
        )
        AnimatedVisibility(
            visible = format != run.effectiveFormat,
            enter = AioMotion.revealEnter,
            exit = AioMotion.revealExit,
        ) {
            Column {
                Spacer(Modifier.height(6.dp))
                HelpText(
                    "Was ${run.effectiveFormat.uppercase()}. The chapters already " +
                        "downloaded are reused — only the final file changes.",
                    tone = MaterialTheme.colorScheme.primary,
                )
            }
        }

        Spacer(Modifier.height(12.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            AioButton(
                text = "Resume",
                icon = Icons.Filled.PlayArrow,
                compact = true,
                enabled = run.canResume,
                onClick = { onResume(format) },
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            AioButton(
                text = if (confirmDiscard) "Delete ${run.sizeLabel}?" else "Discard",
                tone = if (confirmDiscard) ButtonTone.Danger else ButtonTone.Ghost,
                compact = true,
                onClick = {
                    if (confirmDiscard) {
                        confirmDiscard = false
                        onDiscard()
                    } else {
                        confirmDiscard = true
                    }
                },
            )
        }
    }
}

/**
 * What a resume can be re-targeted to. Same four `--format` accepts; "none"
 * is included because a resume to raw images is a legitimate way to salvage
 * pages out of a run whose final-file build is what keeps failing.
 */
private val RESUME_FORMATS = listOf(
    ChoiceOption("cbz", "CBZ"),
    ChoiceOption("epub", "EPUB"),
    ChoiceOption("pdf", "PDF"),
    ChoiceOption("none", "Images"),
)

// ── active ─────────────────────────────────────────────────────────────────

@Composable
private fun ActiveCard(run: ActiveRun, queuedBehind: Int, onCancel: () -> Unit) {
    // 1s heartbeat so the elapsed clock advances even while the download is
    // quiet. Progress events stall for minutes during one slow chapter fetch,
    // and without this the timer visibly freezes and the app looks hung. Scoped
    // to this composable, so an empty Queue screen costs no timer at all.
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(run.job.id) {
        while (true) {
            kotlinx.coroutines.delay(1000)
            now = System.currentTimeMillis()
        }
    }

    val p = run.progress
    AioCard(borderColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.35f)) {
        Row(verticalAlignment = Alignment.Top) {
            Column(Modifier.weight(1f)) {
                Text(
                    run.label,
                    style = MaterialTheme.typography.titleSmall,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.height(3.dp))
                Text(
                    buildString {
                        append(phaseLabel(p.phase))
                        if (p.processed > 0) {
                            append(if (p.total > 0) " · Ch. ${p.processed}/${p.total}" else " · ${p.processed} done")
                        }
                        p.chapter?.let { append(" · ch $it") }
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.aio.mutedForeground,
                )
            }
            Spacer(Modifier.width(8.dp))
            Icon(
                Icons.Filled.Close,
                contentDescription = "Stop this download",
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier
                    .clip(CircleShape)
                    .clickable(onClick = onCancel)
                    .padding(10.dp)
                    .size(18.dp),
            )
        }

        Spacer(Modifier.height(12.dp))
        ProgressTrack(fraction = p.fraction)

        // Elapsed on the left, time-left on the right — both monospace and
        // tabular so a value updating on the poll cadence can't reflow the row.
        Spacer(Modifier.height(7.dp))
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                formatElapsed(now - run.startedAt),
                style = AioText.numericSmall,
                color = MaterialTheme.aio.mutedForeground,
            )
            Text(
                // Below two samples the estimator publishes nothing, and at
                // exactly two the estimate is real but young — so it renders
                // dimmed rather than being hidden or shown at full confidence.
                text = p.etaMs?.takeIf { p.etaSamples >= 2 }?.let { "~${formatEta(it)} left" } ?: "",
                style = AioText.numericSmall,
                color = MaterialTheme.colorScheme.onBackground.copy(
                    alpha = if (p.etaSamples == 2) 0.45f else 0.75f,
                ),
            )
        }

        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            run.job.format.takeIf { it.isNotBlank() }?.let { Pill(it.uppercase()) }
            run.job.chapters.takeIf { it.isNotBlank() && it != "all" }?.let { Pill(it, mono = true) }
            if (queuedBehind > 0) Pill("+$queuedBehind queued", tone = PillTone.Primary)
        }
    }
}

// ── queued ─────────────────────────────────────────────────────────────────

/**
 * "There is work here and nobody is doing it."
 *
 * ── WHY A RESTORED QUEUE DOES NOT START ITSELF ─────────────────────────────
 * The desktop restores its queue and auto-starts the head
 * (`UI-source/src/hooks/useDownloader.js`, grep `_restoreQueueItems`). A phone
 * is not a desktop: the connection is likely metered, the app may have been
 * killed hours ago in a different place, and DownloadService's own reasoning
 * for START_NOT_STICKY — never silently spend someone's mobile data on a
 * multi-hundred-megabyte download they did not just ask for — applies just as
 * well to the moment the app reopens. So the queue comes back, and starting it
 * is one deliberate tap.
 */
@Composable
private fun IdleQueueCard(waiting: Int, restored: Int, onStart: () -> Unit) {
    AioCard(borderColor = MaterialTheme.aio.warning.copy(alpha = 0.30f)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.Filled.Schedule,
                contentDescription = null,
                tint = MaterialTheme.aio.warning,
                modifier = Modifier.size(16.dp),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                if (waiting == 1) "1 download waiting" else "$waiting downloads waiting",
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.weight(1f),
            )
        }
        Spacer(Modifier.height(6.dp))
        HelpText(
            if (restored > 0) {
                "Restored from your last session — nothing starts on its own, so they " +
                    "can't spend mobile data you didn't mean to."
            } else {
                "Nothing is running right now."
            },
        )
        Spacer(Modifier.height(12.dp))
        AioButton(
            text = if (waiting == 1) "Start" else "Start all $waiting",
            icon = Icons.Filled.PlayArrow,
            compact = true,
            onClick = onStart,
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

@Composable
private fun QueuedCard(job: DownloadJob, restored: Boolean, onRemove: () -> Unit) {
    AioCard(provisional = true, contentPadding = PaddingValues(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                Icons.Filled.Schedule,
                contentDescription = null,
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier.size(16.dp),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                job.displayLabel,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.aio.mutedForeground,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Icon(
                Icons.Filled.Close,
                contentDescription = "Remove from queue",
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier
                    .clip(CircleShape)
                    .clickable(onClick = onRemove)
                    .padding(8.dp)
                    .size(16.dp),
            )
        }
        AnimatedVisibility(
            visible = restored || job.isResume ||
                job.format.isNotBlank() || job.chapters.isNotBlank(),
            enter = AioMotion.revealEnter,
            exit = AioMotion.revealExit,
        ) {
            Row(
                Modifier.padding(start = 24.dp, top = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                // "Restored" earns a pill because it changes what the row MEANS:
                // this is not something queued a moment ago, it is something the
                // app was carrying when it was killed.
                if (restored) Pill("restored", tone = PillTone.Warning)
                if (job.isResume) Pill("resume", tone = PillTone.Primary)
                job.format.takeIf { it.isNotBlank() }?.let { Pill(it.uppercase()) }
                job.chapters.takeIf { it.isNotBlank() && it != "all" }?.let { Pill(it, mono = true) }
            }
        }
    }
}

// ── history ────────────────────────────────────────────────────────────────

/**
 * One finished run.
 *
 * ── THE FINAL-FILE LINE IS THE POINT OF THIS CARD ──────────────────────────
 * "cancelled · 3/40 ch" used to be the whole story, and it left the most
 * important question unanswered: what happened to the combined archive that was
 * already on disk? aio-dl.py answers it with a `final_file_skipped` event
 * whenever it declines to rebuild, and [finalFileDetail] renders that answer in
 * the same words the terminal notification uses — one wording, two surfaces,
 * because a user reading both must not have to reconcile them.
 */
@Composable
private fun HistoryCard(record: RunRecord) {
    val entry = record.finished
    val status = MaterialTheme.aio
    val (tint, icon, word) = when (entry.outcome) {
        RunOutcome.Completed -> Triple(status.success, Icons.Filled.Check, "completed")
        RunOutcome.Cancelled -> Triple(status.warning, Icons.Filled.PauseCircle, "cancelled")
        RunOutcome.Failed -> Triple(MaterialTheme.colorScheme.error, Icons.Filled.Close, "failed")
    }

    AioCard(borderColor = tint.copy(alpha = 0.28f), contentPadding = PaddingValues(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(icon, contentDescription = null, tint = tint, modifier = Modifier.size(16.dp))
            Spacer(Modifier.width(8.dp))
            Text(
                entry.label,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            Text(
                formatElapsed(entry.durationMs),
                style = AioText.numericSmall,
                color = MaterialTheme.aio.mutedForeground,
            )
        }
        Spacer(Modifier.height(7.dp))
        Row(
            Modifier.padding(start = 24.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Pill(
                word,
                tone = when (entry.outcome) {
                    RunOutcome.Completed -> PillTone.Success
                    RunOutcome.Cancelled -> PillTone.Warning
                    RunOutcome.Failed -> PillTone.Danger
                },
            )
            if (entry.total > 0) Pill("${entry.processed}/${entry.total} ch", mono = true)
            // The exit code earns its place only when it says something the word
            // above does not: 0 and 130 are already spelled out as completed and
            // cancelled.
            if (entry.outcome == RunOutcome.Failed) Pill("exit ${entry.exitCode}", mono = true)
        }
        record.finalFileSkip?.let { skip ->
            Spacer(Modifier.height(8.dp))
            Row(Modifier.padding(start = 24.dp)) {
                HelpText(finalFileDetail(skip), tone = MaterialTheme.aio.warning)
            }
        }
    }
}

// ── empty ──────────────────────────────────────────────────────────────────

@Composable
private fun EmptyState(onNewDownload: () -> Unit) {
    AioCard(contentPadding = PaddingValues(24.dp)) {
        Column(
            Modifier.fillMaxWidth(),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Box(
                Modifier
                    .size(40.dp)
                    .clip(CircleShape)
                    .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.10f)),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    Icons.Filled.Schedule,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(19.dp),
                )
            }
            Spacer(Modifier.height(12.dp))
            Text("Nothing downloading", style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(4.dp))
            HelpText("Paste a series URL on the New tab to get started.")
            Spacer(Modifier.height(14.dp))
            AioButton(
                text = "New download",
                tone = ButtonTone.Outline,
                compact = true,
                onClick = onNewDownload,
            )
        }
    }
}
