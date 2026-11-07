package com.aio.downloader.ui.screens

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
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
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.Block
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.OpenInBrowser
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
// androidx.lifecycle.compose, NOT androidx.compose.ui.platform: the ui.platform
// copy was deprecated when the lifecycle artifacts took ownership of it, and it
// is scheduled to go away.
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aio.downloader.core.DownloadForm
import com.aio.downloader.core.LANGUAGES
import com.aio.downloader.ui.AioViewModel
import com.aio.downloader.ui.components.AioButton
import com.aio.downloader.ui.components.AioSelect
import com.aio.downloader.ui.components.AioTextField
import com.aio.downloader.ui.components.ButtonTone
import com.aio.downloader.ui.components.CalloutTone
import com.aio.downloader.ui.components.CheckRow
import com.aio.downloader.ui.components.ChoiceOption
import com.aio.downloader.ui.components.Collapsible
import com.aio.downloader.ui.components.FieldLabel
import com.aio.downloader.ui.components.Hairline
import com.aio.downloader.ui.components.HelpText
import com.aio.downloader.ui.components.HelpTextWithCode
import com.aio.downloader.ui.components.SectionHeader
import com.aio.downloader.ui.components.SegmentedChoice
import com.aio.downloader.ui.components.SliderRow
import com.aio.downloader.ui.components.StatusCallout
import com.aio.downloader.ui.components.SwitchRow
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.aio

/**
 * The new-download form — the desktop's New tab, cut down to a phone.
 *
 * ── WHAT IS SURFACED AND WHY ───────────────────────────────────────────────
 * aio-dl.py has 99 flags. Showing all of them would be faithful and useless.
 * Above the fold: URL, format, Komikku, chapter range, language, multi-source —
 * the six that change what you actually get. Everything else sits behind a
 * [Collapsible] grouped the way the desktop's Settings tab groups it, so a
 * reader who knows the desktop can find things where they expect.
 *
 * Deliberately ABSENT: `--modernize` (JXL/AVIF). It has no Android wheel
 * behind it — no pillow-jxl — so offering it would be offering a run that
 * cannot work.
 */
@Composable
fun DownloadScreen(
    form: DownloadForm,
    onFormChange: ((DownloadForm) -> DownloadForm) -> Unit,
    onStart: () -> AioViewModel.StartResult,
    onPersist: () -> Unit,
    onSeeQueue: () -> Unit,
    busy: Boolean,
    queued: Int,
) {
    // Persist on STOP rather than per keystroke: this is a SharedPreferences
    // write, and the URL field would otherwise commit a blob per character.
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP) onPersist()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    var notice by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(notice) {
        if (notice != null) {
            kotlinx.coroutines.delay(4000)
            notice = null
        }
    }

    Column(Modifier.fillMaxSize()) {
        Column(
            Modifier
                .weight(1f)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp),
        ) {
            UrlSection(form, onFormChange)
            FormatSection(form, onFormChange)
            ChapterSection(form, onFormChange)
            MultiSourceSection(form, onFormChange)
            AdvancedSection(form, onFormChange)
            // Breathing room so the last collapsible is not jammed against the
            // sticky footer when scrolled to the bottom.
            Spacer(Modifier.height(24.dp))
        }

        StartFooter(
            form = form,
            busy = busy,
            queued = queued,
            notice = notice,
            onSeeQueue = onSeeQueue,
            onStart = {
                val result = onStart()
                notice = when {
                    result.accepted == 0 && result.duplicates > 0 ->
                        "Already queued"
                    result.duplicates > 0 ->
                        "Queued ${result.accepted}, skipped ${result.duplicates} already queued"
                    result.accepted > 1 ->
                        "Queued ${result.accepted} downloads"
                    else -> null
                }
                if (result.accepted > 0) onSeeQueue()
            },
        )
    }
}

// ── URL + site callouts ────────────────────────────────────────────────────

private val TAPAS_RE = Regex("""\btapas\.io""", RegexOption.IGNORE_CASE)

