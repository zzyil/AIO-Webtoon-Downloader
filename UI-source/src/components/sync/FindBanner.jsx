// ============================================================
// FindBanner — the Search tab's "Finding a source for <folder> on <target>"
// callout (UI pass U4-6/7; mockup findSearchBanner())
//
// Shown while App holds a single-find request (App.jsx `findFor`). While it
// is up, a download started from Search is linked to that folder first
// (config-op add-alias), so it syncs into the existing folder on the target.
// `error` is that link's refusal; nothing was queued then. Until the find's
// own search has started (`searched`, SearchTab), nothing is linked and the
// banner says so (review R13).
// Consumer: SearchTab.jsx.
// ============================================================

import React from "react";
import { Link2, X } from "lucide-react";
import { Callout } from "./SyncCards";

/** @param {{findFor: {folder, targetName, error?}, onDismiss: () => void}} p */
export default function FindBanner({ findFor, onDismiss }) {
  if (!findFor) return null;
  return (
    <div data-testid="find-banner">
      <Callout tone={findFor.error ? "bad" : "info"} icon={Link2}>
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <b className="font-semibold">
              Finding a source for “{findFor.folder}” on {findFor.targetName}.
            </b>{" "}
            <span className="text-muted-foreground">
              {findFor.searched
                ? "A download you start from these results is linked to that folder, so it's synced into it instead of creating a new one."
                : "Another search is still running. The search for this folder starts when it ends; downloads from the results above aren't linked to the folder."}
            </span>
            {findFor.error && <div className="mt-1 text-destructive">Not queued: {findFor.error}</div>}
          </div>
          <button type="button" onClick={onDismiss} aria-label="Stop finding a source" title="Stop finding a source" className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </Callout>
    </div>
  );
}
