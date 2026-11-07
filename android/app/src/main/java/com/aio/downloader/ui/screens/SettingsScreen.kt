package com.aio.downloader.ui.screens

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.FolderOpen
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
// androidx.lifecycle.compose, NOT androidx.compose.ui.platform: the ui.platform
// copy was deprecated when the lifecycle artifacts took ownership of it.
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aio.downloader.core.Aio
import com.aio.downloader.core.AppSettings
import com.aio.downloader.core.AppSettingsStore
import com.aio.downloader.core.CoverStore
import com.aio.downloader.core.RESOURCE_LEVELS
import com.aio.downloader.core.StorageProbe
import com.aio.downloader.core.probeLibraryRoot
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.AioCard
import com.aio.downloader.ui.components.AioSelect
import com.aio.downloader.ui.components.AioTextField
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.CalloutTone
import com.aio.downloader.ui.components.CheckRow
import com.aio.downloader.ui.components.Collapsible
import com.aio.downloader.ui.components.FieldLabel
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.components.HelpTextWithCode
import com.aio.downloader.ui.components.Pill
import com.aio.downloader.ui.components.PillTone
import com.aio.downloader.ui.components.SectionHeader
import com.aio.downloader.ui.components.SliderRow
import com.aio.downloader.ui.components.StatusCallout
import com.aio.downloader.ui.components.SwitchRow
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.aio
import kotlinx.coroutines.launch
import java.io.File

/**
 * The device-level preferences — the desktop's Settings tab, cut to what a
 * phone can actually act on.
 *
 * ── WHY IT IS NOT A SIXTH TAB ──────────────────────────────────────────────
 * The desktop puts Settings in its 64px rail, which has room. A bottom bar does
 * not: five is already the practical maximum before the labels stop being
 * readable, and Settings is a place you visit rarely and leave — a destination,
 * not a mode. So it opens over the shell from the top bar and takes the system
 * Back gesture, which is what every other Android app does with the same
 * screen.
 *
 * ── WHAT IS AND IS NOT HERE ────────────────────────────────────────────────
 * Only things that describe the DEVICE. Format, chapter range, groups and the
 * rest stay on the New tab, because they are properties of a download. The
 * split is [AppSettings] vs `DownloadForm`; see the former's header.
 *
 * Deliberately absent, and worth knowing why rather than rediscovering:
 *  - The disabled-sites block list. The desktop's is a scrolling table over 297
 *    handlers with per-site health; the one site that genuinely cannot work
 *    here (comix) is already force-excluded in Python.
 *  - Paths & Python, App Updates, Modernize. The first two are Electron
 *    concerns; the third has no Android wheel behind it (no pillow-jxl).
 *
 * Cross-file: core/AppSettings.kt (the model), core/AppSettingsStore.kt (the
 * holder), aio_android.probe_library_root (the storage verdict).
 */
@Composable
fun SettingsScreen(
    settings: AppSettings,
    onChange: ((AppSettings) -> AppSettings) -> Unit,
    onDiagnostics: () -> Unit,
) {
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp),
    ) {
        StorageSection(settings, onChange)
        LimitsSection(settings, onChange)
        SearchSection(settings, onChange)
        LibrarySection(settings, onChange)
        AboutSection(onDiagnostics, onChange)
        Spacer(Modifier.height(32.dp))
    }
}

// ── storage ────────────────────────────────────────────────────────────────

/**
 * Where the library lives, and the MANAGE_EXTERNAL_STORAGE opt-in.
 *
 * THE WHOLE POINT of a custom path is that Komikku/Mihon can READ the library —
 * an app-scoped directory is invisible to them, so `--komikku` output lands
 * somewhere no reader can open. That is also why SAF is not the mechanism:
 * it hands back `content://` URIs and Python cannot `open()` one.
 *
 * The flow is deliberately three separate steps rather than one button, because
 * each can fail on its own and for its own reason: grant the permission, name a
 * folder, prove the downloader can write there. The probe is the honest one —
 * a granted permission is not a writable directory, and finding that out at the
 * first chapter write is finding out too late.
 */