/**
 * Sites that reach a browser THROUGH `sites/browser_backend.py`, which on
 * Android is the WebView bridge. They work — the note exists because the first
 * download on one pays a visible browser boot and may ask for a bot check, and
 * an unexplained verification window reads as something having gone wrong.
 *
 * Cross-file: browser/WebViewBridge.kt, sites/mangafire_vrf.py.
 */
private val BROWSER_BACKED_RE =
    Regex("""\b(mangafire\.to|violetscans)""", RegexOption.IGNORE_CASE)

/**
 * comix drives its own Patchright session directly (grep `_comix_worker_loop`
 * in sites/comix.py) rather than going through the backend seam, so the WebView
 * bridge does not reach it and it genuinely cannot run here. Saying so before
 * Start beats a Python traceback ten seconds in.
 */
private val UNSUPPORTED_RE = Regex("""\bcomix\.to""", RegexOption.IGNORE_CASE)

@Composable
private fun UrlSection(form: DownloadForm, onChange: ((DownloadForm) -> DownloadForm) -> Unit) {
    Spacer(Modifier.height(16.dp))
    FieldLabel("Series URL")
    Spacer(Modifier.height(3.dp))
    HelpText("One per line to queue several.")
    Spacer(Modifier.height(8.dp))
    AioTextField(
        value = form.url,
        onValueChange = { v -> onChange { it.copy(url = v) } },
        placeholder = "https://mangadex.org/title/…",
        mono = true,
        singleLine = false,
        minHeight = 76,
        keyboardType = KeyboardType.Uri,
    )

    AnimatedVisibility(
        visible = TAPAS_RE.containsMatchIn(form.url),
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        Box(Modifier.padding(top = 10.dp)) {
            StatusCallout(
                title = if (form.multiSource) {
                    "Premium episodes will be filled in"
                } else {
                    "Tapas locks premium episodes"
                },
                body = if (form.multiSource) {
                    "Paid and wait-to-unlock episodes get pulled from the " +
                        "highest-rated alternative site, so the download stays gap-free."
                } else {
                    "Paid and wait-to-unlock episodes can't be fetched directly and " +
                        "would leave gaps. Multi-source pulls each from another site instead."
                },
                icon = if (form.multiSource) Icons.Filled.AutoAwesome else Icons.Filled.Lock,
                tone = if (form.multiSource) CalloutTone.Success else CalloutTone.Warning,
                tag = "tapas.io",
                action = if (form.multiSource) {
                    null
                } else {
                    {
                        AioButton(
                            text = "Enable multi-source",
                            icon = Icons.Filled.AutoAwesome,
                            compact = true,
                            // Enabling force-resets the nested lazy toggle ON —
                            // an opt-out inside an opt-in, mirroring the desktop.
                            onClick = {
                                onChange { it.copy(multiSource = true, multiSourceLazy = true) }
                            },
                        )
                    }
                },
            )
        }
    }

    AnimatedVisibility(
        visible = BROWSER_BACKED_RE.containsMatchIn(form.url),
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        Box(Modifier.padding(top = 10.dp)) {
            StatusCallout(
                title = "This site opens a browser first",
                body = "MangaFire signs every request with its own page script, so the first " +
                    "chapter takes a few extra seconds and the site may ask you to confirm " +
                    "you're human. After that it's cached and the download runs at full speed.",
                icon = Icons.Filled.OpenInBrowser,
                tone = CalloutTone.Info,
            )
        }
    }

    AnimatedVisibility(
        visible = UNSUPPORTED_RE.containsMatchIn(form.url),
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        Box(Modifier.padding(top = 10.dp)) {
            StatusCallout(
                title = "comix doesn't work on Android",
                body = "It renders every page in a desktop browser it drives itself, which " +
                    "this app has no way to reach. Try another source for this series — " +
                    "multi-source can find one for you.",
                icon = Icons.Filled.Block,
                tone = CalloutTone.Warning,
            )
        }
    }
}

