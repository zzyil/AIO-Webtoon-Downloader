// ============================================================
// CONFIRM-QUIT DIALOG
//
// Renders ONLY while the main process has a window close pending. main.js's
// mainWindow "close" listener preventDefaults whenever a download is actually
// RUNNING and pushes "confirm-quit" with { running: [{downloadId, title, url,
// startedAt}] }; this component is the answer surface and replies with exactly
// one of confirmQuit / cancelQuit (preload.js).
//
// A React dialog rather than dialog.showMessageBox on purpose: a native modal
// breaks Electron's renderer focus/input handling, which is why every
// confirmation in this app is hand-rolled (see ResumeBar.jsx's delete confirm).
// There is no Dialog primitive in ui/primitives.jsx — the shell here mirrors
// SearchTab's download-settings modal (fixed inset-0 overlay + bordered card +
// animate-slide-up + a justify-end footer).
//
// Mounted once from App.jsx, outside the tab switch, so it can appear no
// matter which tab is open.
//
// NOTE: queued items are NOT a reason to prompt — they survive the close via
// the queue snapshot (useDownloader.js, grep QUEUE_PERSIST_LIMIT), and
// prompting on a draining backlog would fire on nearly every close. main.js
// owns that decision; this component just renders what it's handed.
// ============================================================

import React, { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@/components/ui/primitives";
import { AlertTriangle, Loader2 } from "lucide-react";

// Cap the visible list so a "start alongside" spree can't push the buttons off
// screen; the remainder collapses into a "+N more" line.
const MAX_LISTED = 5;

export default function ConfirmQuitDialog() {
  // null = no close pending (the overwhelmingly common state, and the one
  // where this component renders nothing at all).
  const [pending, setPending] = useState(null);
  const keepRef = useRef(null);

  useEffect(() => {
    const api = typeof window !== "undefined" && window.electronAPI;
    if (!api || typeof api.onConfirmQuit !== "function") return;
    return api.onConfirmQuit((data) => {
      setPending({ running: Array.isArray(data?.running) ? data.running : [] });
    });
  }, []);

  const cancel = useCallback(() => {
    setPending(null);
    // Also clears main's 15s "wedged renderer" safety valve, which would
    // otherwise close the window out from under the user.
    window.electronAPI?.cancelQuit?.();
  }, []);

  const confirm = useCallback(() => {
    setPending(null);
    window.electronAPI?.confirmQuit?.();
  }, []);

  // Escape AND Enter both CANCEL. The safe action is the default on both keys
  // so a reflexive keypress can never kill a running download; quitting stays
  // a deliberate click on a destructive-styled button. Capture phase +
  // preventDefault so Enter doesn't also fire the focused button's click.
  useEffect(() => {
    if (!pending) return;
    const onKey = (e) => {
      if (e.key === "Escape" || e.key === "Enter") {
        e.preventDefault();
        cancel();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [pending, cancel]);

  // Land the focus ring on "Keep downloading", not on the destructive button.
  useEffect(() => {
    if (pending) keepRef.current?.focus();
  }, [pending]);

  if (!pending) return null;

  const running = pending.running;
  const n = running.length;
  const plural = n !== 1;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm px-4">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-quit-title"
        className="w-full max-w-md rounded-lg border bg-card shadow-xl animate-slide-up"
      >
        <div className="flex items-start gap-3 border-b px-4 py-3">
          <span className="flex items-center justify-center w-8 h-8 rounded-lg bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 shrink-0">
            <AlertTriangle className="w-4 h-4" />
          </span>
          <div className="min-w-0">
            <div id="confirm-quit-title" className="text-sm font-semibold">
              {plural ? `${n} downloads are still running` : "A download is still running"}
            </div>
            <div className="text-[11px] text-muted-foreground mt-0.5">
              Closing now stops {plural ? "them" : "it"} mid-chapter.
            </div>
          </div>
        </div>

        <div className="px-4 py-3 space-y-3">
          <ul className="space-y-1">
            {running.slice(0, MAX_LISTED).map((r, i) => (
              <li
                key={r.downloadId || i}
                className="flex items-center gap-2 rounded-md border border-border/60 bg-secondary/40 px-2.5 py-1.5 animate-slide-up"
                style={{ animationDelay: `${Math.min(i * 40, 240)}ms` }}
              >
                <Loader2 className="w-3 h-3 animate-spin text-primary shrink-0" />
                <span className="text-xs truncate">
                  {r.title || r.url || "Download"}
                </span>
              </li>
            ))}
            {n > MAX_LISTED && (
              <li className="text-[10px] text-muted-foreground pl-1 pt-0.5">
                +{n - MAX_LISTED} more
              </li>
            )}
          </ul>

          {/* The point of this dialog is that quitting is CHEAP, not scary —
              say what actually survives so the choice is informed rather than
              a speed bump. */}
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            Chapters that already finished are kept:{" "}
            {plural ? "each run reappears" : "the run reappears"} in the{" "}
            <span className="font-medium text-foreground">Resume</span> bar next
            launch and picks up where it stopped. Anything still queued is
            restored automatically.
          </p>
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-4 py-3">
          <Button variant="destructive" size="sm" onClick={confirm}>
            Quit anyway
          </Button>
          <Button ref={keepRef} size="sm" autoFocus onClick={cancel}>
            Keep downloading
          </Button>
        </div>
      </div>
    </div>
  );
}
