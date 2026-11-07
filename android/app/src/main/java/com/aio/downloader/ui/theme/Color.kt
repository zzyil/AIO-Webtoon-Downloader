package com.aio.downloader.ui.theme

import androidx.compose.ui.graphics.Color

/**
 * The desktop design system's color tokens, converted to Compose Colors.
 *
 * SOURCE OF TRUTH IS `UI-source/src/styles/globals.css` — every value here is a
 * direct HSL->RGB conversion of one `--token` on `:root` (light) or `.dark`.
 * The comment on each line carries the original HSL triple so a change on the
 * desktop side can be re-converted without guessing which token it was. If you
 * edit globals.css, edit this file; there is no build-time link between them.
 *
 * WHY CONVERTED, NOT RE-PICKED: the two apps are meant to read as one product.
 * Colors chosen by eye against a phone screen drift from the desktop within a
 * release or two, and the drift is invisible until you put the two side by side.
 *
 * Read by [AioTheme] only. Screens and components must go through
 * `MaterialTheme.colorScheme` / `LocalAioStatus`, never through these raw
 * values — that indirection is what makes light/dark work.
 */

// ── Light ──────────────────────────────────────────────────────────────────
internal object LightTokens {
    val background = Color(0xFFFAFAFA)        // 0 0% 98%
    val foreground = Color(0xFF1C1F26)        // 220 15% 13%
    val card = Color(0xFFFFFFFF)              // 0 0% 100%
    val cardForeground = Color(0xFF1C1F26)    // 220 15% 13%
    val primary = Color(0xFF2060DF)           // 220 75% 50%
    val primaryForeground = Color(0xFFFFFFFF) // 0 0% 100%
    val secondary = Color(0xFFEBEDEF)         // 220 10% 93%
    val secondaryForeground = Color(0xFF2B303B) // 220 15% 20%
    val muted = Color(0xFFF1F2F4)             // 220 10% 95%
    val mutedForeground = Color(0xFF6C727F)   // 220 8% 46%
    val accent = Color(0xFFE3E5E8)            // 220 10% 90%
    val accentForeground = Color(0xFF1C1F26)  // 220 15% 13%
    val destructive = Color(0xFFDC2828)       // 0 72% 51%
    val destructiveForeground = Color(0xFFFFFFFF)
    val border = Color(0xFFDDDFE3)            // 220 10% 88%
    val ring = Color(0xFF2060DF)              // 220 75% 50%

    val success = Color(0xFF29A356)           // 142 60% 40%
    val warning = Color(0xFFF59F0A)           // 38 92% 50%
    val info = Color(0xFF1AA1E6)              // 200 80% 50%
}

// ── Dark ───────────────────────────────────────────────────────────────────
internal object DarkTokens {
    val background = Color(0xFF16181D)        // 224 15% 10%
    val foreground = Color(0xFFE2E6E9)        // 210 15% 90%
    val card = Color(0xFF1C1F26)              // 224 15% 13%
    val cardForeground = Color(0xFFE2E6E9)    // 210 15% 90%
    val primary = Color(0xFF4481E4)           // 217 75% 58%
    val primaryForeground = Color(0xFFFFFFFF) // 0 0% 100%
    val secondary = Color(0xFF282B33)         // 224 12% 18%
    val secondaryForeground = Color(0xFFD3D9DE) // 210 15% 85%
    val muted = Color(0xFF24272E)             // 224 12% 16%
    val mutedForeground = Color(0xFF838995)   // 220 8% 55%
    val accent = Color(0xFF2D3039)            // 224 12% 20%
    val accentForeground = Color(0xFFE2E6E9)  // 210 15% 90%
    val destructive = Color(0xFFD34545)       // 0 62% 55%
    val destructiveForeground = Color(0xFFFFFFFF)
    val border = Color(0xFF2D3039)            // 224 12% 20%
    val ring = Color(0xFF4481E4)              // 217 75% 58%

    val success = Color(0xFF37BE69)           // 142 55% 48%
    val warning = Color(0xFFF6A823)           // 38 92% 55%
    val info = Color(0xFF36A9E2)              // 200 75% 55%
}

/**
 * Flatten `fg` at `alpha` over `bg`.
 *
 * The desktop leans hard on translucent fills — `bg-primary/10` for an active
 * tab, `bg-success/15` for a resolved callout — because CSS composites them
 * against whatever is behind. Compose can do the same with `Color.copy(alpha)`,
 * and for decorative fills it does. But Material3's [androidx.compose.material3.ColorScheme]
 * roles are expected to be OPAQUE: a translucent `secondaryContainer` lets the
 * scrim and the elevation overlay bleed through and the component reads muddy.
 * So container roles get pre-composited here instead.
 */
internal fun Color.over(bg: Color, alpha: Float): Color = Color(
    red = red * alpha + bg.red * (1 - alpha),
    green = green * alpha + bg.green * (1 - alpha),
    blue = blue * alpha + bg.blue * (1 - alpha),
    alpha = 1f,
)
