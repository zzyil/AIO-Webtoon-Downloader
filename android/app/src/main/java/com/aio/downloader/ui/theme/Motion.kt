package com.aio.downloader.ui.theme

import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.Easing
import androidx.compose.animation.core.tween
import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.slideInVertically

/**
 * The desktop's motion vocabulary, ported from `tailwind.config.js` keyframes
 * and the `transition-*` durations sprinkled through the components.
 *
 * It is a SHORT list on purpose. The desktop app has exactly three named
 * animations and two transition durations, and the restraint is the point: this
 * is a tool that runs for twenty minutes at a stretch, so anything that moves
 * repeatedly during a download becomes irritating by chapter forty. Motion here
 * marks arrivals and state changes, never idle decoration.
 *
 * Cross-file: `UI-source/tailwind.config.js` (keyframes `slide-up`,
 * `slide-in-right`, `pulse-subtle`) and the `duration-300` / `duration-500`
 * classes in DownloadTab.jsx and QueueTab.jsx.
 */
object AioMotion {
    /** `animate-slide-up` — 0.15s ease-out. New rows, revealed sub-forms. */
    const val SLIDE_UP_MS = 150

    /** `transition-colors duration-300` — a callout crossing to a new state. */
    const val COLOR_MS = 300

    /**
     * `transition-all duration-500 ease-out` on the progress bar fill. Slow
     * enough that a chapter tick reads as movement rather than a jump, and it
     * comfortably outlasts the 700ms poll interval so the bar is still easing
     * when the next batch lands — which is what keeps it from looking steppy.
     */
    const val PROGRESS_MS = 500

    /** Tailwind's `ease-out`. */
    val EaseOut: Easing = CubicBezierEasing(0f, 0f, 0.2f, 1f)

    /**
     * `animate-slide-up` as an AnimatedVisibility pair. Height animates too —
     * on the desktop a revealed sub-form pushes the page down through normal
     * layout; in Compose that has to be asked for explicitly or the content
     * appears in a hole that was already there.
     */
    val revealEnter: EnterTransition =
        fadeIn(tween(SLIDE_UP_MS, easing = EaseOut)) +
            slideInVertically(tween(SLIDE_UP_MS, easing = EaseOut)) { -it / 6 } +
            expandVertically(tween(SLIDE_UP_MS, easing = EaseOut))

    val revealExit: ExitTransition =
        fadeOut(tween(SLIDE_UP_MS / 2)) + shrinkVertically(tween(SLIDE_UP_MS, easing = EaseOut))
}