// ── format ─────────────────────────────────────────────────────────────────

private val FORMATS = listOf(
    ChoiceOption("cbz", "CBZ", "Readers"),
    ChoiceOption("epub", "EPUB", "E-reader"),
    ChoiceOption("pdf", "PDF", "Print"),
    ChoiceOption("none", "None", "Images"),
)

@Composable
private fun FormatSection(form: DownloadForm, onChange: ((DownloadForm) -> DownloadForm) -> Unit) {
    SectionHeader("Output")
    SegmentedChoice(
        options = FORMATS,
        selected = form.format,
        onSelect = { value ->
            onChange {
                it.copy(
                    format = value,
                    // "Images" has to mean images: aio-dl.py treats --format none
                    // as "skip the final book build" and produces NOTHING unless
                    // --keep-images or --keep-chapters is also set. Auto-enabling
                    // here is what makes the tile's label truthful.
                    keepImages = if (value == "none") true else it.keepImages,
                    // Recompression needs an archive to write into; modernize is
                    // CBZ-only. Clear anything the new format invalidates so an
                    // unlaunchable combination can't be assembled.
                    webtoonRecompress = if ((value == "pdf" || value == "none") && !it.komikku) {
                        false
                    } else {
                        it.webtoonRecompress
                    },
                )
            }
        },
    )

    AnimatedVisibility(
        visible = form.format == "none" && !form.keepImages && !form.keepChapters,
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        Box(Modifier.padding(top = 8.dp)) {
            HelpText(
                "With neither \"Keep chapters\" nor \"Keep images\" on, this produces " +
                    "only metadata — no pages land on disk.",
                tone = MaterialTheme.aio.warning,
            )
        }
    }

    Spacer(Modifier.height(6.dp))
    SwitchRow(
        label = "Komikku-compatible output",
        checked = form.komikku,
        onCheckedChange = { v -> onChange { it.copy(komikku = v) } },
        hint = if (form.komikku) {
            "Forces CBZ. One archive per chapter with its own ComicInfo.xml, plus " +
                "cover.jpg and details.json at the series root."
        } else {
            "For Mihon / Tachiyomi / Komikku. Each chapter becomes its own CBZ with " +
                "per-chapter metadata. Forces format to CBZ."
        },
    )

    AnimatedVisibility(
        visible = form.format == "epub" && !form.komikku,
        enter = AioMotion.revealEnter,
        exit = AioMotion.revealExit,
    ) {
        Column {
            Spacer(Modifier.height(8.dp))
            FieldLabel("EPUB layout")
            Spacer(Modifier.height(6.dp))
            SegmentedChoice(
                options = listOf(
                    ChoiceOption("vertical", "Vertical", "Webtoon"),
                    ChoiceOption("page", "Page", "Manga"),
                ),
                selected = form.epubLayout,
                onSelect = { v -> onChange { it.copy(epubLayout = v) } },
            )
        }
    }
}

// ── chapters ───────────────────────────────────────────────────────────────

@Composable
private fun ChapterSection(form: DownloadForm, onChange: ((DownloadForm) -> DownloadForm) -> Unit) {
    SectionHeader("Chapters")
    FieldLabel("Range")
    Spacer(Modifier.height(6.dp))
    AioTextField(
        value = form.chapters,
        onValueChange = { v -> onChange { it.copy(chapters = v) } },
        placeholder = "all",
        mono = true,
    )
    Spacer(Modifier.height(4.dp))
    HelpTextWithCode(
        "", "all", ", a single ", "75", ", ranges ", "1-50", ", or ", "-3",
        " for the newest three.",
    )

    Spacer(Modifier.height(12.dp))
    FieldLabel("Language")
    Spacer(Modifier.height(6.dp))
    AioSelect(
        options = LANGUAGES,
        selected = form.language,
        onSelect = { v -> onChange { it.copy(language = v) } },
    )
}

// ── multi-source ───────────────────────────────────────────────────────────

