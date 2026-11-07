package com.aio.downloader.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CheckboxDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import com.aio.downloader.ui.theme.AioMotion
import com.aio.downloader.ui.theme.AioText
import com.aio.downloader.ui.theme.aio

/**
 * The shared control vocabulary, ported from
 * `UI-source/src/components/ui/primitives.jsx`.
 *
 * WHAT THIS FILE IS FOR: the desktop app gets its consistency from every screen
 * building out of the same dozen pieces, so a new setting looks like an old one
 * for free. Reproducing that here means screens compose these rather than
 * reaching for raw Material components — a bare `Switch` in a `Row` will be
 * subtly wrong (wrong label size, no hint slot, wrong touch target) in a way
 * nobody notices until three screens have drifted apart.
 *
 * TWO PLACES THIS DELIBERATELY DIVERGES FROM THE DESKTOP:
 *  - Touch targets. The desktop's controls are 16-36px tall because a mouse
 *    drives them. Everything tappable here is >=44dp regardless of how small
 *    its text is. The TYPE stays dense; the hit area does not.
 *  - Text fields are hand-built on BasicTextField instead of Material's
 *    OutlinedTextField, which carries a 56dp minimum height and a floating
 *    label — neither matches the desktop's flat `h-9` input, and at 56dp a row
 *    this form has fifteen of does not fit on a phone.
 */

// ── Section header ─────────────────────────────────────────────────────────

/**
 * An uppercase tracked label followed by a hairline rule running to the edge.
 * The most recognizable piece of the desktop's form layout, and what turns one
 * long scroll into readable groups.
 */
@Composable
fun SectionHeader(text: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(top = 20.dp, bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text.uppercase(),
            style = AioText.sectionHeader,
            color = MaterialTheme.aio.mutedForeground,
        )
        Spacer(Modifier.width(12.dp))
        Box(
            Modifier
                .weight(1f)
                .height(1.dp)
                .background(MaterialTheme.aio.border),
        )
    }
}

// ── Card ───────────────────────────────────────────────────────────────────

/** `rounded-lg border bg-card shadow-sm` — the desktop's only container idiom. */
@Composable
fun AioCard(
    modifier: Modifier = Modifier,
    borderColor: Color? = null,
    /**
     * The desktop marks a not-yet-started queue item `border-dashed`. Compose
     * has no dashed-border modifier short of a PathEffect stroke, so the "not
     * real yet" signal is carried by a dimmer hairline — same reading, and it
     * survives at phone density where a 2px dash pattern would not.
     */
    provisional: Boolean = false,
    contentPadding: PaddingValues = PaddingValues(14.dp),
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.medium)
            .background(MaterialTheme.aio.card)
            .border(
                width = 1.dp,
                color = borderColor
                    ?: MaterialTheme.aio.border.copy(alpha = if (provisional) 0.55f else 1f),
                shape = MaterialTheme.shapes.medium,
            )
            .padding(contentPadding),
        content = content,
    )
}

// ── Badge / pill ───────────────────────────────────────────────────────────

enum class PillTone { Primary, Neutral, Success, Warning, Danger, Info }

/** `Badge` — a `rounded-full border px-2.5 py-0.5 text-xs font-medium` pill. */
@Composable
fun Pill(
    text: String,
    modifier: Modifier = Modifier,
    tone: PillTone = PillTone.Neutral,
    mono: Boolean = false,
) {
    val status = MaterialTheme.aio
    val fg = when (tone) {
        PillTone.Primary -> MaterialTheme.colorScheme.primary
        PillTone.Neutral -> status.mutedForeground
        PillTone.Success -> status.success
        PillTone.Warning -> status.warning
        PillTone.Danger -> MaterialTheme.colorScheme.error
        PillTone.Info -> status.info
    }
    Text(
        text = text,
        style = if (mono) AioText.numeric else MaterialTheme.typography.labelSmall,
        color = fg,
        maxLines = 1,
        modifier = modifier
            .clip(CircleShape)
            .background(fg.copy(alpha = 0.10f))
            .border(1.dp, fg.copy(alpha = 0.22f), CircleShape)
            .padding(horizontal = 8.dp, vertical = 3.dp),
    )
}

// ── Labels and hints ───────────────────────────────────────────────────────

