package com.aio.downloader.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
// Same simple name as the grid `items` above, different receiver
// (LazyListScope vs LazyGridScope) — overload resolution separates them.
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Bolt
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.FilterList
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.automirrored.filled.LibraryBooks
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.SwapVert
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aio.downloader.core.AppSettingsStore
import com.aio.downloader.core.FacetGroup
import com.aio.downloader.core.FacetValue
import com.aio.downloader.core.LibraryFilters
import com.aio.downloader.core.LibraryRepository
import com.aio.downloader.core.LibrarySeries
import com.aio.downloader.core.LibrarySort
import com.aio.downloader.core.SweepState
import com.aio.downloader.core.UpdateGroups
import com.aio.downloader.core.UpdateRow
import com.aio.downloader.core.UpdateRowState
import com.aio.downloader.core.buildFacetIndex
import com.aio.downloader.core.buildUpdateGroups
import com.aio.downloader.core.formatSize
import com.aio.downloader.core.sortedForLibrary
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.AioCard
import com.aio.downloader.ui.components.AioTextField
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.CoverArt
import com.aio.downloader.ui.components.Hairline
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.components.Pill
import com.aio.downloader.ui.components.ProgressTrack
import com.aio.downloader.ui.components.SwitchRow
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.AioText
import com.aio.downloader.ui.theme.aio

/**
 * Roughly how wide a cover tile wants to be, before the 3..6 column clamp.
 *
 * Tuned on the reference tablet, where the first pass (118dp) produced five
 * columns of ~114dp — technically within spec and visibly too small to read a
 * cover at arm's length. 145dp lands on four generous columns there and still
 * clamps up to three on a 411dp phone, which is the narrow case this has to work
 * for at all.
 */
private val TILE_TARGET = 145.dp

/**
 * What is on disk: the cover grid, and everything that narrows it.
 *
 * ── WHAT WAS PORTED FROM LibraryTab.jsx AND WHAT WAS RESHAPED ──────────────
 * Ported: the grid, search, the six sorts, the four facet groups with
 * library-wide counts, active-filter chips, the three distinct empty states,
 * per-series update checking. Reshaped: the desktop's Updates Center is a side
 * sheet driven by a four-slot worker pool; here a sweep is SERIAL (see
 * LibraryRepository.startSweep — that is a constraint, not a simplification)
 * and reports itself in a strip above the grid, because a phone has no room for
 * a panel that covers the thing it is describing.
 *
 * ── THIS SCREEN COLLECTS ITS OWN STATE ─────────────────────────────────────
 * unlike Queue and Logs, which take theirs from AioApp. Six flows plus six
 * callbacks would be a worse signature than a direct read of a singleton this
 * screen is the only consumer of. AioApp still collects `updates` itself — it
 * needs the count for the tab badge.
 */
