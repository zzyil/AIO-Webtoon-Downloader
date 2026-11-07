package com.aio.downloader.ui.screens

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDownward
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Terminal
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import kotlinx.coroutines.launch
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.unit.dp
import com.aio.downloader.core.LogLevel
import com.aio.downloader.core.LogLine
import com.aio.downloader.core.LogTail
import com.aio.downloader.core.logMatchRanges
import com.aio.downloader.core.logQueryTerms
import com.aio.downloader.core.matchesLogQuery
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.AioCard
import com.aio.downloader.ui.components.AioTextField
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.Hairline
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.AioText
import com.aio.downloader.ui.theme.aio

/**
 * aio-dl.py's console output, classified and colour-coded.
 *
 * WHY THIS SCREEN EARNS ITS PLACE: the structured events drive the Queue card,
 * but they carry milestones only. The reasoning — which scanlation group won and
 * why, which CDN host got throttled, which pages were retried, what the
 * skipped-chapter report said — is printed and nothing else captures it. On a
 * phone there is no terminal to fall back to, so without this screen a failed
 * download is simply inexplicable.
 *
 * The lines arrive from [LogTail], already through the same strip/drop/classify
 * pipeline the desktop applies (`UI-source/electron/log-filter.js`).
 *
 * ── THE SCREEN NO LONGER OWNS THE READER ───────────────────────────────────
 * [LogTail] is started at process start and runs for the process lifetime; this
 * screen only reads it (and calls [LogTail.start] as a backstop, which is a
 * no-op when the reader is already up). It used to start and STOP the reader
 * with the composition, which combined with logcat's follow-from-now meant the
 * run you opened this tab to understand was precisely the one with no output.
 *
 * ── AND WHY SEARCH AND COPY BELONG HERE ────────────────────────────────────
 * The two things anyone does with a log they cannot fix themselves: find the
 * line that matters, and send it to somebody. There is no other route to either
 * on a phone — no text selection in a LazyColumn of unwrapped rows, and no
 * terminal to pipe through grep.
 */
@Composable
fun LogsScreen(
    lines: List<LogLine>,
    error: String?,
    onClear: () -> Unit,
) {
    // A no-op while the reader is alive, which it normally is from
    // MainActivity.onCreate. Kept as the backstop for the one case that is not
    // covered: a reader that died while nobody was looking. Note there is no
    // matching stop — see this file's header.
    LaunchedEffect(Unit) { LogTail.start() }

    val context = LocalContext.current
    var levelFilter by rememberSaveable { mutableStateOf<String?>(null) }
    var query by rememberSaveable { mutableStateOf("") }
    // Saved alongside the query, and the two only ever move together (closing
    // clears the query) — a rotation must not be able to hide an active search
    // behind a collapsed field and leave the list mysteriously short.
    var searchOpen by rememberSaveable { mutableStateOf(false) }
    var notice by rememberSaveable { mutableStateOf<String?>(null) }

    val terms = remember(query) { logQueryTerms(query) }
    val filtered = remember(lines, levelFilter, terms) {
        lines.filter { line ->
            (levelFilter == null || line.level.name == levelFilter) &&
                matchesLogQuery(line.text, terms)
        }
    }

    LaunchedEffect(notice) {
        if (notice != null) {
            kotlinx.coroutines.delay(2500)
            notice = null
        }
    }

    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    // "Following" means pinned within a few rows of the bottom. Once the user
    // scrolls up to read something, auto-scroll must stop — yanking the view
    // away mid-sentence is the classic log-viewer sin.
    val following by remember {
        derivedStateOf {
            val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            last >= listState.layoutInfo.totalItemsCount - 3
        }
    }
    LaunchedEffect(filtered.size, following) {
        if (following && filtered.isNotEmpty()) {
            listState.scrollToItem(filtered.lastIndex)
        }
    }

    Column(Modifier.fillMaxSize()) {
        FilterBar(
            lines = lines,
            active = levelFilter,
            onSelect = { levelFilter = it },
            searchOpen = searchOpen,
            onToggleSearch = {
                searchOpen = !searchOpen
                // Closing the field clears the query: leaving a hidden search
                // in force is the same trap as a hidden level filter.
                if (!searchOpen) query = ""
            },
            onCopy = {
                val copied = copyLogLines(context, filtered)
                notice = when {
                    copied < 0 -> "Couldn't reach the clipboard"
                    copied == 0 -> "Nothing to copy"
                    else -> "Copied $copied line${if (copied == 1) "" else "s"}"
                }
            },
            onClear = onClear,
        )

        AnimatedVisibility(
            visible = searchOpen,
            enter = AioMotion.revealEnter,
            exit = AioMotion.revealExit,
        ) {
            SearchRow(
                query = query,
                onQueryChange = { query = it },
                matches = filtered.size,
                total = lines.size,
            )
        }

        notice?.let {
            Row(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 2.dp)) {
                HelpText(it, tone = MaterialTheme.colorScheme.primary)
            }
        }

        // A reader that died AFTER capturing something must not hide what it
        // captured — that output is usually the reason the screen is open. The
        // full-card ErrorState below is for the case where there is genuinely
        // nothing to show.
        if (error != null && lines.isNotEmpty()) {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(Modifier.weight(1f)) {
                    HelpText("Live capture stopped: $error", tone = MaterialTheme.aio.warning)
                }
                AioButton(
                    text = "Try again",
                    tone = ButtonTone.Ghost,
                    compact = true,
                    onClick = LogTail::restart,
                )
            }
        }

        Hairline()

        Box(Modifier.weight(1f)) {
            when {
                error != null && lines.isEmpty() -> ErrorState(error, onRetry = LogTail::restart)
                filtered.isEmpty() -> EmptyState(
                    hasLines = lines.isNotEmpty(),
                    searching = terms.isNotEmpty(),
                )
                else -> LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(horizontal = 14.dp, vertical = 10.dp),
                ) {
                    items(filtered, key = { it.id }) { line -> LogRow(line, terms) }
                }
            }

            // Jump-to-bottom, shown only when it would actually do something —
            // i.e. when the user has scrolled up and auto-follow is suspended.
            JumpToLatest(
                visible = !following && filtered.isNotEmpty(),
                onClick = { scope.launch { listState.animateScrollToItem(filtered.lastIndex) } },
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .padding(16.dp),
            )
        }
    }
}