/** `<Label>` — `text-xs font-medium` above a control. */
@Composable
fun FieldLabel(text: String, modifier: Modifier = Modifier, enabled: Boolean = true) {
    Text(
        text = text,
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onBackground.copy(alpha = if (enabled) 1f else 0.4f),
        modifier = modifier,
    )
}

/**
 * The quiet 10px explanation under a control. The desktop has one of these
 * almost everywhere, and they are the reason its dense forms are usable at all
 * — keep writing them.
 */
@Composable
fun HelpText(text: String, modifier: Modifier = Modifier, tone: Color? = null) {
    Text(
        text = text,
        style = MaterialTheme.typography.labelSmall,
        color = tone ?: MaterialTheme.aio.mutedForeground,
        modifier = modifier,
    )
}

/**
 * [HelpText] with `code`-styled runs: segments alternate plain/mono starting
 * with plain. The desktop writes `<span className="font-mono">CBZ</span>` inline
 * through half its hint copy — flag and format names read as values rather than
 * prose, and losing that costs real clarity in text this small.
 */
@Composable
fun HelpTextWithCode(vararg segments: String, modifier: Modifier = Modifier) {
    val codeColor = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.85f)
    val codeFamily = AioText.numeric.fontFamily
    val text = buildAnnotatedString {
        segments.forEachIndexed { i, part ->
            if (i % 2 == 1) {
                withStyle(SpanStyle(fontFamily = codeFamily, color = codeColor)) { append(part) }
            } else {
                append(part)
            }
        }
    }
    Text(
        text = text,
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.aio.mutedForeground,
        modifier = modifier,
    )
}

// ── Switch + checkbox rows ─────────────────────────────────────────────────

/**
 * The desktop's dominant setting shape: a switch, a one-line label, and a hint
 * explaining what turning it on actually costs.
 *
 * The whole row is the hit target, not just the switch — a 32dp control at the
 * far right of a 400dp row is a miss waiting to happen on a phone.
 */
@Composable
fun SwitchRow(
    label: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    hint: String? = null,
    /** Shown under the hint in amber — the "unavailable with PDF" case. */
    warning: String? = null,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.small)
            .clickable(enabled = enabled) { onCheckedChange(!checked) }
            .padding(vertical = 8.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Column(
            Modifier
                .weight(1f)
                .padding(end = 12.dp, top = 2.dp),
        ) {
            FieldLabel(label, enabled = enabled)
            hint?.let {
                Spacer(Modifier.height(3.dp))
                HelpText(
                    it,
                    tone = if (enabled) null else MaterialTheme.aio.mutedForeground.copy(alpha = 0.5f),
                )
            }
            AnimatedVisibility(
                visible = warning != null,
                enter = AioMotion.revealEnter,
                exit = AioMotion.revealExit,
            ) {
                Column {
                    Spacer(Modifier.height(5.dp))
                    HelpText(warning.orEmpty(), tone = MaterialTheme.aio.warning)
                }
            }
        }
        Switch(
            checked = checked,
            onCheckedChange = onCheckedChange,
            enabled = enabled,
            colors = SwitchDefaults.colors(
                checkedTrackColor = MaterialTheme.colorScheme.primary,
                checkedThumbColor = Color.White,
                checkedBorderColor = Color.Transparent,
                uncheckedTrackColor = MaterialTheme.aio.secondary,
                uncheckedThumbColor = MaterialTheme.aio.mutedForeground,
                uncheckedBorderColor = MaterialTheme.aio.border,
            ),
        )
    }
}

/** Checkbox + label — the desktop's Output Options list. */
@Composable
fun CheckRow(
    label: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.small)
            .clickable(enabled = enabled) { onCheckedChange(!checked) }
            .heightIn(min = 44.dp)
            .padding(end = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(
            checked = checked,
            onCheckedChange = onCheckedChange,
            enabled = enabled,
            colors = CheckboxDefaults.colors(
                checkedColor = MaterialTheme.colorScheme.primary,
                uncheckedColor = MaterialTheme.aio.border,
                checkmarkColor = MaterialTheme.colorScheme.onPrimary,
            ),
        )
        Spacer(Modifier.width(4.dp))
        FieldLabel(label, enabled = enabled, modifier = Modifier.weight(1f))
    }
}

// ── Text field ─────────────────────────────────────────────────────────────