@Composable
private fun MultiSourceSection(
    form: DownloadForm,
    onChange: ((DownloadForm) -> DownloadForm) -> Unit,
) {
    SectionHeader("Multi-source fallback")
    SwitchRow(
        label = "Fill failed chapters from another site",
        checked = form.multiSource,
        onCheckedChange = { v ->
            onChange { it.copy(multiSource = v, multiSourceLazy = if (v) true else it.multiSourceLazy) }
        },
        hint = "When the primary CDN throttles or 404s a page, the chapter falls over " +
            "to the next-highest-rated source automatically.",
    )

    AnimatedVisibility(form.multiSource, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
        Column(Modifier.padding(start = 8.dp, top = 4.dp)) {
            SwitchRow(
                label = "Only search after a chapter fails",
                checked = form.multiSourceLazy,
                onCheckedChange = { v -> onChange { it.copy(multiSourceLazy = v) } },
                hint = "On: start downloading immediately, and pay the 30-80s cross-site " +
                    "search only if something actually needs a fallback. Off: discover up " +
                    "front — slower to start, better split-collapse and ghost detection.",
            )
            Spacer(Modifier.height(4.dp))
            SliderRow(
                label = "Alternative quality floor",
                value = form.multiSourceQualityMin,
                onValueChange = { v ->
                    // Snap to the desktop's 0.05 step. Slider `steps` would do
                    // this too, but it also draws tick marks the desktop has not
                    // got, so the rounding happens here instead.
                    onChange { it.copy(multiSourceQualityMin = kotlin.math.round(v * 20f) / 20f) }
                },
                valueRange = 0.30f..0.95f,
                display = "%.2f".format(form.multiSourceQualityMin),
                hint = "Sources rated below this aren't used as fallbacks. 0.65 keeps " +
                    "unknown-language Madara mirrors out.",
            )
        }
    }
}

// ── advanced ───────────────────────────────────────────────────────────────