/**
 * Put the visible lines on the clipboard. Returns how many made it, or -1 if
 * the clipboard service could not be reached.
 *
 * COPIES WHAT IS ON SCREEN, not the whole buffer: a filtered view is the user
 * having already said which lines they care about, and pasting 1500 lines into
 * a chat message when they asked for the 4 errors is not helpfulness.
 *
 * Capped from the END. Android's clipboard rides a Binder transaction and a
 * large clip fails — sometimes silently, sometimes as
 * TransactionTooLargeException — so a very long selection keeps the most
 * recent lines, which are the ones nearest whatever went wrong.
 */
private fun copyLogLines(context: Context, lines: List<LogLine>): Int {
    if (lines.isEmpty()) return 0

    val kept = ArrayDeque<String>()
    var chars = 0
    for (index in lines.indices.reversed()) {
        val text = lines[index].text
        if (chars + text.length + 1 > MAX_COPY_CHARS && kept.isNotEmpty()) break
        kept.addFirst(text)
        chars += text.length + 1
    }

    val clipboard = context.getSystemService(ClipboardManager::class.java) ?: return -1
    return runCatching {
        clipboard.setPrimaryClip(
            ClipData.newPlainText("aio-dl log", kept.joinToString("\n")),
        )
        kept.size
    }.getOrDefault(-1)
}

/** ~64k of text. Comfortably inside the Binder limit, and far more than anyone
 *  pastes into a bug report. */
private const val MAX_COPY_CHARS = 64_000

/**
 * EXTRACTED RATHER THAN INLINED: inside the `Box` nested in this screen's
 * `Column`, `AnimatedVisibility` resolved to the `ColumnScope` overload while
 * only `BoxScope` was innermost — a compile error. Its own composable takes the
 * outer ColumnScope out of the resolution set. Same trick as AioApp's
 * BadgedIcon.
 */
@Composable
private fun JumpToLatest(visible: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier) {
    AnimatedVisibility(
        visible = visible,
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
        modifier = modifier,
    ) {
        AioButton(
            text = "Latest",
            icon = Icons.Filled.ArrowDownward,
            compact = true,
            onClick = onClick,
        )
    }
}