/** `<Input>` / `<Textarea>` — a flat bordered field that focuses to primary. */
@Composable
fun AioTextField(
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    placeholder: String = "",
    enabled: Boolean = true,
    mono: Boolean = false,
    singleLine: Boolean = true,
    minHeight: Int = 44,
    keyboardType: KeyboardType = KeyboardType.Text,
    /**
     * Drawn inside the field, before the text — the desktop's `pl-8` search
     * input with its absolutely-positioned magnifier. Inside rather than beside
     * because a glyph sitting outside the border reads as a separate button.
     */
    leading: (@Composable () -> Unit)? = null,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()
    val borderColor by animateColorAsState(
        targetValue = if (focused) MaterialTheme.colorScheme.primary else MaterialTheme.aio.border,
        animationSpec = tween(AioMotion.COLOR_MS),
        label = "fieldBorder",
    )

    val textStyle = (if (mono) AioText.numeric else MaterialTheme.typography.bodyLarge)
        .copy(color = MaterialTheme.colorScheme.onBackground)
    val placeholderColor = MaterialTheme.aio.mutedForeground.copy(alpha = 0.7f)

    BasicTextField(
        value = value,
        onValueChange = onValueChange,
        enabled = enabled,
        singleLine = singleLine,
        textStyle = textStyle,
        interactionSource = interaction,
        cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
        modifier = modifier
            .fillMaxWidth()
            .defaultMinSize(minHeight = minHeight.dp)
            .clip(MaterialTheme.shapes.small)
            .background(MaterialTheme.colorScheme.background)
            .border(1.dp, borderColor, MaterialTheme.shapes.small),
        decorationBox = { inner ->
            Row(
                Modifier
                    .fillMaxWidth()
                    .heightIn(min = minHeight.dp)
                    .padding(horizontal = 12.dp, vertical = 11.dp),
                verticalAlignment = if (singleLine) Alignment.CenterVertically else Alignment.Top,
            ) {
                leading?.let {
                    it()
                    Spacer(Modifier.width(8.dp))
                }
                Box(
                    Modifier.weight(1f),
                    contentAlignment = if (singleLine) Alignment.CenterStart else Alignment.TopStart,
                ) {
                    if (value.isEmpty()) {
                        Text(text = placeholder, style = textStyle, color = placeholderColor)
                    }
                    inner()
                }
            }
        },
    )
}

// ── Slider row ─────────────────────────────────────────────────────────────

/**
 * Label + live value pill + slider + hint. The pill is monospace and sits on the
 * label row exactly as on the desktop, so the number does not shift the layout
 * as it changes.
 */
@Composable
fun SliderRow(
    label: String,
    value: Float,
    onValueChange: (Float) -> Unit,
    valueRange: ClosedFloatingPointRange<Float>,
    display: String,
    modifier: Modifier = Modifier,
    steps: Int = 0,
    hint: String? = null,
    enabled: Boolean = true,
) {
    Column(
        modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
    ) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            FieldLabel(label, enabled = enabled)
            Pill(display, mono = true)
        }
        Slider(
            value = value,
            onValueChange = onValueChange,
            valueRange = valueRange,
            steps = steps,
            enabled = enabled,
            colors = SliderDefaults.colors(
                thumbColor = MaterialTheme.colorScheme.primary,
                activeTrackColor = MaterialTheme.colorScheme.primary,
                inactiveTrackColor = MaterialTheme.aio.secondary,
                activeTickColor = Color.Transparent,
                inactiveTickColor = Color.Transparent,
            ),
        )
        hint?.let { HelpText(it, modifier = Modifier.padding(top = 2.dp)) }
    }
}

// ── Segmented choice ───────────────────────────────────────────────────────

data class ChoiceOption(val value: String, val label: String, val hint: String? = null)

/**
 * The Output Format grid: bordered tiles where the active one takes a primary
 * tint plus a heavier ring. Reproduced closely — including the two-line
 * label/hint tile — because it is the desktop's one piece of genuinely
 * satisfying chrome and it is doing the screen's most important choice.
 */