@Composable
private fun AdvancedSection(form: DownloadForm, onChange: ((DownloadForm) -> DownloadForm) -> Unit) {
    SectionHeader("Advanced")
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {

        // Battery & data (the resource limits) LEFT this screen in M7. They are
        // a device policy that a search and an update check obey too, so they
        // live in Settings with the rest of AppSettings — a per-download form
        // was the wrong owner for a value three other code paths read.

        Collapsible(title = "Image quality") {
            SliderRow(
                label = "Quality",
                value = form.quality.toFloat(),
                onValueChange = { v -> onChange { it.copy(quality = v.toInt()) } },
                valueRange = 1f..100f,
                display = form.quality.toString(),
                hint = if (form.quality >= 100) {
                    "100 keeps the original bytes: CBZ pages are copied through untouched."
                } else {
                    "Below 100 every page is decoded and re-encoded, which loses the " +
                        "byte-preserving fast path."
                },
            )
            SliderRow(
                label = "Scaling",
                value = form.scaling.toFloat(),
                onValueChange = { v -> onChange { it.copy(scaling = v.toInt()) } },
                valueRange = 1f..100f,
                display = "${form.scaling}%",
            )
            Spacer(Modifier.height(8.dp))
            FieldLabel("Width (px)")
            Spacer(Modifier.height(6.dp))
            AioTextField(
                value = form.width,
                onValueChange = { v -> onChange { it.copy(width = v.filter(Char::isDigit)) } },
                placeholder = "auto",
                mono = true,
                keyboardType = KeyboardType.Number,
            )
        }

        Collapsible(title = "Output options") {
            CheckRow(
                label = "Skip partial chapters (1.5, 60.1, …)",
                checked = form.noPartials,
                onCheckedChange = { v -> onChange { it.copy(noPartials = v) } },
            )
            CheckRow(
                label = "Keep individual chapter files",
                checked = form.keepChapters,
                onCheckedChange = { v ->
                    onChange { it.copy(keepChapters = v, noFinalFile = if (v) it.noFinalFile else false) }
                },
            )
            CheckRow(
                label = "Skip the combined final file",
                checked = form.noFinalFile,
                enabled = form.keepChapters,
                onCheckedChange = { v -> onChange { it.copy(noFinalFile = v) } },
            )
            CheckRow(
                label = "Keep original unprocessed images",
                checked = form.keepImages,
                onCheckedChange = { v -> onChange { it.copy(keepImages = v) } },
            )
            CheckRow(
                label = "Skip all image processing",
                checked = form.noProcessing,
                onCheckedChange = { v -> onChange { it.copy(noProcessing = v) } },
            )
        }

        Collapsible(title = "Scanlation groups") {
            FieldLabel("Preferred groups")
            Spacer(Modifier.height(6.dp))
            AioTextField(
                value = form.group,
                onValueChange = { v -> onChange { it.copy(group = v) } },
                placeholder = "Official, GroupA",
            )
            Spacer(Modifier.height(4.dp))
            HelpTextWithCode(
                "Comma-separated, in priority order. Naming a group forces it and " +
                    "overrides every automatic signal — which is also the escape hatch " +
                    "when a real group is mistaken for machine translation. Use ",
                "Official",
                " to match any licensed release.",
            )

            Spacer(Modifier.height(12.dp))
            FieldLabel("Groups to avoid")
            Spacer(Modifier.height(6.dp))
            AioTextField(
                value = form.excludeGroup,
                onValueChange = { v -> onChange { it.copy(excludeGroup = v) } },
                placeholder = "SomeGroup",
            )

            Spacer(Modifier.height(12.dp))
            FieldLabel("Machine translations")
            Spacer(Modifier.height(6.dp))
            AioSelect(
                options = listOf(
                    "avoid" to "Avoid — only if it's the only version",
                    "allow" to "Allow — rank like any other",
                    "exclude" to "Exclude — skip the chapter",
                ),
                selected = form.mtl,
                onSelect = { v -> onChange { it.copy(mtl = v) } },
            )
            Spacer(Modifier.height(4.dp))
            HelpText(
                "Detected from the group's name and self-description. \"Avoid\" never " +
                    "costs you a chapter; \"Exclude\" leaves gaps where only MTL exists.",
            )
        }

        Collapsible(title = "LINE Webtoon recompression") {
            SwitchRow(
                label = "Recompress PNG pages to WebP",
                checked = form.webtoonRecompress && form.recompressAllowed,
                enabled = form.recompressAllowed,
                onCheckedChange = { v -> onChange { it.copy(webtoonRecompress = v) } },
                hint = "webtoons.com only, and a no-op everywhere else. Lossless PNG pages " +
                    "become lossy WebP — around 90% smaller with no visible loss on a " +
                    "phone screen. JPEG-served chapters are skipped automatically.",
                warning = if (form.recompressAllowed) {
                    null
                } else {
                    "Unavailable with ${form.format.uppercase()} output — there's no archive " +
                        "to write into. Switch to CBZ or EPUB, or turn on Komikku."
                },
            )
            AnimatedVisibility(
                visible = form.webtoonRecompress && form.recompressAllowed,
                enter = AioMotion.revealEnter,
                exit = AioMotion.revealExit,
            ) {
                SliderRow(
                    label = "WebP quality",
                    value = form.webtoonRecompressQuality.toFloat(),
                    onValueChange = { v ->
                        onChange { it.copy(webtoonRecompressQuality = v.toInt()) }
                    },
                    valueRange = 1f..100f,
                    display = form.webtoonRecompressQuality.toString(),
                    hint = "85 is storage-optimized. 90 is archival-safe and about 60% larger. " +
                        "95+ is wasted bytes on color webtoons.",
                )
            }
        }

        Collapsible(title = "Network tuning") {
            NumberField(
                label = "Image workers",
                value = form.imageWorkers,
                onValueChange = { v -> onChange { it.copy(imageWorkers = v) } },
                hint = "Threads per chapter for page downloads.",
            )
            NumberField(
                label = "HTTP timeout (sec)",
                value = form.httpTimeout,
                onValueChange = { v -> onChange { it.copy(httpTimeout = v) } },
            )
            NumberField(
                label = "Max retries",
                value = form.httpMaxRetries,
                onValueChange = { v -> onChange { it.copy(httpMaxRetries = v) } },
            )
            Spacer(Modifier.height(4.dp))
            CheckRow(
                label = "Disable the end-of-run missed-chapter retry",
                checked = form.noRetryMissedChapters,
                onCheckedChange = { v -> onChange { it.copy(noRetryMissedChapters = v) } },
            )
            NumberField(
                label = "Retry attempts",
                value = form.missedRetries,
                enabled = !form.noRetryMissedChapters,
                onValueChange = { v -> onChange { it.copy(missedRetries = v) } },
            )
        }

        Collapsible(title = "Cookies") {
            AioTextField(
                value = form.cookies,
                onValueChange = { v -> onChange { it.copy(cookies = v) } },
                placeholder = "key1=value1;key2=value2",
                mono = true,
            )
            Spacer(Modifier.height(4.dp))
            HelpText("For sites that gate chapters behind a login.")
        }
    }
}

