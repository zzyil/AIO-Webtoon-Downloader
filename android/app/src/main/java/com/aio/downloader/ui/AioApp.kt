package com.aio.downloader.ui

import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.automirrored.filled.LibraryBooks
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Terminal
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aio.downloader.DownloadService
import com.aio.downloader.core.AppSettings
import com.aio.downloader.core.AppSettingsStore
import com.aio.downloader.core.DownloadRepository
import com.aio.downloader.core.LogLevel
import com.aio.downloader.core.LibraryRepository
import com.aio.downloader.core.LogTail
import com.aio.downloader.core.ResumeRepository
import com.aio.downloader.ui.components.Hairline
import com.aio.downloader.ui.screens.DiagnosticsSheet
import com.aio.downloader.ui.screens.DownloadScreen
import com.aio.downloader.ui.screens.LibraryScreen
import com.aio.downloader.ui.screens.LogsScreen
import com.aio.downloader.ui.screens.QueueScreen
import com.aio.downloader.ui.screens.SearchScreen
import com.aio.downloader.ui.screens.SettingsScreen
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.aio

/**
 * The app shell.
 *
 * PORTING THE DESKTOP'S LEFT ICON RAIL: on the desktop this is a 64px column of
 * icon+label buttons, active state `bg-primary/10 text-primary` plus a 3px
 * indicator bar on the left edge. On a phone that column would eat a sixth of
 * the width and put the primary navigation where no thumb reaches, so it
 * becomes a bottom bar — with the SAME tint, the SAME badge, and the indicator
 * rotated to the top edge of the item. On a tablet in landscape there is room
 * for the real thing, so the rail comes back.
 *
 * The switch is [BoxWithConstraints] rather than WindowSizeClass on purpose: it
 * measures the actual container, so split-screen and freeform windows get the
 * right answer without the Activity having to be involved.
 */

/**
 * Library leads, and is where the app opens.
 *
 * The desktop puts Library on the APP ICON above its five-tab rail rather than
 * in the rail itself — its own way of saying the library is home, forced by a
 * 64px column that already had five entries. With four tabs there is room to say
 * it directly. Landing there also warms the interpreter (the scan is the first
 * Python call) behind a skeleton grid, so the first download starts sooner.
 */
private enum class Tab(val label: String, val icon: ImageVector) {
    Library("Library", Icons.AutoMirrored.Filled.LibraryBooks),
    Search("Search", Icons.Filled.Search),
    New("New", Icons.Filled.Download),
    Queue("Queue", Icons.AutoMirrored.Filled.List),
    Logs("Logs", Icons.Filled.Terminal),
}