@Composable
fun SegmentedChoice(
    options: List<ChoiceOption>,
    selected: String,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        options.forEach { option ->
            val active = option.value == selected
            val border by animateColorAsState(
                if (active) MaterialTheme.colorScheme.primary else MaterialTheme.aio.border,
                tween(AioMotion.COLOR_MS),
                label = "choiceBorder",
            )
            val fill by animateColorAsState(
                if (active) MaterialTheme.colorScheme.primary.copy(alpha = 0.07f) else Color.Transparent,
                tween(AioMotion.COLOR_MS),
                label = "choiceFill",
            )
            Column(
                modifier = Modifier
                    .weight(1f)
                    .clip(MaterialTheme.shapes.medium)
                    .background(fill)
                    .border(if (active) 1.5.dp else 1.dp, border, MaterialTheme.shapes.medium)
                    .clickable { onSelect(option.value) }
                    .heightIn(min = 58.dp)
                    .padding(vertical = 9.dp, horizontal = 4.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                Text(
                    option.label,
                    style = MaterialTheme.typography.titleSmall,
                    color = if (active) {
                        MaterialTheme.colorScheme.primary
                    } else {
                        MaterialTheme.colorScheme.onBackground
                    },
                )
                option.hint?.let {
                    Spacer(Modifier.height(2.dp))
                    Text(
                        it,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.aio.mutedForeground,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}

// ── Collapsible ────────────────────────────────────────────────────────────

/**
 * A bordered disclosure with a chevron that rotates.
 *
 * This is what keeps the Download screen honest: the CLI has 99 flags, the
 * primary controls are about eight of them, and everything else lives behind
 * one of these. Open state is `rememberSaveable` so rotating the tablet or
 * returning from the background does not collapse the section being worked in.
 */
@Composable
fun Collapsible(
    title: String,
    modifier: Modifier = Modifier,
    subtitle: String? = null,
    defaultOpen: Boolean = false,
    content: @Composable ColumnScope.() -> Unit,
) {
    var open by rememberSaveable(title) { mutableStateOf(defaultOpen) }
    val chevron by animateFloatAsState(
        targetValue = if (open) 180f else 0f,
        animationSpec = tween(AioMotion.SLIDE_UP_MS, easing = AioMotion.EaseOut),
        label = "chevron",
    )

    Column(
        modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.small)
            .border(1.dp, MaterialTheme.aio.border, MaterialTheme.shapes.small),
    ) {
        Row(
            Modifier
                .fillMaxWidth()
                .clickable { open = !open }
                .heightIn(min = 48.dp)
                .padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.titleSmall)
                subtitle?.let {
                    Spacer(Modifier.height(1.dp))
                    HelpText(it)
                }
            }
            Icon(
                Icons.Filled.KeyboardArrowDown,
                contentDescription = if (open) "Collapse" else "Expand",
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier
                    .size(20.dp)
                    .rotate(chevron),
            )
        }
        AnimatedVisibility(open, enter = AioMotion.revealEnter, exit = AioMotion.revealExit) {
            Column {
                Hairline()
                Column(Modifier.padding(horizontal = 12.dp, vertical = 10.dp), content = content)
            }
        }
    }
}

// ── Status callout ─────────────────────────────────────────────────────────

enum class CalloutTone { Warning, Info, Success, Danger }

/**
 * The desktop's `TapasPremiumCallout` / `ComixBrowserCallout` shape: a tinted
 * panel with a colored spine down its left edge and a ringed icon chip.
 *
 * Worth porting closely because it is the app's strongest visual signature and
 * because it does a job nothing else does — it explains a situation the user is
 * about to walk into, in place, at the moment it becomes relevant. The tone
 * crossfades over [AioMotion.COLOR_MS] so a callout that resolves (amber
 * "premium episodes will be skipped" -> green "will be filled in") reads as one
 * panel changing its mind rather than two panels swapping.
 */
@Composable
fun StatusCallout(
    title: String,
    body: String,
    icon: ImageVector,
    modifier: Modifier = Modifier,
    tone: CalloutTone = CalloutTone.Warning,
    tag: String? = null,
    action: (@Composable () -> Unit)? = null,
) {
    val status = MaterialTheme.aio
    val target = when (tone) {
        CalloutTone.Warning -> status.warning
        CalloutTone.Info -> status.info
        CalloutTone.Success -> status.success
        CalloutTone.Danger -> MaterialTheme.colorScheme.error
    }
    val accent by animateColorAsState(target, tween(AioMotion.COLOR_MS), label = "calloutAccent")

    Row(
        modifier
            .fillMaxWidth()
            .clip(MaterialTheme.shapes.medium)
            .background(accent.copy(alpha = 0.07f))
            .border(1.dp, accent.copy(alpha = 0.35f), MaterialTheme.shapes.medium)
            // IntrinsicSize.Min is load-bearing, not tidiness. The spine below
            // uses fillMaxHeight(), which is a NO-OP under the infinite height
            // constraint of a verticalScroll Column (where every caller used to
            // live) but EXPANDS TO FILL in an ordinary one. Without this the
            // callout silently swallows the whole screen and every sibling after
            // it is measured to zero — which is how browser/ChallengeActivity's
            // WebView ended up 0px tall, and a 0px WebView takes its window's
            // rendering down with it (black window, no error anywhere).
            // Pinning the row to its content's intrinsic height makes the
            // primitive behave identically in both kinds of parent.
            .height(IntrinsicSize.Min)
            .heightIn(min = 56.dp),
    ) {
        // The spine. Reads as a status band before any text is parsed.
        Box(
            Modifier
                .width(3.dp)
                .fillMaxHeight()
                .background(accent),
        )
        Row(Modifier.padding(start = 11.dp, end = 12.dp, top = 11.dp, bottom = 11.dp)) {
            Box(
                Modifier
                    .size(30.dp)
                    .clip(MaterialTheme.shapes.small)
                    .background(accent.copy(alpha = 0.15f))
                    .border(1.dp, accent.copy(alpha = 0.3f), MaterialTheme.shapes.small),
                contentAlignment = Alignment.Center,
            ) {
                Icon(icon, contentDescription = null, tint = accent, modifier = Modifier.size(16.dp))
            }
            Spacer(Modifier.width(11.dp))
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        title,
                        style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier.weight(1f, fill = false),
                    )
                    tag?.let {
                        Spacer(Modifier.width(8.dp))
                        Pill(it, mono = true)
                    }
                }
                Spacer(Modifier.height(4.dp))
                HelpText(body)
                action?.let {
                    Spacer(Modifier.height(10.dp))
                    it()
                }
            }
        }
    }
}