@Composable
private fun LogRow(line: LogLine, terms: List<String>) {
    val status = MaterialTheme.aio
    val color = when (line.level) {
        LogLevel.Error -> MaterialTheme.colorScheme.error
        LogLevel.Warning -> status.warning
        LogLevel.Success -> status.success
        // Verbose is detail hanging off the line above it — dimmed rather than
        // hidden, because it is usually the answer when something looks wrong.
        LogLevel.Verbose -> status.mutedForeground.copy(alpha = 0.75f)
        LogLevel.Info -> MaterialTheme.colorScheme.onBackground.copy(alpha = 0.9f)
    }
    val highlight = MaterialTheme.colorScheme.primary.copy(alpha = 0.28f)
    // Every matching line is a hit, so without marking the matched SPAN the
    // search only tells you which lines qualified, not where to look on a row
    // that scrolls sideways past the screen edge.
    val rendered: AnnotatedString = remember(line.id, terms, highlight) {
        val ranges = logMatchRanges(line.text, terms)
        if (ranges.isEmpty()) {
            AnnotatedString(line.text)
        } else {
            buildAnnotatedString {
                append(line.text)
                ranges.forEach { range ->
                    addStyle(SpanStyle(background = highlight), range.first, range.last + 1)
                }
            }
        }
    }

    Row(Modifier.fillMaxWidth()) {
        // A 2px severity gutter instead of a coloured background: at this text
        // size a tinted row block would drown the text it is meant to mark, and
        // an error still has to be findable while scrolling fast.
        Box(
            Modifier
                .padding(top = 2.dp, bottom = 2.dp, end = 8.dp)
                .width(2.dp)
                .height(15.dp)
                .background(
                    if (line.level == LogLevel.Info || line.level == LogLevel.Verbose) {
                        Color.Transparent
                    } else {
                        color
                    },
                ),
        )
        Text(
            text = rendered,
            style = AioText.log,
            color = color,
            // Horizontal scroll, not wrap: aio-dl.py aligns its output in
            // columns, and wrapping a 200-char line destroys that alignment for
            // every line around it.
            modifier = Modifier
                .weight(1f)
                .horizontalScroll(rememberScrollState()),
            softWrap = false,
        )
    }
}

// ── filter bar ─────────────────────────────────────────────────────────────

@Composable
private fun FilterBar(
    lines: List<LogLine>,
    active: String?,
    onSelect: (String?) -> Unit,
    searchOpen: Boolean,
    onToggleSearch: () -> Unit,
    onCopy: () -> Unit,
    onClear: () -> Unit,
) {
    val errors = remember(lines) { lines.count { it.level == LogLevel.Error } }
    val warnings = remember(lines) { lines.count { it.level == LogLevel.Warning } }

    Row(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 14.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        FilterChip("All", lines.size, active == null) { onSelect(null) }
        FilterChip(
            "Errors",
            errors,
            active == LogLevel.Error.name,
            tint = MaterialTheme.colorScheme.error,
        ) {
            onSelect(if (active == LogLevel.Error.name) null else LogLevel.Error.name)
        }
        FilterChip(
            "Warnings",
            warnings,
            active == LogLevel.Warning.name,
            tint = MaterialTheme.aio.warning,
        ) {
            onSelect(if (active == LogLevel.Warning.name) null else LogLevel.Warning.name)
        }
        Spacer(Modifier.weight(1f))
        // Icons, not labelled buttons: three more words would push the level
        // chips off a phone-width row, and both actions are conventional
        // enough to read as glyphs.
        BarIcon(
            icon = if (searchOpen) Icons.Filled.Close else Icons.Filled.Search,
            description = if (searchOpen) "Close search" else "Search the log",
            tint = if (searchOpen) {
                MaterialTheme.colorScheme.primary
            } else {
                MaterialTheme.aio.mutedForeground
            },
            onClick = onToggleSearch,
        )
        BarIcon(
            icon = Icons.Filled.ContentCopy,
            description = "Copy the visible lines",
            tint = MaterialTheme.aio.mutedForeground,
            onClick = onCopy,
        )
        AioButton(text = "Clear", tone = ButtonTone.Ghost, compact = true, onClick = onClear)
    }
}