@Composable
fun LibraryScreen(
    /** Queue "download the chapters this series is missing". */
    onQueueUpdate: (LibrarySeries, String) -> Unit,
    onNewDownload: () -> Unit,
) {
    val context = LocalContext.current

    val series by LibraryRepository.series.collectAsStateWithLifecycle()
    val loading by LibraryRepository.loading.collectAsStateWithLifecycle()
    val error by LibraryRepository.error.collectAsStateWithLifecycle()
    val updates by LibraryRepository.updates.collectAsStateWithLifecycle()
    val checking by LibraryRepository.checking.collectAsStateWithLifecycle()
    val sweep by LibraryRepository.sweep.collectAsStateWithLifecycle()

    // First entry only. `series == null` is the never-scanned sentinel, so a tab
    // switch back to an already-loaded (even empty) library costs no disk walk.
    LaunchedEffect(Unit) {
        if (series == null) LibraryRepository.refresh(context)
    }

    // Opt-in auto-sweep, once per app run and only after a scan has produced
    // something to sweep. Gated on `sweep == null` — the sweep state is a
    // process-lifetime singleton, so that reads as "no sweep has happened yet"
    // and a tab switch cannot restart minutes of network work.
    LaunchedEffect(series != null, sweep == null) {
        if (series != null && sweep == null &&
            AppSettingsStore.current(context).autoCheckUpdates &&
            !LibraryRepository.busyWithDownload()
        ) {
            LibraryRepository.startSweep(context)
        }
    }

    var query by rememberSaveable { mutableStateOf("") }
    var sort by rememberSaveable { mutableStateOf(LibrarySort.TitleAsc) }
    var filters by remember { mutableStateOf(LibraryFilters()) }
    var filterOpen by rememberSaveable { mutableStateOf(false) }
    // The folder, not the object: a rescan replaces every LibrarySeries
    // instance, and holding one would pin the detail screen to a stale copy.
    var openFolder by rememberSaveable { mutableStateOf<String?>(null) }

    val all = series.orEmpty()
    val selected = openFolder?.let { folder -> all.firstOrNull { it.folder == folder } }

    if (selected != null) {
        SeriesDetailScreen(
            series = selected,
            onBack = { openFolder = null },
            onQueueUpdate = { range -> onQueueUpdate(selected, range) },
            onFilterByFacet = { group, key ->
                filters = filters.add(group, key)
                openFolder = null
            },
        )
        return
    }

    val facets = remember(all) { buildFacetIndex(all) }
    val visible = remember(all, query, filters, sort) {
        val needle = query.trim().lowercase()
        all.asSequence()
            .filter { needle.isEmpty() || it.searchKey.contains(needle) }
            .filter { filters.isEmpty || filters.matches(it) }
            .toList()
            .sortedForLibrary(sort)
    }

    Box(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize()) {
            LibraryToolbar(
                query = query,
                onQuery = { query = it },
                total = all.size,
                shown = visible.size,
                sort = sort,
                onSort = { sort = it },
                activeFilters = filters.activeCount,
                onOpenFilters = { filterOpen = true },
                loading = loading,
                onRefresh = { LibraryRepository.refresh(context) },
                sweepRunning = sweep?.running == true,
                canSweep = all.any { it.checkable },
                onSweep = { LibraryRepository.startSweep(context) },
            )

            error?.let { ErrorStrip(it) }

            ActiveFilterChips(
                filters = filters,
                facets = facets,
                onToggle = { group, key -> filters = filters.toggle(group, key) },
                onClear = { filters = filters.cleared() },
            )

            // Built here, once, from the three flows — so the strip, the card
            // badges and the detail screen cannot disagree about what a check
            // found. See buildUpdateGroups in core/LibraryModels.kt.
            val updateGroups = remember(all, updates, checking, sweep?.running) {
                buildUpdateGroups(all, updates, checking, sweep?.running == true)
            }

            // Queue one series' new chapters and drop its badge as the job
            // leaves: leaving it up implies there is still something to act on.
            val queueOne: (UpdateRow) -> Unit = { row ->
                all.firstOrNull { it.folder == row.folder }?.let { entry ->
                    onQueueUpdate(entry, row.range)
                    LibraryRepository.dismissUpdate(row.folder)
                }
            }

            UpdatesStrip(
                sweep = sweep,
                groups = updateGroups,
                onStop = { LibraryRepository.cancelSweep() },
                onDismissAll = { LibraryRepository.clearUpdates() },
                onQueueAll = { updateGroups.found.forEach(queueOne) },
                onQueueOne = queueOne,
                onDismissOne = { LibraryRepository.dismissUpdate(it.folder) },
                onOpen = { openFolder = it.folder },
            )

            Box(Modifier.weight(1f)) {
                when {
                    visible.isNotEmpty() -> SeriesGrid(
                        series = visible,
                        // From the same grouping the strip renders, so a card's
                        // "+N" and the strip's row can never say different
                        // numbers for one series.
                        newCounts = remember(updateGroups) {
                            updateGroups.found.associate { it.folder to it.newChapters }
                        },
                        checking = checking,
                        onOpen = { openFolder = it.folder },
                    )

                    loading && all.isEmpty() -> LoadingGrid()

                    // Ordering matters: an empty library must never offer
                    // "clear filters", and a filtered-empty view must not
                    // pretend nothing has ever been downloaded.
                    all.isEmpty() -> EmptyLibrary(onNewDownload)

                    filters.activeCount > 0 -> NoMatches(query) { filters = filters.cleared() }

                    else -> NoSearchResults(query)
                }
            }
        }

        // Aligned from HERE rather than inside FilterPanel: BoxScope.align is
        // only available at the call site, and moving the panel's own
        // AnimatedVisibility into a Box scope would put it next to the
        // ColumnScope overload — the resolution trap documented in AioApp.kt.
        FilterPanel(
            open = filterOpen,
            facets = facets,
            filters = filters,
            matched = visible.size,
            total = all.size,
            onChange = { filters = it },
            onClose = { filterOpen = false },
            scrimModifier = Modifier.fillMaxSize(),
            panelModifier = Modifier.align(Alignment.BottomCenter),
        )
    }
}

// ── toolbar ────────────────────────────────────────────────────────────────

/**
 * Two rows, because five controls do not fit one on a phone.
 *
 * Row 1 pairs search with the filter button — both narrow the same list, so the
 * badge sits where the eye already is. Row 2 is status on the left and verbs on
 * the right: how many, then sort, check, rescan.
 */