/**
 * An integer field that tolerates being empty mid-edit.
 *
 * The desktop learned this the hard way (its `setNum` clamp): clearing the box
 * to retype a number must not momentarily forward 0 to the CLI, where
 * `--http-timeout 0` is a real and very broken setting.
 */
@Composable
private fun NumberField(
    label: String,
    value: Int,
    onValueChange: (Int) -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    hint: String? = null,
) {
    var text by remember(value) { mutableStateOf(value.toString()) }
    Column(modifier.padding(top = 8.dp)) {
        FieldLabel(label, enabled = enabled)
        Spacer(Modifier.height(6.dp))
        AioTextField(
            value = text,
            enabled = enabled,
            mono = true,
            keyboardType = KeyboardType.Number,
            onValueChange = { raw ->
                text = raw.filter(Char::isDigit)
                text.toIntOrNull()?.let(onValueChange)
            },
        )
        hint?.let {
            Spacer(Modifier.height(4.dp))
            HelpText(it)
        }
    }
}

// ── footer ─────────────────────────────────────────────────────────────────

/**
 * The Start button, pinned. Also the only place the screen reports back, since
 * a queued download navigates away and a rejected duplicate does not — so
 * without a word here the button would look broken in exactly the case where it
 * did the most thinking.
 */
@Composable
private fun StartFooter(
    form: DownloadForm,
    busy: Boolean,
    queued: Int,
    notice: String?,
    onSeeQueue: () -> Unit,
    onStart: () -> Unit,
) {
    val count = form.urls().size
    Column {
        Hairline()
        Column(
            Modifier
                .fillMaxWidth()
                .background(MaterialTheme.colorScheme.background)
                .padding(horizontal = 20.dp, vertical = 12.dp),
        ) {
            AnimatedVisibility(notice != null, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
                Column {
                    HelpText(notice.orEmpty(), tone = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.height(8.dp))
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                AioButton(
                    text = when {
                        count > 1 -> "Queue $count downloads"
                        busy -> "Add to queue"
                        else -> "Start download"
                    },
                    icon = Icons.Filled.Download,
                    enabled = count > 0,
                    onClick = onStart,
                    modifier = Modifier.weight(1f),
                )
                AnimatedVisibility(busy || queued > 0, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
                    Row {
                        Spacer(Modifier.width(8.dp))
                        AioButton(
                            text = if (busy) "Running" else "$queued queued",
                            tone = ButtonTone.Outline,
                            onClick = onSeeQueue,
                        )
                    }
                }
            }
            AnimatedVisibility(busy && count > 0, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
                Column {
                    Spacer(Modifier.height(6.dp))
                    HelpText(
                        "A download is already running. Downloads go one at a time — " +
                            "this one starts when that finishes.",
                    )
                }
            }
        }
    }
}