@Composable
private fun BarIcon(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    description: String,
    tint: Color,
    onClick: () -> Unit,
) {
    Icon(
        icon,
        contentDescription = description,
        tint = tint,
        modifier = Modifier
            .clip(CircleShape)
            .clickable(onClick = onClick)
            .padding(7.dp)
            .size(17.dp),
    )
}

@Composable
private fun SearchRow(
    query: String,
    onQueryChange: (String) -> Unit,
    matches: Int,
    total: Int,
) {
    val focus = remember { FocusRequester() }
    // Open the keyboard with the field. Opening a search box and then having to
    // tap it is two gestures for one intention. runCatching because
    // requestFocus throws if the node is not attached yet, which is a timing
    // detail no user should ever see as a crash.
    LaunchedEffect(Unit) { runCatching { focus.requestFocus() } }

    Column(Modifier.padding(start = 14.dp, end = 14.dp, bottom = 8.dp)) {
        AioTextField(
            value = query,
            onValueChange = onQueryChange,
            placeholder = "Find in log — every word must match",
            mono = true,
            minHeight = 40,
            modifier = Modifier.focusRequester(focus),
            leading = {
                Icon(
                    Icons.Filled.Search,
                    contentDescription = null,
                    tint = MaterialTheme.aio.mutedForeground,
                    modifier = Modifier.size(15.dp),
                )
            },
        )
        if (query.isNotBlank()) {
            Spacer(Modifier.height(5.dp))
            HelpText("$matches of $total lines match")
        }
    }
}

@Composable
private fun FilterChip(
    label: String,
    count: Int,
    selected: Boolean,
    tint: Color? = null,
    onClick: () -> Unit,
) {
    val accent = tint ?: MaterialTheme.colorScheme.primary
    val fg = if (selected) accent else MaterialTheme.aio.mutedForeground
    Row(
        Modifier
            .clip(CircleShape)
            .background(if (selected) accent.copy(alpha = 0.12f) else Color.Transparent)
            .border(
                1.dp,
                if (selected) accent.copy(alpha = 0.35f) else MaterialTheme.aio.border,
                CircleShape,
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 11.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = fg)
        if (count > 0) {
            Spacer(Modifier.width(5.dp))
            Text(count.toString(), style = AioText.numericSmall, color = fg.copy(alpha = 0.8f))
        }
    }
}

// ── empty / error ──────────────────────────────────────────────────────────

@Composable
private fun EmptyState(hasLines: Boolean, searching: Boolean) {
    Box(Modifier.fillMaxSize().padding(20.dp), contentAlignment = Alignment.Center) {
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
                        Icons.Filled.Terminal,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.size(19.dp),
                    )
                }
                Spacer(Modifier.height(12.dp))
                Text(
                    when {
                        searching -> "No line matches that search"
                        hasLines -> "Nothing matches that filter"
                        else -> "No output yet"
                    },
                    style = MaterialTheme.typography.titleSmall,
                )
                Spacer(Modifier.height(4.dp))
                HelpText(
                    when {
                        searching ->
                            "Every word has to appear on the same line. Drop a word, or " +
                                "close the search to see everything."
                        hasLines -> "Tap the chip again to see everything."
                        else -> "Output appears here once a download starts."
                    },
                )
            }
        }
    }
}

@Composable
private fun ErrorState(message: String, onRetry: () -> Unit) {
    Box(Modifier.fillMaxSize().padding(20.dp), contentAlignment = Alignment.Center) {
        AioCard(
            borderColor = MaterialTheme.colorScheme.error.copy(alpha = 0.3f),
            contentPadding = PaddingValues(20.dp),
        ) {
            Text("Log reader unavailable", style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(6.dp))
            HelpText(
                "$message\n\nThe download itself is unaffected — everything here is also " +
                    "in `adb logcat`.",
            )
            Spacer(Modifier.height(12.dp))
            // The reader follows logcat from "now", so a retry cannot recover
            // what was missed while it was down — it only starts capturing
            // again, which is still the difference between a dead screen and a
            // working one.
            AioButton(
                text = "Try again",
                icon = Icons.Filled.Refresh,
                tone = ButtonTone.Outline,
                compact = true,
                onClick = onRetry,
            )
        }
    }
}