@Composable
private fun LibraryToolbar(
    query: String,
    onQuery: (String) -> Unit,
    total: Int,
    shown: Int,
    sort: LibrarySort,
    onSort: (LibrarySort) -> Unit,
    activeFilters: Int,
    onOpenFilters: () -> Unit,
    loading: Boolean,
    onRefresh: () -> Unit,
    sweepRunning: Boolean,
    canSweep: Boolean,
    onSweep: () -> Unit,
) {
    Column {
        Row(
            Modifier
                .fillMaxWidth()
                .padding(start = 16.dp, end = 10.dp, top = 10.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(Modifier.weight(1f)) {
                AioTextField(
                    value = query,
                    onValueChange = onQuery,
                    placeholder = "Search library…",
                    minHeight = 42,
                    leading = {
                        Icon(
                            Icons.Filled.Search,
                            contentDescription = null,
                            tint = MaterialTheme.aio.mutedForeground,
                            modifier = Modifier.size(15.dp),
                        )
                    },
                )
            }
            Spacer(Modifier.width(6.dp))
            ToolbarIcon(
                icon = Icons.Filled.FilterList,
                label = "Filter by genre, tag, status or source",
                active = activeFilters > 0,
                badge = activeFilters,
                onClick = onOpenFilters,
            )
        }

        Row(
            Modifier
                .fillMaxWidth()
                .padding(start = 16.dp, end = 10.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                // Says how much is hidden whenever anything is: "24 series" is
                // reassuring, "3 of 24" explains why the grid looks short.
                text = if (shown == total) {
                    "$total series"
                } else {
                    "$shown of $total"
                },
                style = AioText.numericSmall,
                color = MaterialTheme.aio.mutedForeground,
                modifier = Modifier.weight(1f),
            )

            SortMenu(sort = sort, onSort = onSort)

            if (canSweep) {
                ToolbarIcon(
                    icon = Icons.Filled.Bolt,
                    label = "Check every series for new chapters",
                    active = sweepRunning,
                    pulsing = sweepRunning,
                    onClick = onSweep,
                )
            }

            ToolbarIcon(
                icon = Icons.Filled.Refresh,
                label = "Rescan the library folder",
                pulsing = loading,
                onClick = onRefresh,
            )
        }
        Hairline()
    }
}

/**
 * A 44dp icon target with an optional count badge and an optional slow pulse.
 *
 * The pulse stands in for a spinner. A rotating Refresh glyph would be the
 * literal port of the desktop's `animate-spin`, but a rescan finishes in well
 * under a second and a spin that never completes a revolution reads as a
 * glitch, not as progress.
 */
@Composable
private fun ToolbarIcon(
    icon: ImageVector,
    label: String,
    onClick: () -> Unit,
    active: Boolean = false,
    badge: Int = 0,
    pulsing: Boolean = false,
) {
    val base = if (active) MaterialTheme.colorScheme.primary else MaterialTheme.aio.mutedForeground
    // The transition is created ONLY in the pulsing branch. Composable calls in
    // an if/else are fine (each branch gets its own group), and it matters here:
    // an always-running infinite transition per toolbar icon would keep the
    // whole screen in a permanent recomposition loop for no visible reason.
    val tint = if (pulsing) pulsingTint(base) else base

    Box(
        Modifier
            .size(44.dp)
            .clip(MaterialTheme.shapes.small)
            .background(
                if (active) {
                    MaterialTheme.colorScheme.primary.copy(alpha = 0.10f)
                } else {
                    Color.Transparent
                },
            )
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        BadgedToolbarIcon(icon = icon, label = label, tint = tint, badge = badge)
    }
}

/**
 * EXTRACTED, not inlined into [ToolbarIcon]'s Box, for the reason AioApp.kt's
 * BadgedIcon documents: nested inside another layout scope the badge's
 * AnimatedVisibility resolves to the wrong overload and fails to compile with a
 * message about implicit receivers.
 */
@Composable
private fun BadgedToolbarIcon(icon: ImageVector, label: String, tint: Color, badge: Int) {
    Box {
        Icon(icon, contentDescription = label, tint = tint, modifier = Modifier.size(19.dp))
        AnimatedVisibility(
            visible = badge > 0,
            enter = fadeIn(tween(AioMotion.COLOR_MS)),
            exit = fadeOut(tween(AioMotion.COLOR_MS)),
            modifier = Modifier.align(Alignment.TopEnd),
        ) {
            Box(
                Modifier
                    .padding(start = 10.dp)
                    .background(MaterialTheme.colorScheme.primary, CircleShape)
                    .padding(horizontal = 4.dp),
            ) {
                Text(
                    badge.toString(),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onPrimary,
                )
            }
        }
    }
}

@Composable
private fun pulsingTint(base: Color): Color {
    val alpha by rememberInfiniteTransition(label = "toolbarPulse").animateFloat(
        initialValue = 0.3f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            tween(700, easing = AioMotion.EaseOut),
            RepeatMode.Reverse,
        ),
        label = "toolbarPulseAlpha",
    )
    return base.copy(alpha = alpha)
}

@Composable
private fun SortMenu(sort: LibrarySort, onSort: (LibrarySort) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        Row(
            Modifier
                .clip(MaterialTheme.shapes.small)
                .clickable { open = true }
                .heightIn(min = 44.dp)
                .padding(horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                Icons.Filled.SwapVert,
                contentDescription = "Sort",
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier.size(16.dp),
            )
            Spacer(Modifier.width(5.dp))
            Text(
                sort.label,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.aio.mutedForeground,
                maxLines = 1,
            )
        }
        DropdownMenu(
            expanded = open,
            onDismissRequest = { open = false },
            containerColor = MaterialTheme.aio.card,
        ) {
            LibrarySort.entries.forEach { option ->
                DropdownMenuItem(
                    text = {
                        Text(
                            option.label,
                            style = MaterialTheme.typography.bodyLarge,
                            color = if (option == sort) {
                                MaterialTheme.colorScheme.primary
                            } else {
                                MaterialTheme.colorScheme.onBackground
                            },
                        )
                    },
                    onClick = {
                        onSort(option)
                        open = false
                    },
                )
            }
        }
    }
}

@Composable
private fun ErrorStrip(message: String) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.error.copy(alpha = 0.08f))
            .padding(horizontal = 16.dp, vertical = 8.dp),
    ) {
        HelpText(message, tone = MaterialTheme.colorScheme.error)
    }
}

// ── filter chips ───────────────────────────────────────────────────────────

/**
 * The active selection, spelled out under the toolbar.
 *
 * Worth the vertical space: a filter outlives a tab switch, and a grid quietly
 * hiding two thirds of the library with no visible cause is the bug report that
 * cannot be reproduced. A key whose value no longer exists in the library still
 * gets a chip — falling back to the raw key — so it stays removable rather than
 * silently matching nothing.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ActiveFilterChips(
    filters: LibraryFilters,
    facets: Map<FacetGroup, List<FacetValue>>,
    onToggle: (FacetGroup, String) -> Unit,
    onClear: () -> Unit,
) {
    AnimatedVisibility(
        visible = filters.activeCount > 0,
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        FlowRow(
            Modifier
                .fillMaxWidth()
                .background(MaterialTheme.aio.card.copy(alpha = 0.35f))
                .padding(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            FacetGroup.entries.forEach { group ->
                filters.selected(group).forEach { key ->
                    val label = facets[group]?.firstOrNull { it.key == key }?.label ?: key
                    Row(
                        Modifier
                            .clip(CircleShape)
                            .background(MaterialTheme.aio.secondary.copy(alpha = 0.7f))
                            .border(1.dp, MaterialTheme.aio.border, CircleShape)
                            .clickable { onToggle(group, key) }
                            .padding(start = 9.dp, end = 6.dp, top = 5.dp, bottom = 5.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            label,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onBackground,
                            maxLines = 1,
                        )
                        Spacer(Modifier.width(4.dp))
                        Icon(
                            Icons.Filled.Close,
                            contentDescription = "Remove ${group.label} filter $label",
                            tint = MaterialTheme.aio.mutedForeground,
                            modifier = Modifier.size(12.dp),
                        )
                    }
                }
            }
            if (filters.matchAll) {
                Text(
                    "MATCH ALL",
                    style = AioText.sectionHeader,
                    color = MaterialTheme.aio.mutedForeground,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }
            Text(
                "Clear",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .clip(MaterialTheme.shapes.extraSmall)
                    .clickable(onClick = onClear)
                    .padding(horizontal = 6.dp, vertical = 6.dp),
            )
        }
    }
}

// ── updates strip ──────────────────────────────────────────────────────────

/** How tall the expanded row list may get before it scrolls internally. */
private val STRIP_LIST_MAX = 240.dp