// ── Progress track ─────────────────────────────────────────────────────────

/**
 * `h-2 rounded-full bg-secondary` with a primary fill. A null [fraction] is
 * indeterminate.
 *
 * The fill eases over [AioMotion.PROGRESS_MS] — deliberately longer than
 * DownloadService's 700ms poll interval, so the bar is still moving when the
 * next batch of events lands and a chapter tick reads as motion, not a step.
 */
@Composable
fun ProgressTrack(fraction: Float?, modifier: Modifier = Modifier) {
    Box(
        modifier
            .fillMaxWidth()
            .height(6.dp)
            .clip(CircleShape)
            .background(MaterialTheme.aio.secondary),
    ) {
        if (fraction == null) {
            // Indeterminate: a pulsing third-width bar, as on the desktop —
            // rather than a sweeping one. A sweep implies progress that is being
            // measured, and at this point nothing is.
            val alpha by rememberInfiniteTransition(label = "pulse").animateFloat(
                initialValue = 0.4f,
                targetValue = 1f,
                animationSpec = infiniteRepeatable(
                    tween(1000, easing = AioMotion.EaseOut),
                    RepeatMode.Reverse,
                ),
                label = "pulseAlpha",
            )
            Box(
                Modifier
                    .fillMaxWidth(0.34f)
                    .fillMaxHeight()
                    .clip(CircleShape)
                    .background(MaterialTheme.colorScheme.primary.copy(alpha = alpha)),
            )
        } else {
            val width by animateFloatAsState(
                targetValue = fraction.coerceIn(0f, 1f),
                animationSpec = tween(AioMotion.PROGRESS_MS, easing = AioMotion.EaseOut),
                label = "progressFill",
            )
            Box(
                Modifier
                    // A zero-width Box would still paint its rounded cap, so a
                    // 0/40 chapter run would show a stub that looks like real
                    // progress. Floor it just above nothing instead.
                    .fillMaxWidth(width.coerceAtLeast(0.004f))
                    .fillMaxHeight()
                    .clip(CircleShape)
                    .background(MaterialTheme.colorScheme.primary),
            )
        }
    }
}

// ── Buttons ────────────────────────────────────────────────────────────────

enum class ButtonTone { Primary, Outline, Ghost, Danger }

/**
 * One button instead of Material's five. The desktop has `default` / `outline` /
 * `ghost` / `destructive` and uses `secondary` almost nowhere, so this covers it
 * — in a flatter, tighter shape than Material's default pill.
 */