@Composable
fun AioApp() {
    val vm: AioViewModel = viewModel()
    val context = LocalContext.current
    var tab by rememberSaveable { mutableStateOf(Tab.Library) }
    var showDiagnostics by rememberSaveable { mutableStateOf(false) }
    // Settings is a DESTINATION over the shell rather than a sixth tab — see
    // SettingsScreen's header. rememberSaveable so a rotation mid-edit does not
    // dump the user back to the tab underneath.
    var showSettings by rememberSaveable { mutableStateOf(false) }
    val settings by AppSettingsStore.settings.collectAsStateWithLifecycle()

    // Load once, up front. Without this the first read would be the one inside
    // the Settings branch below — which publishes to the StateFlow DURING
    // composition and recomposes the whole shell on the way in.
    LaunchedEffect(Unit) { AppSettingsStore.current(context) }

    val queue by DownloadRepository.queue.collectAsStateWithLifecycle()
    val active by DownloadRepository.active.collectAsStateWithLifecycle()
    val history by DownloadRepository.history.collectAsStateWithLifecycle()
    val logLines by LogTail.lines.collectAsStateWithLifecycle()
    val logError by LogTail.error.collectAsStateWithLifecycle()
    // The one piece of library state the SHELL needs; LibraryScreen collects the
    // rest itself (see its header).
    val libraryUpdates by LibraryRepository.updates.collectAsStateWithLifecycle()
    val allResumable by ResumeRepository.runs.collectAsStateWithLifecycle()

    // Everything on disk minus what is already queued or running: a resume of
    // the download currently downloading is not an offer worth making.
    val resumable = ResumeRepository.visible(allResumable, active, queue)

    // Unfinished work counts toward the Queue badge. A badge means "there is
    // something here for you", and a half-downloaded series is the clearest
    // case of that in the app — it is also the only one that will sit there
    // forever if nobody looks.
    val queueCount = queue.size + resumable.size + if (active != null) 1 else 0
    val updateCount = libraryUpdates.values.count { it.hasUpdates }
    // Mirrors the desktop's red dot on the Logs tab: the most recent line is an
    // error and you are not already looking at it.
    val logAlert = tab != Tab.Logs && logLines.lastOrNull()?.level == LogLevel.Error

    // A finished download changes what is on disk. Rescan — but only a library
    // that has already been looked at, so a user who never opens the tab never
    // pays for a disk walk they did not ask for.
    LaunchedEffect(history.size) {
        if (history.isNotEmpty() && LibraryRepository.series.value != null) {
            LibraryRepository.refresh(context)
        }
        // Unfinished work changes too, and in BOTH directions: a completed run
        // removes its tmp folder, a cancelled one leaves a resumable behind.
        // This is what puts the badge up the moment a download is cancelled,
        // rather than the next time the Queue tab is opened.
        if (history.isNotEmpty()) ResumeRepository.refresh(context)
    }

    BoxWithConstraints(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        val wide = maxWidth >= 600.dp

        Row(Modifier.fillMaxSize()) {
            if (wide) {
                NavRail(
                    current = tab,
                    onSelect = { tab = it },
                    queueCount = queueCount,
                    updateCount = updateCount,
                    logAlert = logAlert,
                )
                Box(
                    Modifier
                        .width(1.dp)
                        .fillMaxHeight()
                        .background(MaterialTheme.aio.border),
                )
            }

            Column(Modifier.weight(1f)) {
                TopBar(
                    title = tab.label,
                    onSettings = { showSettings = true },
                )

                Box(Modifier.weight(1f)) {
                    when (tab) {
                        Tab.Library -> LibraryScreen(
                            onQueueUpdate = { series, chapters ->
                                if (vm.queueLibraryUpdate(series, chapters)) tab = Tab.Queue
                            },
                            onNewDownload = { tab = Tab.New },
                        )

                        // Search hands its winner to the New tab rather than
                        // downloading directly: the user still has to choose a
                        // format, chapter range and the rest, and duplicating
                        // that form here would be two places to keep correct.
                        Tab.Search -> SearchScreen(
                            onDownload = { url ->
                                vm.update { it.copy(url = url) }
                                vm.persist()
                                tab = Tab.New
                            },
                            // The device-level settings ONLY. A search reads the
                            // resource limits and the search tuning; it has no
                            // business seeing a half-composed download form,
                            // which is what it used to be handed.
                            settingsJson = {
                                AppSettingsStore.current(context).toGlobalSettingsJson()
                            },
                        )

                        Tab.New -> DownloadScreen(
                            form = vm.form,
                            onFormChange = vm::update,
                            onStart = vm::startDownloads,
                            onPersist = vm::persist,
                            onSeeQueue = { tab = Tab.Queue },
                            busy = active != null,
                            queued = queue.size,
                        )

                        Tab.Queue -> QueueScreen(
                            active = active,
                            queue = queue,
                            history = history,
                            resumable = resumable,
                            onCancelActive = rememberCancelAction(),
                            onRemoveQueued = DownloadRepository::removeQueued,
                            onClearHistory = DownloadRepository::clearHistory,
                            onNewDownload = { tab = Tab.New },
                        )

                        Tab.Logs -> LogsScreen(
                            lines = logLines,
                            error = logError,
                            onClear = LogTail::clear,
                        )
                    }
                }

                if (!wide) {
                    BottomBar(
                        current = tab,
                        onSelect = { tab = it },
                        queueCount = queueCount,
                        updateCount = updateCount,
                        logAlert = logAlert,
                    )
                }
            }
        }

        if (showSettings) {
            SettingsOverlay(
                settings = settings ?: AppSettingsStore.current(context),
                onChange = { transform -> AppSettingsStore.update(context, transform) },
                onDiagnostics = { showDiagnostics = true },
                onClose = { showSettings = false },
            )
        }

        // Outside the Settings branch on purpose: the sheet is reached FROM
        // Settings and has to draw on top of it, not instead of it.
        if (showDiagnostics) {
            DiagnosticsSheet(onDismiss = { showDiagnostics = false })
        }
    }
}

/**
 * The cancel action needs a Context, and threading one through every screen
 * signature to reach a single button is worse than fetching it at the one place
 * that uses it.
 */
@Composable
private fun rememberCancelAction(): () -> Unit {
    val context = LocalContext.current
    return remember(context) { { DownloadService.cancel(context) } }
}

// ── chrome ─────────────────────────────────────────────────────────────────

/**
 * `px-5 py-3 border-b bg-card/30` with a `text-sm font-semibold tracking-wide`
 * title, plus one affordance on the right.
 *
 * That affordance used to open Diagnostics directly and now opens Settings,
 * which carries Diagnostics inside it. One icon rather than two: Diagnostics is
 * a thing you need once a year with a broken app, Settings is a thing you look
 * for by habit, and two grey glyphs in a corner is how neither gets found.
 */