/**
 * The phone-shaped Updates Center: one strip above the grid instead of a panel
 * over it.
 *
 * ── WHY NOT THE DESKTOP'S SIDE SHEET ───────────────────────────────────────
 * `UpdatesCenter.jsx` is a 480px right-hand panel over the library. At phone
 * width a panel IS the screen, so it would cover the grid it is describing and
 * the "+N" badges it exists to explain. The strip keeps both on screen; the
 * cost is that the row list has to be [STRIP_LIST_MAX]-capped and scroll
 * inside itself.
 *
 * ── WHAT IT CONSOLIDATES ───────────────────────────────────────────────────
 * Everything about update state used to be three disconnected surfaces: a
 * found-COUNT here, a `+N` badge per card, and a "Download N" button on the
 * detail screen — with no way to see WHICH series had what without opening each
 * one. Collapsed, this is still the one-line summary it was; expanded, it is
 * the desktop's four sections (found / checking / up to date / errors) with the
 * same per-row queue and dismiss actions, and a row taps through to its series.
 * The badge and the detail button stay: the badge marks a card in the grid and
 * the button is where you already are after checking one series.
 *
 * It now shows for ANY update state, not just a sweep — a single check from the
 * detail screen used to leave nothing but a card badge behind.
 */
@Composable
private fun UpdatesStrip(
    sweep: SweepState?,
    groups: UpdateGroups,
    onStop: () -> Unit,
    onDismissAll: () -> Unit,
    onQueueAll: () -> Unit,
    onQueueOne: (UpdateRow) -> Unit,
    onDismissOne: (UpdateRow) -> Unit,
    onOpen: (UpdateRow) -> Unit,
) {
    AnimatedVisibility(
        visible = sweep != null || !groups.isEmpty,
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        val running = sweep?.running == true
        val stopped = sweep?.stoppedBecause.orEmpty()
        val accent = when {
            running || groups.checking.isNotEmpty() -> MaterialTheme.colorScheme.primary
            groups.found.isNotEmpty() || stopped.isNotEmpty() -> MaterialTheme.aio.warning
            groups.failed.isNotEmpty() -> MaterialTheme.colorScheme.error
            else -> MaterialTheme.aio.success
        }

        // Collapsed by default, and it never opens itself: the headline plus
        // "Download all N" is the whole job for most sweeps, and a strip that
        // expanded on its own would push the grid down at the exact moment the
        // user is looking at which cards gained a badge. rememberSaveable so a
        // rotation does not collapse a list being read.
        var expanded by rememberSaveable { mutableStateOf(false) }
        var foundSection by rememberSaveable { mutableStateOf(true) }
        var checkingSection by rememberSaveable { mutableStateOf(true) }
        var upToDateSection by rememberSaveable { mutableStateOf(false) }
        var failedSection by rememberSaveable { mutableStateOf(true) }

        Box(Modifier.padding(horizontal = 16.dp, vertical = 10.dp)) {
            AioCard(borderColor = accent.copy(alpha = 0.4f), contentPadding = PaddingValues(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = stripHeadline(sweep, groups),
                        style = MaterialTheme.typography.titleSmall,
                        color = accent,
                        modifier = Modifier.weight(1f),
                    )
                    if (!groups.isEmpty) {
                        val chevron by animateFloatAsState(
                            targetValue = if (expanded) 180f else 0f,
                            animationSpec = tween(AioMotion.SLIDE_UP_MS, easing = AioMotion.EaseOut),
                            label = "stripChevron",
                        )
                        Icon(
                            Icons.Filled.KeyboardArrowDown,
                            contentDescription = if (expanded) "Hide the series list" else "Show which series",
                            tint = MaterialTheme.aio.mutedForeground,
                            modifier = Modifier
                                .clip(CircleShape)
                                .clickable { expanded = !expanded }
                                .padding(7.dp)
                                .size(17.dp)
                                .rotate(chevron),
                        )
                    }
                    Icon(
                        if (running) Icons.Filled.Close else Icons.Filled.Check,
                        contentDescription = if (running) "Stop checking" else "Dismiss",
                        tint = MaterialTheme.aio.mutedForeground,
                        modifier = Modifier
                            .clip(CircleShape)
                            .clickable(onClick = if (running) onStop else onDismissAll)
                            .padding(7.dp)
                            .size(16.dp),
                    )
                }

                if (running && sweep != null) {
                    Spacer(Modifier.height(9.dp))
                    ProgressTrack(
                        fraction = if (sweep.total > 0) {
                            sweep.completed.toFloat() / sweep.total
                        } else {
                            null
                        },
                    )
                    if (sweep.current.isNotEmpty()) {
                        Spacer(Modifier.height(6.dp))
                        HelpText(sweep.current)
                    }
                }

                AnimatedVisibility(
                    visible = expanded && !groups.isEmpty,
                    enter = AioMotion.revealEnter,
                    exit = AioMotion.revealExit,
                ) {
                    Column {
                        Spacer(Modifier.height(6.dp))
                        // Lazy, not a plain scrolling Column: "up to date" is
                        // every checked series, which on a big library is
                        // hundreds of rows the user may well expand.
                        LazyColumn(Modifier.heightIn(max = STRIP_LIST_MAX)) {
                            updateSection(
                                title = "Updates found",
                                state = UpdateRowState.Found,
                                rows = groups.found,
                                open = foundSection,
                                onToggle = { foundSection = !foundSection },
                                onOpen = onOpen,
                                onQueue = onQueueOne,
                                onDismiss = onDismissOne,
                            )
                            updateSection(
                                title = "Checking",
                                state = UpdateRowState.Checking,
                                rows = groups.checking,
                                open = checkingSection,
                                note = if (groups.waiting > 0) "+${groups.waiting} waiting" else "",
                                onToggle = { checkingSection = !checkingSection },
                                onOpen = onOpen,
                                onQueue = onQueueOne,
                                onDismiss = onDismissOne,
                            )
                            updateSection(
                                title = "Errors",
                                state = UpdateRowState.Failed,
                                rows = groups.failed,
                                open = failedSection,
                                onToggle = { failedSection = !failedSection },
                                onOpen = onOpen,
                                onQueue = onQueueOne,
                                onDismiss = onDismissOne,
                            )
                            updateSection(
                                title = "Up to date",
                                state = UpdateRowState.UpToDate,
                                rows = groups.upToDate,
                                open = upToDateSection,
                                onToggle = { upToDateSection = !upToDateSection },
                                onOpen = onOpen,
                                onQueue = onQueueOne,
                                onDismiss = onDismissOne,
                            )
                        }
                    }
                }

                if (!running && groups.found.isNotEmpty()) {
                    Spacer(Modifier.height(10.dp))
                    AioButton(
                        text = if (groups.found.size == 1) {
                            "Download the new chapters"
                        } else {
                            "Download all ${groups.found.size}"
                        },
                        icon = Icons.Filled.Download,
                        compact = true,
                        onClick = onQueueAll,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        }
    }
}

/**
 * The one line the collapsed strip has to carry.
 *
 * Order is by urgency, not by section order: a sweep that stopped early is the
 * most important thing on the card even when it also found updates — those are
 * still listed and still queueable underneath.
 */
private fun stripHeadline(sweep: SweepState?, groups: UpdateGroups): String {
    val found = groups.found.size
    val failed = groups.failed.size
    val stopped = sweep?.stoppedBecause.orEmpty()
    return when {
        sweep?.running == true ->
            "Checking ${(sweep.completed + 1).coerceAtMost(sweep.total)} of ${sweep.total}"
        // A check started from the detail screen has no sweep behind it.
        groups.checking.isNotEmpty() -> "Checking ${groups.checking.first().name}"
        stopped.isNotEmpty() -> stopped
        found > 0 -> "$found series ${if (found == 1) "has" else "have"} new chapters"
        failed > 0 -> "$failed check${if (failed == 1) "" else "s"} failed"
        groups.upToDate.isNotEmpty() -> "Everything is up to date"
        else -> "Nothing to check"
    }
}

/**
 * One collapsible section of the strip: a header that toggles, then its rows.
 *
 * A LazyListScope extension rather than a composable so all four sections share
 * one lazy list — four nested scrollers inside a 240dp card would be unusable.
 * Renders nothing at all when the section is empty, which is what makes the
 * same four calls work for a mid-sweep view and a finished one.
 */
private fun LazyListScope.updateSection(
    title: String,
    state: UpdateRowState,
    rows: List<UpdateRow>,
    open: Boolean,
    onToggle: () -> Unit,
    onOpen: (UpdateRow) -> Unit,
    onQueue: (UpdateRow) -> Unit,
    onDismiss: (UpdateRow) -> Unit,
    note: String = "",
) {
    if (rows.isEmpty() && note.isEmpty()) return
    item(key = "section-$title") {
        Row(
            Modifier
                .fillMaxWidth()
                .clip(MaterialTheme.shapes.extraSmall)
                .clickable(enabled = rows.isNotEmpty(), onClick = onToggle)
                .padding(vertical = 7.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                Icons.Filled.KeyboardArrowDown,
                contentDescription = null,
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier
                    .size(14.dp)
                    .rotate(if (open || rows.isEmpty()) 0f else -90f),
            )
            Spacer(Modifier.width(5.dp))
            Text(
                title.uppercase(),
                style = AioText.sectionHeader,
                color = updateTone(state),
            )
            Spacer(Modifier.weight(1f))
            if (note.isNotEmpty()) {
                Text(
                    note,
                    style = AioText.numericSmall,
                    color = MaterialTheme.aio.mutedForeground,
                )
                Spacer(Modifier.width(6.dp))
            }
            if (rows.isNotEmpty()) Pill(rows.size.toString(), mono = true)
        }
    }
    if (!open) return
    items(rows, key = { "row-${it.folder}" }) { row ->
        UpdateRowItem(
            row = row,
            onOpen = { onOpen(row) },
            onQueue = { onQueue(row) },
            onDismiss = { onDismiss(row) },
        )
    }
}

/** Section/row accent. One place, so a chip and its rows cannot drift apart. */
@Composable
private fun updateTone(state: UpdateRowState): Color = when (state) {
    UpdateRowState.Found -> MaterialTheme.aio.warning
    UpdateRowState.Checking -> MaterialTheme.colorScheme.primary
    UpdateRowState.UpToDate -> MaterialTheme.aio.success
    UpdateRowState.Failed -> MaterialTheme.colorScheme.error
}

/**
 * One series inside the strip. The whole row opens it; the two icon buttons at
 * the end are the found-row actions the desktop's panel has (queue, dismiss).
 */
@Composable
private fun UpdateRowItem(
    row: UpdateRow,
    onOpen: () -> Unit,
    onQueue: () -> Unit,
    onDismiss: () -> Unit,
) {
    val tone = updateTone(row.state)
    Row(
        Modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.small)
            .clickable(onClick = onOpen)
            .heightIn(min = 44.dp)
            .padding(horizontal = 4.dp, vertical = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                row.name,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(1.dp))
            Text(
                buildString {
                    if (row.site.isNotEmpty()) append(row.site)
                    val detail = when (row.state) {
                        UpdateRowState.Found -> row.range
                        UpdateRowState.Checking -> "checking…"
                        UpdateRowState.UpToDate ->
                            if (row.total > 0) "${row.total} on the site" else "up to date"
                        UpdateRowState.Failed -> row.problem
                    }
                    if (detail.isNotEmpty()) {
                        if (isNotEmpty()) append(" · ")
                        append(detail)
                    }
                },
                style = AioText.numericSmall,
                color = if (row.state == UpdateRowState.Failed) tone else MaterialTheme.aio.mutedForeground,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }

        when (row.state) {
            UpdateRowState.Found -> {
                Spacer(Modifier.width(6.dp))
                Text(
                    "+${row.newChapters}",
                    style = AioText.numericSmall,
                    color = Color.White,
                    modifier = Modifier
                        .clip(CircleShape)
                        .background(tone)
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                )
                Icon(
                    Icons.Filled.Download,
                    contentDescription = "Download ${row.newChapters} new " +
                        "chapter${if (row.newChapters == 1) "" else "s"} of ${row.name}",
                    tint = tone,
                    modifier = Modifier
                        .clip(CircleShape)
                        .clickable(onClick = onQueue)
                        .padding(9.dp)
                        .size(16.dp),
                )
                Icon(
                    Icons.Filled.Close,
                    contentDescription = "Dismiss ${row.name}",
                    tint = MaterialTheme.aio.mutedForeground,
                    modifier = Modifier
                        .clip(CircleShape)
                        .clickable(onClick = onDismiss)
                        .padding(9.dp)
                        .size(14.dp),
                )
            }

            UpdateRowState.Checking -> Box(
                Modifier
                    .padding(horizontal = 9.dp)
                    .size(9.dp)
                    .clip(CircleShape)
                    .background(pulsingTint(tone)),
            )

            UpdateRowState.UpToDate -> Icon(
                Icons.Filled.Check,
                contentDescription = null,
                tint = tone,
                modifier = Modifier.padding(horizontal = 8.dp).size(14.dp),
            )

            UpdateRowState.Failed -> Icon(
                Icons.Filled.Warning,
                contentDescription = null,
                tint = tone,
                modifier = Modifier.padding(horizontal = 8.dp).size(14.dp),
            )
        }
    }
}

// ── grid ───────────────────────────────────────────────────────────────────

/**
 * Column count is computed rather than left to [GridCells.Adaptive].
 *
 * Adaptive has no upper bound, so the tile width that gives a phone a
 * comfortable three columns gives a 1280dp tablet ten postage stamps. Clamping
 * to 3..6 keeps the tile in a readable range on both, which was the actual goal
 * — a minimum width was only ever a proxy for it.
 */
@Composable
private fun SeriesGrid(
    series: List<LibrarySeries>,
    newCounts: Map<String, Int>,
    checking: Set<String>,
    onOpen: (LibrarySeries) -> Unit,
) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val columns = ((maxWidth - 28.dp) / TILE_TARGET).toInt().coerceIn(3, 6)
        LazyVerticalGrid(
            columns = GridCells.Fixed(columns),
            contentPadding = PaddingValues(start = 14.dp, end = 14.dp, top = 12.dp, bottom = 28.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            items(series, key = { it.folder }) { entry ->
                SeriesCard(
                    series = entry,
                    newCount = newCounts[entry.folder] ?: 0,
                    checking = entry.folder in checking,
                    onClick = { onOpen(entry) },
                )
            }
        }
    }
}

