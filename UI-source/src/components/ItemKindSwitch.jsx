// ============================================================
// Chapters | Volumes segmented switch for native volume downloads.
//
// Rendered only where the URL's site has native volumes
// (lib/siteCapabilities.js; MangaFire today). Consumers:
//   - DownloadTab.jsx: under the URL box.
//   - SearchTab.jsx SearchDownloadOptionsDialog: in place of the range
//     field's "Chapters" label (`compact`), so it sits on the field it
//     relabels (user decision 2026-11-06).
// The value is job data (downloadForm.js DEFAULT_FORM.downloadVolumes), never
// a setting. `disabled` (Komikku on: aio-dl.py's --komikku turns volumes off)
// keeps the switch visible so the caller can show the reason next to it.
// ============================================================

import React from "react";
import { BookOpen, Layers } from "lucide-react";
import { cn } from "@/lib/utils";

const OPTIONS = [
  { value: false, label: "Chapters", Icon: Layers },
  { value: true, label: "Volumes", Icon: BookOpen },
];

export default function ItemKindSwitch({ volumes, disabled, onChange, compact = false, className }) {
  return (
    <div
      role="radiogroup"
      aria-label="Download chapters or volumes"
      className={cn("inline-flex rounded-md border p-0.5", disabled && "opacity-50", className)}
    >
      {OPTIONS.map(({ value, label, Icon }) => (
        <button
          key={label}
          type="button"
          role="radio"
          aria-checked={volumes === value}
          disabled={disabled}
          onClick={() => onChange(value)}
          className={cn(
            "flex items-center rounded font-medium transition-colors",
            compact ? "gap-1 px-2 py-px text-[11px]" : "gap-1.5 px-3 py-1 text-xs",
            volumes === value
              ? "bg-primary/10 text-primary"
              : "text-muted-foreground hover:bg-accent",
            disabled && "cursor-not-allowed"
          )}
        >
          <Icon className={compact ? "h-3 w-3" : "h-3.5 w-3.5"} />
          {label}
        </button>
      ))}
    </div>
  );
}