@Composable
private fun TopBar(title: String, onSettings: () -> Unit) {
    Column {
        Spacer(Modifier.height(WindowInsets.statusBars.asPaddingValues().calculateTopPadding()))
        Row(
            Modifier
                .fillMaxWidth()
                .background(MaterialTheme.aio.card.copy(alpha = 0.3f))
                .padding(start = 20.dp, end = 8.dp, top = 12.dp, bottom = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                title,
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.weight(1f),
            )
            Icon(
                Icons.Filled.Settings,
                contentDescription = "Settings",
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier
                    .clip(CircleShape)
                    .clickable(onClick = onSettings)
                    .padding(10.dp)
                    .size(20.dp),
            )
        }
        Hairline()
    }
}

/**
 * Settings, over everything, with its own bar and the system Back gesture.
 *
 * A full-bleed overlay rather than a navigation graph: this app has exactly one
 * push destination, and adding navigation-compose for it would mean a route
 * table, a NavHost and argument encoding to express `showSettings = true`.
 */
@Composable
private fun SettingsOverlay(
    settings: AppSettings,
    onChange: ((AppSettings) -> AppSettings) -> Unit,
    onDiagnostics: () -> Unit,
    onClose: () -> Unit,
) {
    BackHandler(onBack = onClose)
    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
    ) {
        Spacer(Modifier.height(WindowInsets.statusBars.asPaddingValues().calculateTopPadding()))
        Row(
            Modifier
                .fillMaxWidth()
                .background(MaterialTheme.aio.card.copy(alpha = 0.3f))
                .padding(start = 8.dp, end = 20.dp, top = 12.dp, bottom = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                Icons.AutoMirrored.Filled.ArrowBack,
                contentDescription = "Back",
                tint = MaterialTheme.colorScheme.onBackground,
                modifier = Modifier
                    .clip(CircleShape)
                    .clickable(onClick = onClose)
                    .padding(10.dp)
                    .size(20.dp),
            )
            Spacer(Modifier.width(4.dp))
            Text("Settings", style = MaterialTheme.typography.titleMedium)
        }
        Hairline()
        Box(Modifier.weight(1f)) {
            SettingsScreen(
                settings = settings,
                onChange = onChange,
                onDiagnostics = onDiagnostics,
            )
        }
        Spacer(
            Modifier.height(
                WindowInsets.navigationBars.asPaddingValues().calculateBottomPadding(),
            ),
        )
    }
}

/**
 * Which number, if any, a tab carries. Queue counts work waiting to happen;
 * Library counts series with new chapters found by the last check — both are
 * "there is something here for you", which is the only thing a badge should
 * ever mean.
 */
private fun badgeFor(tab: Tab, queueCount: Int, updateCount: Int): Int = when (tab) {
    Tab.Queue -> queueCount
    Tab.Library -> updateCount
    else -> 0
}

@Composable
private fun BottomBar(
    current: Tab,
    onSelect: (Tab) -> Unit,
    queueCount: Int,
    updateCount: Int,
    logAlert: Boolean,
) {
    Column {
        Hairline()
        Row(
            Modifier
                .fillMaxWidth()
                .background(MaterialTheme.aio.card.copy(alpha = 0.5f))
                .padding(
                    bottom = WindowInsets.navigationBars.asPaddingValues()
                        .calculateBottomPadding(),
                ),
            horizontalArrangement = Arrangement.SpaceEvenly,
        ) {
            Tab.entries.forEach { entry ->
                NavItem(
                    tab = entry,
                    active = entry == current,
                    onClick = { onSelect(entry) },
                    badge = badgeFor(entry, queueCount, updateCount),
                    alert = entry == Tab.Logs && logAlert,
                    // Bottom bar: the desktop's 3px left-edge indicator becomes
                    // a top-edge one, which is where the eye looks for it when
                    // the strip runs horizontally.
                    indicatorOnTop = true,
                    modifier = Modifier.weight(1f),
                )
            }
        }
    }
}