@Composable
private fun SeriesCard(
    series: LibrarySeries,
    newCount: Int,
    checking: Boolean,
    onClick: () -> Unit,
) {
    Column(
        Modifier
            .clip(MaterialTheme.shapes.medium)
            .clickable(onClick = onClick)
            .padding(bottom = 2.dp),
    ) {
        CoverTile(
            series = series,
            newCount = newCount,
            checking = checking,
            modifier = Modifier.fillMaxWidth().aspectRatio(3f / 4f),
        )
        Spacer(Modifier.height(6.dp))
        Text(
            series.name,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 1.dp),
        )
        Spacer(Modifier.height(2.dp))
        Text(
            buildString {
                if (series.chapters > 0) append("${series.chapters} ch")
                if (series.size > 0) {
                    if (isNotEmpty()) append(" · ")
                    append(formatSize(series.size))
                }
            },
            style = AioText.numericSmall,
            color = MaterialTheme.aio.mutedForeground,
            maxLines = 1,
            modifier = Modifier.padding(horizontal = 1.dp),
        )
    }
}

/**
 * The cover with its overlays.
 *
 * EXTRACTED from [SeriesCard] so the AnimatedVisibility calls below sit in a
 * plain BoxScope — inside the card's Column they would resolve against the
 * ColumnScope overload. Same trap as AioApp.kt's BadgedIcon.
 *
 * The status band rides a bottom scrim ON the cover rather than floating as a
 * corner badge. Two reasons: at ~120dp wide there is no second corner to spare
 * once "+N new" has one, and cover art almost always wastes its lower edge — so
 * the scrim costs nothing and guarantees the text is legible over any artwork.
 */