@Composable
private fun StorageSection(
    settings: AppSettings,
    onChange: ((AppSettings) -> AppSettings) -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var draft by remember(settings.libraryPath) {
        mutableStateOf(settings.libraryPath.ifBlank { suggestedSharedPath() })
    }
    var probe by remember { mutableStateOf<StorageProbe?>(null) }
    var probing by remember { mutableStateOf(false) }
    val granted = rememberAllFilesAccess()
    val usingCustom = settings.libraryPath.isNotBlank()
    val fallback = Aio.libraryFallbackReason

    SectionHeader("Storage")

    AioCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            FieldLabel("Library folder", modifier = Modifier.weight(1f))
            Pill(
                if (usingCustom) "Shared" else "App folder",
                tone = if (usingCustom) PillTone.Primary else PillTone.Neutral,
            )
        }
        Spacer(Modifier.height(6.dp))
        Text(
            Aio.libraryDir(context).absolutePath,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.aio.mutedForeground,
        )
        Spacer(Modifier.height(8.dp))
        HelpText(
            if (usingCustom) {
                "Other apps can read this folder, so Komikku and Mihon can open the library."
            } else {
                "Private to this app. No permission needed, and Android deletes it when " +
                    "the app is uninstalled — but no reader app can see inside it."
            },
        )
    }

    // A configured path that turned out to be unusable. Reported rather than
    // silently absorbed: the symptom is otherwise an empty library, which reads
    // as data loss.
    AnimatedVisibility(fallback != null, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
        Box(Modifier.padding(top = 10.dp)) {
            StatusCallout(
                title = "Using the app's own folder",
                body = fallback.orEmpty(),
                icon = Icons.Filled.Warning,
                tone = CalloutTone.Warning,
            )
        }
    }

    Spacer(Modifier.height(10.dp))
    Collapsible(
        title = "Use a shared folder",
        subtitle = "So Komikku or Mihon can read the library",
    ) {
        if (!granted) {
            StatusCallout(
                title = "Needs all-files access",
                body = "Android only lets an app write outside its own folder with this " +
                    "permission. It's granted in system settings, and you can revoke it " +
                    "there at any time — the app falls back to its own folder if you do.",
                icon = Icons.Filled.FolderOpen,
                tone = CalloutTone.Info,
                action = {
                    AioButton(
                        text = "Grant access",
                        compact = true,
                        onClick = { context.startActivity(allFilesAccessIntent(context)) },
                    )
                },
            )
            Spacer(Modifier.height(12.dp))
        }

        FieldLabel("Folder path", enabled = granted)
        Spacer(Modifier.height(6.dp))
        AioTextField(
            value = draft,
            onValueChange = {
                draft = it
                probe = null
            },
            enabled = granted,
            placeholder = suggestedSharedPath(),
            mono = true,
            keyboardType = KeyboardType.Uri,
        )
        Spacer(Modifier.height(4.dp))
        HelpTextWithCode(
            "A path on internal storage, e.g. ",
            suggestedSharedPath(),
            ". Point it at an existing library and it will be picked up as is.",
        )

        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            AioButton(
                text = if (probing) "Checking…" else "Check folder",
                tone = ButtonTone.Outline,
                compact = true,
                enabled = granted && draft.isNotBlank() && !probing,
                onClick = {
                    probing = true
                    scope.launch {
                        probe = probeLibraryRoot(context, draft)
                        probing = false
                    }
                },
            )
            Spacer(Modifier.width(8.dp))
            AioButton(
                text = "Use this folder",
                icon = Icons.Filled.Check,
                compact = true,
                // Only after a PASSING probe. A "Save anyway" would be a way to
                // configure a path that cannot work, and the failure surfaces
                // an app restart later — far from the tap that caused it.
                enabled = probe?.ok == true,
                onClick = { onChange { it.copy(libraryPath = probe?.path.orEmpty()) } },
            )
        }

        probe?.let { result ->
            Spacer(Modifier.height(10.dp))
            if (result.ok) {
                StatusCallout(
                    title = "Ready to use",
                    body = buildString {
                        append(formatBytes(result.freeBytes))
                        append(" free")
                        if (result.seriesCount > 0) {
                            append(" · ${result.seriesCount} series already here")
                        }
                        append(". The library moves on the next app start.")
                    },
                    icon = Icons.Filled.Check,
                    tone = CalloutTone.Success,
                )
            } else {
                StatusCallout(
                    title = "Can't use that folder",
                    body = result.message,
                    icon = Icons.Filled.Warning,
                    tone = CalloutTone.Warning,
                )
            }
        }

        AnimatedVisibility(usingCustom, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
            Column {
                Spacer(Modifier.height(12.dp))
                AioButton(
                    text = "Back to the app's own folder",
                    tone = ButtonTone.Ghost,
                    compact = true,
                    onClick = { onChange { it.copy(libraryPath = "") } },
                )
            }
        }

        Spacer(Modifier.height(10.dp))
        HelpText(
            "Changing this takes effect when the app next starts, and nothing is moved — " +
                "downloads already in the old folder stay there.",
            tone = MaterialTheme.aio.warning,
        )
    }
}