@Composable
private fun NavRail(
    current: Tab,
    onSelect: (Tab) -> Unit,
    queueCount: Int,
    updateCount: Int,
    logAlert: Boolean,
) {
    Column(
        Modifier
            .width(72.dp)
            .fillMaxHeight()
            .background(MaterialTheme.aio.card.copy(alpha = 0.5f))
            .padding(
                top = WindowInsets.statusBars.asPaddingValues().calculateTopPadding() + 12.dp,
            ),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Tab.entries.forEach { entry ->
            NavItem(
                tab = entry,
                active = entry == current,
                onClick = { onSelect(entry) },
                badge = if (entry == Tab.Queue) queueCount else 0,
                alert = entry == Tab.Logs && logAlert,
                indicatorOnTop = false,
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

/**
 * One rail/bar entry. Icon over a 10sp label, primary tint when active, a count
 * badge on Queue and an error dot on Logs — all four straight from
 * `App.jsx`'s rail button.
 */
@Composable
private fun NavItem(
    tab: Tab,
    active: Boolean,
    onClick: () -> Unit,
    badge: Int,
    alert: Boolean,
    indicatorOnTop: Boolean,
    modifier: Modifier = Modifier,
) {
    val tint by animateColorAsState(
        if (active) MaterialTheme.colorScheme.primary else MaterialTheme.aio.mutedForeground,
        tween(AioMotion.COLOR_MS),
        label = "navTint",
    )
    val fill by animateColorAsState(
        if (active) MaterialTheme.colorScheme.primary.copy(alpha = 0.10f) else Color.Transparent,
        tween(AioMotion.COLOR_MS),
        label = "navFill",
    )

    Box(modifier.clickable(onClick = onClick), contentAlignment = Alignment.Center) {
        Column(
            Modifier
                .padding(vertical = 6.dp, horizontal = 4.dp)
                .clip(MaterialTheme.shapes.medium)
                .background(fill)
                .padding(vertical = 8.dp, horizontal = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            BadgedIcon(tab = tab, tint = tint, badge = badge, alert = alert)
            Spacer(Modifier.height(3.dp))
            Text(tab.label, style = MaterialTheme.typography.labelSmall, color = tint)
        }

        // The indicator bar. 3px, rounded on the inner end — the desktop's
        // `rounded-r-full` spine, reoriented for whichever edge it sits on.
        AnimatedVisibility(
            visible = active,
            enter = fadeIn(tween(AioMotion.COLOR_MS)),
            exit = fadeOut(tween(AioMotion.COLOR_MS)),
            modifier = Modifier.align(
                if (indicatorOnTop) Alignment.TopCenter else Alignment.CenterStart,
            ),
        ) {
            if (indicatorOnTop) {
                Box(
                    Modifier
                        .width(26.dp)
                        .height(3.dp)
                        .background(
                            MaterialTheme.colorScheme.primary,
                            RoundedCornerShape(bottomStart = 3.dp, bottomEnd = 3.dp),
                        ),
                )
            } else {
                Box(
                    Modifier
                        .width(3.dp)
                        .height(26.dp)
                        .background(
                            MaterialTheme.colorScheme.primary,
                            RoundedCornerShape(topEnd = 3.dp, bottomEnd = 3.dp),
                        ),
                )
            }
        }
    }
}

/**
 * The tab icon with its queue count and error dot.
 *
 * EXTRACTED RATHER THAN INLINED: nested inside NavItem's Column, the badges'
 * `AnimatedVisibility` calls resolved to the `ColumnScope` overload while only
 * `BoxScope` was innermost, which is a compile error ("cannot be called in this
 * context with an implicit receiver"). Pulling the Box into its own composable
 * removes the outer ColumnScope from the resolution set. Same trick is used in
 * LogsScreen for the jump-to-latest button.
 */
@Composable
private fun BadgedIcon(tab: Tab, tint: Color, badge: Int, alert: Boolean) {
    Box {
        Icon(
            tab.icon,
            contentDescription = tab.label,
            tint = tint,
            modifier = Modifier.size(21.dp),
        )

        // Count badge — a primary pill, exactly the desktop's.
        AnimatedVisibility(
            visible = badge > 0,
            enter = fadeIn(tween(AioMotion.COLOR_MS)),
            exit = fadeOut(tween(AioMotion.COLOR_MS)),
            modifier = Modifier.align(Alignment.TopEnd),
        ) {
            Box(
                Modifier
                    .offsetBadge()
                    .background(MaterialTheme.colorScheme.primary, CircleShape)
                    .padding(horizontal = 4.dp, vertical = 1.dp),
            ) {
                Text(
                    badge.toString(),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onPrimary,
                )
            }
        }

        // Error dot on Logs.
        AnimatedVisibility(
            visible = alert,
            enter = fadeIn(tween(AioMotion.COLOR_MS)),
            exit = fadeOut(tween(AioMotion.COLOR_MS)),
            modifier = Modifier.align(Alignment.TopEnd),
        ) {
            Box(
                Modifier
                    .offsetBadge()
                    .size(7.dp)
                    .background(MaterialTheme.colorScheme.error, CircleShape),
            )
        }
    }
}

/** Nudge a badge off the icon's corner without a magic number at each use site. */
private fun Modifier.offsetBadge(): Modifier = this.then(Modifier.padding(start = 8.dp))