@Composable
private fun CoverTile(
    series: LibrarySeries,
    newCount: Int,
    checking: Boolean,
    modifier: Modifier = Modifier,
) {
    Box(modifier) {
        CoverArt(series = series, modifier = Modifier.fillMaxSize())

        if (series.status.isNotBlank()) {
            Box(
                Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .height(32.dp)
                    .clip(MaterialTheme.shapes.medium)
                    .background(
                        Brush.verticalGradient(
                            listOf(Color.Transparent, Color.Black.copy(alpha = 0.66f)),
                        ),
                    ),
            )
            Text(
                series.status.uppercase(),
                style = AioText.sectionHeader,
                color = Color.White.copy(alpha = 0.92f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .padding(start = 7.dp, end = 7.dp, bottom = 5.dp),
            )
        }

        // Filled, unlike every other pill in the app: this one is a
        // notification rather than a label, and it has to win against whatever
        // the cover art is doing behind it.
        AnimatedVisibility(
            visible = newCount > 0,
            enter = fadeIn(tween(AioMotion.COLOR_MS)),
            exit = fadeOut(tween(AioMotion.COLOR_MS)),
            modifier = Modifier.align(Alignment.TopEnd).padding(5.dp),
        ) {
            Text(
                "+$newCount",
                style = MaterialTheme.typography.labelSmall,
                color = Color.White,
                modifier = Modifier
                    .clip(CircleShape)
                    .background(MaterialTheme.aio.warning)
                    .padding(horizontal = 6.dp, vertical = 2.dp),
            )
        }

        AnimatedVisibility(
            visible = checking,
            enter = fadeIn(tween(AioMotion.COLOR_MS)),
            exit = fadeOut(tween(AioMotion.COLOR_MS)),
            modifier = Modifier.align(Alignment.TopStart).padding(6.dp),
        ) {
            // Pulses, because a static dot reads as a status and this is a
            // process. It only exists while a check is in flight, so the
            // animation cost is bounded by the sweep.
            Box(
                Modifier
                    .size(9.dp)
                    .clip(CircleShape)
                    .background(pulsingTint(MaterialTheme.colorScheme.primary)),
            )
        }
    }
}

// ── empty + loading states ─────────────────────────────────────────────────

/**
 * Skeleton tiles, not a spinner. The first scan of a large library takes a
 * moment, and a skeleton says "this is going to be a grid" — which is true, and
 * more than a spinner says.
 */
@Composable
private fun LoadingGrid() {
    val shimmer by rememberInfiniteTransition(label = "skeleton").animateFloat(
        initialValue = 0.25f,
        targetValue = 0.55f,
        animationSpec = infiniteRepeatable(
            tween(900, easing = AioMotion.EaseOut),
            RepeatMode.Reverse,
        ),
        label = "skeletonAlpha",
    )
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val columns = ((maxWidth - 28.dp) / TILE_TARGET).toInt().coerceIn(3, 6)
        LazyVerticalGrid(
            columns = GridCells.Fixed(columns),
            contentPadding = PaddingValues(start = 14.dp, end = 14.dp, top = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
            userScrollEnabled = false,
        ) {
            items(columns * 3) {
                Column {
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .aspectRatio(3f / 4f)
                            .clip(MaterialTheme.shapes.medium)
                            .background(MaterialTheme.aio.secondary.copy(alpha = shimmer)),
                    )
                    Spacer(Modifier.height(7.dp))
                    Box(
                        Modifier
                            .fillMaxWidth(0.8f)
                            .height(9.dp)
                            .clip(CircleShape)
                            .background(MaterialTheme.aio.secondary.copy(alpha = shimmer)),
                    )
                }
            }
        }
    }
}