// ── resource limits ────────────────────────────────────────────────────────

@Composable
private fun LimitsSection(
    settings: AppSettings,
    onChange: ((AppSettings) -> AppSettings) -> Unit,
) {
    SectionHeader("Battery & data")
    FieldLabel("Network")
    Spacer(Modifier.height(6.dp))
    AioSelect(
        options = RESOURCE_LEVELS,
        selected = settings.networkLimit,
        onSelect = { value -> onChange { it.copy(networkLimit = value) } },
    )
    Spacer(Modifier.height(4.dp))
    HelpText(
        "Caps parallel image downloads and how many sites a search contacts at once. " +
            "A hard override — it replaces the per-download worker counts rather than " +
            "combining with them.",
    )

    Spacer(Modifier.height(14.dp))
    FieldLabel("CPU")
    Spacer(Modifier.height(6.dp))
    AioSelect(
        options = RESOURCE_LEVELS,
        selected = settings.cpuLimit,
        onSelect = { value -> onChange { it.copy(cpuLimit = value) } },
    )
    Spacer(Modifier.height(4.dp))
    HelpText("Caps the image-processing pools. Lower means a cooler device and a slower run.")

    Spacer(Modifier.height(8.dp))
    HelpText(
        "Applies to downloads, searches, update checks and resumes alike — including a " +
            "download queued before you changed it.",
        tone = MaterialTheme.colorScheme.primary,
    )
}

// ── search ─────────────────────────────────────────────────────────────────

@Composable
private fun SearchSection(
    settings: AppSettings,
    onChange: ((AppSettings) -> AppSettings) -> Unit,
) {
    SectionHeader("Search")
    SwitchRow(
        label = "Only well-known sources",
        checked = settings.seededOnly,
        onCheckedChange = { value -> onChange { it.copy(seededOnly = value) } },
        hint = "Skips roughly 250 mirror sites that have no measured quality rating. " +
            "Much faster, and most of what it skips is foreign-language noise — but a " +
            "rare series may only exist on one of them. Also applies to multi-source.",
    )

    Spacer(Modifier.height(6.dp))
    SliderRow(
        label = "Per-site timeout",
        value = settings.searchTimeout.toFloat(),
        onValueChange = { value -> onChange { it.copy(searchTimeout = value.toInt()) } },
        valueRange = 5f..45f,
        display = "${settings.searchTimeout}s",
        hint = "How long one site gets before the search moves on without it. Pure-HTTP " +
            "sites answer in under two seconds; the rest self-select out.",
    )

    SliderRow(
        label = "Minimum title match",
        value = settings.searchMinMatch,
        onValueChange = { value ->
            // Snapped to 0.05, matching the desktop. Slider `steps` would round
            // too, but it also draws tick marks the desktop has not got.
            onChange { it.copy(searchMinMatch = kotlin.math.round(value * 20f) / 20f) }
        },
        valueRange = 0.30f..0.95f,
        display = "%.2f".format(settings.searchMinMatch),
        hint = "Hits less similar than this are dropped. Lower finds more, including " +
            "more wrong series.",
    )
}

// ── library ────────────────────────────────────────────────────────────────

@Composable
private fun LibrarySection(
    settings: AppSettings,
    onChange: ((AppSettings) -> AppSettings) -> Unit,
) {
    SectionHeader("Library")
    SwitchRow(
        label = "Treat split chapters as one",
        checked = settings.collapseSplits,
        onCheckedChange = { value -> onChange { it.copy(collapseSplits = value) } },
        hint = "Some sites publish one chapter as 12.1 and 12.2. Without this an update " +
            "check lists both as missing every time, because the download merged them " +
            "into a single file.",
    )
    SwitchRow(
        label = "Check for updates when the library opens",
        checked = settings.autoCheckUpdates,
        onCheckedChange = { value -> onChange { it.copy(autoCheckUpdates = value) } },
        hint = "Checks every series once per app run. One site request per series, in " +
            "sequence — on a big library that is minutes of background work, so it is " +
            "off unless you ask for it.",
    )
}

// ── about ──────────────────────────────────────────────────────────────────