@Composable
fun AioButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    tone: ButtonTone = ButtonTone.Primary,
    enabled: Boolean = true,
    icon: ImageVector? = null,
    compact: Boolean = false,
) {
    val scheme = MaterialTheme.colorScheme
    val (bg, fg, stroke) = when (tone) {
        ButtonTone.Primary -> Triple(scheme.primary, scheme.onPrimary, Color.Transparent)
        ButtonTone.Outline -> Triple(Color.Transparent, scheme.onBackground, MaterialTheme.aio.border)
        ButtonTone.Ghost -> Triple(Color.Transparent, MaterialTheme.aio.mutedForeground, Color.Transparent)
        ButtonTone.Danger -> Triple(scheme.error, scheme.onError, Color.Transparent)
    }
    val alpha = if (enabled) 1f else 0.45f

    Row(
        modifier = modifier
            .clip(MaterialTheme.shapes.small)
            .background(bg.copy(alpha = bg.alpha * alpha))
            .border(1.dp, stroke.copy(alpha = stroke.alpha * alpha), MaterialTheme.shapes.small)
            .clickable(enabled = enabled, onClick = onClick)
            .heightIn(min = if (compact) 36.dp else 46.dp)
            .padding(horizontal = if (compact) 12.dp else 18.dp),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        CompositionLocalProvider(LocalContentColor provides fg.copy(alpha = alpha)) {
            icon?.let {
                Icon(it, contentDescription = null, modifier = Modifier.size(if (compact) 15.dp else 17.dp))
                Spacer(Modifier.width(7.dp))
            }
            Text(
                text,
                style = if (compact) {
                    MaterialTheme.typography.labelMedium
                } else {
                    MaterialTheme.typography.labelLarge
                },
                color = fg.copy(alpha = alpha),
                maxLines = 1,
            )
        }
    }
}

// ── Select ─────────────────────────────────────────────────────────────────

/**
 * `<Select>` — a bordered field that opens a menu. Built on the stable
 * [DropdownMenu] rather than Material's `ExposedDropdownMenuBox`, which is
 * still experimental and drags in a text-field anchor this does not need.
 *
 * The desktop uses a native `<select>` here and carries a whole CSS workaround
 * for Chromium rendering its popup unreadably in dark mode. Compose draws its
 * own menu, so that entire class of bug simply does not exist on this side.
 */
@Composable
fun AioSelect(
    options: List<Pair<String, String>>,
    selected: String,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
) {
    var open by remember { mutableStateOf(false) }
    val label = options.firstOrNull { it.first == selected }?.second ?: selected

    Box(modifier) {
        Row(
            Modifier
                .fillMaxWidth()
                .clip(MaterialTheme.shapes.small)
                .background(MaterialTheme.colorScheme.background)
                .border(1.dp, MaterialTheme.aio.border, MaterialTheme.shapes.small)
                .clickable(enabled = enabled) { open = true }
                .heightIn(min = 44.dp)
                .padding(horizontal = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                label,
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onBackground.copy(alpha = if (enabled) 1f else 0.4f),
                modifier = Modifier.weight(1f),
                maxLines = 1,
            )
            Icon(
                Icons.Filled.KeyboardArrowDown,
                contentDescription = null,
                tint = MaterialTheme.aio.mutedForeground,
                modifier = Modifier.size(18.dp),
            )
        }
        DropdownMenu(
            expanded = open,
            onDismissRequest = { open = false },
            containerColor = MaterialTheme.aio.card,
        ) {
            options.forEach { (value, text) ->
                val active = value == selected
                DropdownMenuItem(
                    text = {
                        Text(
                            text,
                            style = MaterialTheme.typography.bodyLarge,
                            color = if (active) {
                                MaterialTheme.colorScheme.primary
                            } else {
                                MaterialTheme.colorScheme.onBackground
                            },
                        )
                    },
                    onClick = {
                        onSelect(value)
                        open = false
                    },
                )
            }
        }
    }
}

/** Hairline rule. `border-t` / `border-b` on the desktop. */
@Composable
fun Hairline(modifier: Modifier = Modifier) {
    Box(
        modifier
            .fillMaxWidth()
            .height(1.dp)
            .background(MaterialTheme.aio.border),
    )
}
