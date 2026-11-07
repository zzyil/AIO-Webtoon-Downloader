package com.aio.downloader.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.LineHeightStyle
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp

/**
 * The type scale, ported from the desktop's Tailwind ramp.
 *
 * WHAT MAKES THIS LOOK LIKE THE DESKTOP APP IS THE SCALE, NOT THE FACE. The
 * desktop is deliberately dense — a 12px control label over a 10px muted hint,
 * with 14px reserved for things you actually read. Material 3's defaults are
 * built for a very different rhythm (14sp is its SMALLEST body size), so
 * adopting them wholesale makes every screen feel like a different product even
 * with identical colors. Every size below is the px value of the Tailwind class
 * it replaces, named in the comment.
 *
 * ── On the typefaces ───────────────────────────────────────────────────────
 * The desktop declares `"DM Sans", system-ui, sans-serif` and
 * `"JetBrains Mono", "Cascadia Code", monospace` and pulls both from a CDN.
 * An APK has no CDN, and the two ways to get there both cost more than they
 * return right now:
 *   - Downloadable Google Fonts (`ui-text-google-fonts`) needs Play Services on
 *     the device plus a certificate resource array; when either is missing it
 *     silently falls back to exactly what is set below, after a load flicker.
 *   - Bundling the TTFs means vendoring ~800 KB of binaries plus their OFL
 *     license into a repo that ships upstream.
 * So this resolves to the platform faces, and the two constants below are the
 * ONLY place that decision lives: drop the TTFs into `res/font/` and point
 * [AioSans] / [AioMono] at a `FontFamily(Font(R.font.…))` and the whole app
 * follows. Nothing else in the codebase names a font.
 */
private val AioSans = FontFamily.SansSerif
private val AioMono = FontFamily.Monospace

/**
 * Trim the extra leading Compose adds above the first line and below the last.
 * Without this, a 10sp hint under a 12sp label sits with visibly more air than
 * the desktop's `leading-snug`, and the dense rhythm above falls apart.
 */
private val TrimEdges = LineHeightStyle(
    alignment = LineHeightStyle.Alignment.Center,
    trim = LineHeightStyle.Trim.Both,
)

private fun aio(
    size: Int,
    weight: FontWeight = FontWeight.Normal,
    lineHeight: Double = size * 1.45,
    tracking: Double = 0.0,
    family: FontFamily = AioSans,
) = TextStyle(
    fontFamily = family,
    fontWeight = weight,
    fontSize = size.sp,
    lineHeight = lineHeight.sp,
    letterSpacing = tracking.em,
    lineHeightStyle = TrimEdges,
)

val AioTypography = Typography(
    // Screen title in the top bar. Desktop: `text-sm font-semibold tracking-wide`.
    titleMedium = aio(14, FontWeight.SemiBold, tracking = 0.02),
    // Card headline — a series name in the queue, a group name in a result.
    titleSmall = aio(13, FontWeight.SemiBold),
    // Big enough to be the one thing you read on a screen. Used sparingly.
    titleLarge = aio(17, FontWeight.SemiBold, tracking = (-0.01)),

    // `text-sm` — input text, primary body copy.
    bodyLarge = aio(14),
    // The workhorse for descriptive paragraphs that are not hints.
    bodyMedium = aio(13),
    // `text-xs` — dense body, list rows, badge text.
    bodySmall = aio(12),

    // Button text. Desktop: `text-sm font-medium`.
    labelLarge = aio(14, FontWeight.Medium),
    // `text-xs font-medium` — the Label primitive above every control.
    labelMedium = aio(12, FontWeight.Medium),
    // `text-[10px] text-muted-foreground leading-snug` — the quiet hint voice
    // under a control. There is one of these under almost every switch on the
    // desktop, and dropping them is how a port stops explaining itself.
    labelSmall = aio(10, lineHeight = 13.5),
)

/**
 * Styles Material 3 has no slot for. Plain top-level vals rather than another
 * CompositionLocal: none of them vary by theme (color is applied by the
 * caller), so a local would be indirection with no payoff.
 */
object AioText {
    /**
     * `text-xs font-semibold uppercase tracking-wider text-muted-foreground` —
     * the label half of SectionHeader. The wide tracking is doing real work:
     * it is what separates a section rule from a bolded sentence at a glance.
     */
    val sectionHeader = aio(11, FontWeight.SemiBold, tracking = 0.09)

    /** `.log-text` — 12px/1.5 monospace. The log panel and nothing else. */
    val log = aio(12, lineHeight = 18.0, family = AioMono)

    /**
     * `font-mono tabular-nums` — counters, ETAs, byte sizes, quality floors.
     * Monospace ISN'T decorative here: these values change while you are
     * looking at them, and a proportional face reflows the row on every tick.
     */
    val numeric = aio(12, FontWeight.Medium, family = AioMono)

    /** The 10px sibling of [numeric], for the timing row under a progress bar. */
    val numericSmall = aio(10, family = AioMono)
}