@Composable
private fun CenteredState(
    icon: ImageVector,
    title: String,
    body: String,
    action: (@Composable () -> Unit)? = null,
) {
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 36.dp, vertical = 64.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(
            Modifier
                .size(56.dp)
                .clip(CircleShape)
                .background(MaterialTheme.aio.secondary.copy(alpha = 0.6f)),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                icon,
                contentDescription = null,
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier.size(24.dp),
            )
        }
        Spacer(Modifier.height(14.dp))
        Text(title, style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(5.dp))
        HelpText(body)
        action?.let {
            Spacer(Modifier.height(16.dp))
            it()
        }
    }
}

@Composable
private fun EmptyLibrary(onNewDownload: () -> Unit) {
    CenteredState(
        icon = Icons.AutoMirrored.Filled.LibraryBooks,
        title = "Nothing downloaded yet",
        body = "Series you download land here, covers and all. " +
            "Paste a URL on the New tab to start one.",
        action = {
            AioButton(
                text = "New download",
                icon = Icons.Filled.Download,
                tone = ButtonTone.Outline,
                compact = true,
                onClick = onNewDownload,
            )
        },
    )
}

@Composable
private fun NoMatches(query: String, onClear: () -> Unit) {
    CenteredState(
        icon = Icons.Filled.FilterList,
        title = "No matches",
        body = if (query.isBlank()) {
            "Nothing in the library matches every active filter."
        } else {
            "Nothing matches these filters and “$query”."
        },
        action = {
            AioButton(
                text = "Clear filters",
                tone = ButtonTone.Outline,
                compact = true,
                onClick = onClear,
            )
        },
    )
}

@Composable
private fun NoSearchResults(query: String) {
    CenteredState(
        icon = Icons.Filled.Search,
        title = "No matches",
        body = "Nothing in the library is called “$query”.",
    )
}

// ── filter panel ───────────────────────────────────────────────────────────

