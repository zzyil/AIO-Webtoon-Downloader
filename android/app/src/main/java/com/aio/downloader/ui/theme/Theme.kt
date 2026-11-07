package com.aio.downloader.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.Modifier
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/**
 * The app theme. Wrap the whole content tree in [AioTheme] exactly once, from
 * MainActivity.
 *
 * ── NO DYNAMIC COLOR, ON PURPOSE ───────────────────────────────────────────
 * The milestone plan said "Material 3, dynamic color". Dynamic color derives
 * the palette from the user's wallpaper, which would replace the blue that
 * carries this product's identity — the active-tab tint, the progress bar, the
 * focus ring, every primary button — with whatever their home screen happens to
 * be. Since the brief is to conform to the desktop's design language, and that
 * language IS the blue-on-neutral palette in Color.kt, wallpaper theming is the
 * one Material 3 feature that actively fights it. Light/dark still follows the
 * system, which is the part users actually notice.
 */

/**
 * The status colors, plus the handful of desktop tokens Material 3 has no role
 * for. Material's `error` covers `destructive` and nothing else; `success` /
 * `warning` / `info` and the `border` / `muted` pair have to travel separately.
 *
 * Read via [LocalAioStatus] or the [MaterialTheme.aio] shorthand below.
 */
@Immutable
data class AioStatusColors(
    val success: Color,
    val warning: Color,
    val info: Color,
    /** `--border`: the hairline under a section header, around a card. */
    val border: Color,
    /** `--muted-foreground`: hint text, inactive icons, secondary counters. */
    val mutedForeground: Color,
    /** `--card`: raised surfaces. Distinct from `background` in BOTH themes. */
    val card: Color,
    /** `--secondary`: the unfilled track of a progress bar or slider. */
    val secondary: Color,
)

private val LocalAioStatus = staticCompositionLocalOf<AioStatusColors> {
    error("AioStatusColors accessed outside AioTheme")
}

/** `MaterialTheme.aio.warning` reads better at call sites than a local lookup. */
val MaterialTheme.aio: AioStatusColors
    @Composable @ReadOnlyComposable get() = LocalAioStatus.current

/**
 * Radius scale from `--radius: 0.5rem` and Tailwind's derived steps:
 * `rounded-sm` = radius-4, `rounded-md` = radius-2, `rounded-lg` = radius.
 * Material's own scale is much rounder (medium is 12dp), which is the single
 * fastest way to make a ported screen stop looking like the app it came from.
 */
private val AioShapes = Shapes(
    extraSmall = androidx.compose.foundation.shape.RoundedCornerShape(4.dp),
    small = androidx.compose.foundation.shape.RoundedCornerShape(6.dp),
    medium = androidx.compose.foundation.shape.RoundedCornerShape(8.dp),
    large = androidx.compose.foundation.shape.RoundedCornerShape(12.dp),
    extraLarge = androidx.compose.foundation.shape.RoundedCornerShape(16.dp),
)