@Composable
private fun AboutSection(
    onDiagnostics: () -> Unit,
    onChange: ((AppSettings) -> AppSettings) -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var cacheNote by remember { mutableStateOf<String?>(null) }
    var confirmReset by remember { mutableStateOf(false) }

    LaunchedEffect(cacheNote) {
        if (cacheNote != null) {
            kotlinx.coroutines.delay(3000)
            cacheNote = null
        }
    }
    LaunchedEffect(confirmReset) {
        if (confirmReset) {
            kotlinx.coroutines.delay(4000)
            confirmReset = false
        }
    }

    SectionHeader("About")
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        AioButton(
            text = "Diagnostics",
            icon = Icons.Filled.Info,
            tone = ButtonTone.Outline,
            onClick = onDiagnostics,
            modifier = Modifier.fillMaxWidth(),
        )
        HelpText(
            "Which handlers loaded, which optional libraries are present, and the " +
                "fuzzy-match fingerprint. The fastest answer to \"why did every site fail\".",
        )

        AioButton(
            text = "Clear cover cache",
            icon = Icons.Filled.Refresh,
            tone = ButtonTone.Outline,
            onClick = {
                scope.launch {
                    val removed = CoverStore.clear(context)
                    cacheNote = if (removed > 0) "Cleared $removed cached covers" else "Nothing cached"
                }
            },
            modifier = Modifier.fillMaxWidth(),
        )
        HelpText(
            "Covers extracted out of archives are cached so the grid does not unzip on " +
                "every scroll. Clearing costs one slower library load, nothing else.",
        )

        AioButton(
            text = if (confirmReset) "Tap again to reset" else "Reset these settings",
            tone = if (confirmReset) ButtonTone.Danger else ButtonTone.Ghost,
            onClick = {
                if (confirmReset) {
                    confirmReset = false
                    // Keeps the library path: a reset means "put the behaviour
                    // back", and repointing the library would strand the
                    // collection behind a restart nobody asked for.
                    onChange { AppSettings(libraryPath = it.libraryPath) }
                } else {
                    confirmReset = true
                }
            },
            modifier = Modifier.fillMaxWidth(),
        )

        AnimatedVisibility(cacheNote != null, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
            HelpText(cacheNote.orEmpty(), tone = MaterialTheme.colorScheme.primary)
        }
    }
}

// ── helpers ────────────────────────────────────────────────────────────────

/**
 * A sensible shared path to pre-fill.
 *
 * `/storage/emulated/0/Manga` in practice. Derived from the public storage root
 * rather than hardcoded because that literal is not guaranteed — a device with
 * a different primary volume returns a different root, and typing the wrong one
 * into the field is exactly the failure the probe then has to explain.
 */
private fun suggestedSharedPath(): String =
    File(Environment.getExternalStorageDirectory(), "Manga").absolutePath

/**
 * Whether MANAGE_EXTERNAL_STORAGE is granted, RE-CHECKED ON RESUME.
 *
 * The re-check is the whole point. The grant happens in the system settings
 * app, which means there is no result callback and nothing in this composition
 * changes when the user comes back — so a plain read would leave the screen
 * saying "Needs all-files access" directly underneath a permission that is now
 * granted, with no way to refresh short of leaving the screen. ON_RESUME is the
 * one moment we are guaranteed to observe.
 *
 * Below API 30 the concept does not exist; broad storage access came from the
 * legacy WRITE_EXTERNAL_STORAGE permission. minSdk here is 26, but on 26-29 an
 * app targeting a modern SDK still gets scoped behaviour — so reporting "not
 * granted" and leaving the user on the app-scoped default is the honest answer
 * rather than offering a flow that cannot complete.
 */
@Composable
private fun rememberAllFilesAccess(): Boolean {
    fun read() =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && Environment.isExternalStorageManager()

    var granted by remember { mutableStateOf(read()) }
    val owner = LocalLifecycleOwner.current
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) granted = read()
        }
        owner.lifecycle.addObserver(observer)
        onDispose { owner.lifecycle.removeObserver(observer) }
    }
    return granted
}

/**
 * The system screen that grants it.
 *
 * The app-specific action deep-links straight to this app's toggle; some OEM
 * builds do not implement it, so the general list is the fallback. Without that
 * fallback the button is a no-op on those devices, which reads as broken.
 */
private fun allFilesAccessIntent(context: android.content.Context): Intent =
    Intent(
        Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
        Uri.parse("package:${context.packageName}"),
    ).takeIf { it.resolveActivity(context.packageManager) != null }
        ?: Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)

internal fun formatBytes(bytes: Long): String = when {
    bytes >= 1_073_741_824 -> "%.1f GB".format(bytes / 1_073_741_824.0)
    bytes >= 1_048_576 -> "%.0f MB".format(bytes / 1_048_576.0)
    bytes >= 1024 -> "%.0f KB".format(bytes / 1024.0)
    else -> "$bytes B"
}