/**
 * The facet sheet, hand-rolled on a scrim rather than built on ModalBottomSheet.
 *
 * Same call that kept AioSelect off ExposedDropdownMenuBox: the Material
 * component is still experimental, and here it would additionally impose its own
 * drag handle, insets and shape over a panel that wants the app's card
 * treatment. A scrim plus a slide-in Column is twenty lines and behaves the same
 * on every device.
 *
 * FlowRow IS used, and that OptIn is worth it: the alternative is a custom
 * Layout, and wrapping chips is precisely what it exists for.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun FilterPanel(
    open: Boolean,
    facets: Map<FacetGroup, List<FacetValue>>,
    filters: LibraryFilters,
    matched: Int,
    total: Int,
    onChange: (LibraryFilters) -> Unit,
    onClose: () -> Unit,
    scrimModifier: Modifier,
    panelModifier: Modifier,
) {
    AnimatedVisibility(
        visible = open,
        enter = fadeIn(tween(160)),
        exit = fadeOut(tween(160)),
        modifier = scrimModifier,
    ) {
        Box(
            Modifier
                .fillMaxSize()
                .background(Color.Black.copy(alpha = 0.45f))
                .clickable(onClick = onClose),
        )
    }

    AnimatedVisibility(
        visible = open,
        enter = slideInVertically(tween(220, easing = AioMotion.EaseOut)) { it },
        exit = slideOutVertically(tween(180, easing = AioMotion.EaseOut)) { it },
        modifier = panelModifier,
    ) {
        var needle by remember { mutableStateOf("") }
        val spoilerTags = facets[FacetGroup.Tags].orEmpty().count { it.spoiler }

        Column(
            Modifier
                .fillMaxWidth()
                .clip(MaterialTheme.shapes.large)
                .background(MaterialTheme.aio.card)
                .border(1.dp, MaterialTheme.aio.border, MaterialTheme.shapes.large),
        ) {
            Row(
                Modifier.fillMaxWidth().padding(start = 18.dp, end = 8.dp, top = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "Filter",
                    style = MaterialTheme.typography.titleLarge,
                    modifier = Modifier.weight(1f),
                )
                Pill("$matched / $total", mono = true)
                Icon(
                    Icons.Filled.Close,
                    contentDescription = "Close filters",
                    tint = MaterialTheme.aio.mutedForeground,
                    modifier = Modifier
                        .clip(CircleShape)
                        .clickable(onClick = onClose)
                        .padding(11.dp)
                        .size(18.dp),
                )
            }

            Column(Modifier.padding(horizontal = 18.dp)) {
                Spacer(Modifier.height(10.dp))
                AioTextField(
                    value = needle,
                    onValueChange = { needle = it },
                    placeholder = "Find a genre, tag or source…",
                    minHeight = 42,
                    leading = {
                        Icon(
                            Icons.Filled.Search,
                            contentDescription = null,
                            tint = MaterialTheme.aio.mutedForeground,
                            modifier = Modifier.size(15.dp),
                        )
                    },
                )
                SwitchRow(
                    label = "Match every selected genre and tag",
                    checked = filters.matchAll,
                    onCheckedChange = { onChange(filters.copy(matchAll = it)) },
                    // Status and source are single-valued, so ANDing them could
                    // never match anything. They stay OR regardless, and saying
                    // so beats letting someone discover it by emptying the grid.
                    hint = "Off matches any one of them. Status and source always match any.",
                )
            }

            Column(
                Modifier
                    .heightIn(max = 380.dp)
                    .verticalScroll(rememberScrollState())
                    .padding(horizontal = 18.dp),
            ) {
                val trimmed = needle.trim().lowercase()
                FacetGroup.entries.forEach { group ->
                    val values = facets[group].orEmpty().filter { value ->
                        (trimmed.isEmpty() || value.search.contains(trimmed)) &&
                            // A selected spoiler tag stays visible even with the
                            // gate closed, or it could never be deselected.
                            (
                                !value.spoiler ||
                                    filters.showSpoilerTags ||
                                    value.key in filters.selected(group)
                                )
                    }
                    if (values.isEmpty()) return@forEach

                    Spacer(Modifier.height(14.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            group.label.uppercase(),
                            style = AioText.sectionHeader,
                            color = MaterialTheme.aio.mutedForeground,
                        )
                        Spacer(Modifier.width(10.dp))
                        Box(
                            Modifier
                                .weight(1f)
                                .height(1.dp)
                                .background(MaterialTheme.aio.border),
                        )
                    }
                    Spacer(Modifier.height(9.dp))
                    FlowRow(
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        verticalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        values.forEach { value ->
                            FacetChip(
                                value = value,
                                selected = value.key in filters.selected(group),
                                onClick = { onChange(filters.toggle(group, value.key)) },
                            )
                        }
                    }
                }

                if (spoilerTags > 0 && !filters.showSpoilerTags) {
                    Spacer(Modifier.height(14.dp))
                    Text(
                        "Show $spoilerTags spoiler tag${if (spoilerTags == 1) "" else "s"}",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier
                            .clip(MaterialTheme.shapes.extraSmall)
                            .clickable { onChange(filters.copy(showSpoilerTags = true)) }
                            .padding(vertical = 6.dp),
                    )
                }
                Spacer(Modifier.height(14.dp))
            }

            Hairline()
            Row(
                Modifier.fillMaxWidth().padding(18.dp),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                AioButton(
                    text = "Clear",
                    tone = ButtonTone.Outline,
                    enabled = filters.activeCount > 0,
                    onClick = { onChange(filters.cleared()) },
                    modifier = Modifier.weight(1f),
                )
                AioButton(text = "Done", onClick = onClose, modifier = Modifier.weight(1f))
            }
        }
    }
}

@Composable
private fun FacetChip(value: FacetValue, selected: Boolean, onClick: () -> Unit) {
    val accent = if (selected) MaterialTheme.colorScheme.primary else MaterialTheme.aio.border
    Row(
        Modifier
            .clip(CircleShape)
            .background(
                if (selected) {
                    MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)
                } else {
                    Color.Transparent
                },
            )
            .border(1.dp, accent, CircleShape)
            .clickable(onClick = onClick)
            .heightIn(min = 34.dp)
            .padding(horizontal = 11.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            value.label,
            style = MaterialTheme.typography.labelMedium,
            color = if (selected) {
                MaterialTheme.colorScheme.primary
            } else {
                MaterialTheme.colorScheme.onBackground
            },
            maxLines = 1,
        )
        Spacer(Modifier.width(6.dp))
        Text(
            value.count.toString(),
            style = AioText.numericSmall,
            color = MaterialTheme.aio.mutedForeground,
        )
    }
}