@Composable
fun AioTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    // The container roles are pre-composited rather than translucent — see the
    // `over` helper in Color.kt for why. The ratios are the desktop's own:
    // an active tab is `bg-primary/10`, a callout chip `bg-warning/15`.
    val colorScheme = if (darkTheme) {
        darkColorScheme(
            primary = DarkTokens.primary,
            onPrimary = DarkTokens.primaryForeground,
            primaryContainer = DarkTokens.primary.over(DarkTokens.background, 0.16f),
            onPrimaryContainer = DarkTokens.primary,
            secondary = DarkTokens.secondaryForeground,
            onSecondary = DarkTokens.secondary,
            // NavigationBar's selected-item pill reads secondaryContainer /
            // onSecondaryContainer. Mapping them to the primary tint is what
            // reproduces the desktop rail's `bg-primary/10 text-primary`.
            secondaryContainer = DarkTokens.primary.over(DarkTokens.background, 0.16f),
            onSecondaryContainer = DarkTokens.primary,
            tertiary = DarkTokens.info,
            onTertiary = Color.White,
            background = DarkTokens.background,
            onBackground = DarkTokens.foreground,
            surface = DarkTokens.background,
            onSurface = DarkTokens.foreground,
            surfaceVariant = DarkTokens.muted,
            onSurfaceVariant = DarkTokens.mutedForeground,
            surfaceContainer = DarkTokens.card,
            surfaceContainerHigh = DarkTokens.accent,
            surfaceContainerHighest = DarkTokens.accent,
            surfaceContainerLow = DarkTokens.card,
            surfaceContainerLowest = DarkTokens.background,
            error = DarkTokens.destructive,
            onError = DarkTokens.destructiveForeground,
            errorContainer = DarkTokens.destructive.over(DarkTokens.background, 0.16f),
            onErrorContainer = DarkTokens.destructive,
            outline = DarkTokens.border,
            outlineVariant = DarkTokens.border,
        )
    } else {
        lightColorScheme(
            primary = LightTokens.primary,
            onPrimary = LightTokens.primaryForeground,
            primaryContainer = LightTokens.primary.over(LightTokens.background, 0.10f),
            onPrimaryContainer = LightTokens.primary,
            secondary = LightTokens.secondaryForeground,
            onSecondary = LightTokens.secondary,
            secondaryContainer = LightTokens.primary.over(LightTokens.background, 0.10f),
            onSecondaryContainer = LightTokens.primary,
            tertiary = LightTokens.info,
            onTertiary = Color.White,
            background = LightTokens.background,
            onBackground = LightTokens.foreground,
            surface = LightTokens.background,
            onSurface = LightTokens.foreground,
            surfaceVariant = LightTokens.muted,
            onSurfaceVariant = LightTokens.mutedForeground,
            surfaceContainer = LightTokens.card,
            surfaceContainerHigh = LightTokens.accent,
            surfaceContainerHighest = LightTokens.accent,
            surfaceContainerLow = LightTokens.card,
            surfaceContainerLowest = LightTokens.card,
            error = LightTokens.destructive,
            onError = LightTokens.destructiveForeground,
            errorContainer = LightTokens.destructive.over(LightTokens.background, 0.10f),
            onErrorContainer = LightTokens.destructive,
            outline = LightTokens.border,
            outlineVariant = LightTokens.border,
        )
    }

    val status = if (darkTheme) {
        AioStatusColors(
            success = DarkTokens.success,
            warning = DarkTokens.warning,
            info = DarkTokens.info,
            border = DarkTokens.border,
            mutedForeground = DarkTokens.mutedForeground,
            card = DarkTokens.card,
            secondary = DarkTokens.secondary,
        )
    } else {
        AioStatusColors(
            success = LightTokens.success,
            warning = LightTokens.warning,
            info = LightTokens.info,
            border = LightTokens.border,
            mutedForeground = LightTokens.mutedForeground,
            card = LightTokens.card,
            secondary = LightTokens.secondary,
        )
    }

    CompositionLocalProvider(LocalAioStatus provides status) {
        MaterialTheme(
            colorScheme = colorScheme,
            typography = AioTypography,
            shapes = AioShapes,
        ) {
            // THE SURFACE IS LOAD-BEARING, not decoration. `LocalContentColor`
            // defaults to BLACK, and MaterialTheme alone does not change that —
            // only a Surface publishes a contentColor. Without this wrapper every
            // `Text` that does not name a colour explicitly renders near-black,
            // which in dark mode is invisible. It shipped that way once: the
            // Collapsible headers and the top-bar title were black-on-charcoal on
            // the first device build.
            Surface(
                modifier = Modifier.fillMaxSize(),
                color = colorScheme.background,
                contentColor = colorScheme.onBackground,
                content = content,
            )
        }
    }
}
