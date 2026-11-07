// ============================================================
// SHADCN-STYLE UI PRIMITIVES
//
// These are custom components styled with Tailwind that look
// like polished desktop UI controls. Each one accepts className
// so you can add extra styles where needed.
// ============================================================

import React, { forwardRef } from "react";
import { cn } from "@/lib/utils";

// ── Button ──
// Variants: default (blue), secondary (gray), destructive (red), ghost (transparent)
export const Button = forwardRef(
  ({ className, variant = "default", size = "default", children, ...props }, ref) => {
    const variants = {
      default:
        "bg-primary text-primary-foreground hover:bg-primary/90 shadow-sm",
      secondary:
        "bg-secondary text-secondary-foreground hover:bg-secondary/80",
      destructive:
        "bg-destructive text-destructive-foreground hover:bg-destructive/90",
      ghost:
        "hover:bg-accent hover:text-accent-foreground",
      outline:
        "border border-input bg-transparent hover:bg-accent hover:text-accent-foreground",
    };
    const sizes = {
      default: "h-9 px-4 py-2 text-sm",
      sm: "h-8 px-3 text-xs",
      lg: "h-10 px-6 text-base",
      icon: "h-9 w-9",
    };

    return (
      <button
        ref={ref}
        className={cn(
          "inline-flex items-center justify-center rounded-md font-medium transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "disabled:pointer-events-none disabled:opacity-50",
          variants[variant],
          sizes[size],
          className
        )}
        {...props}
      >
        {children}
      </button>
    );
  }
);
Button.displayName = "Button";

// ── Input (text field) ──
export const Input = forwardRef(({ className, ...props }, ref) => {
  return (
    <input
      ref={ref}
      className={cn(
        "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1",
        "text-sm shadow-sm transition-colors placeholder:text-muted-foreground",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  );
});
Input.displayName = "Input";

// ── Textarea ──
export const Textarea = forwardRef(({ className, ...props }, ref) => {
  return (
    <textarea
      ref={ref}
      className={cn(
        "flex min-h-[80px] w-full rounded-md border border-input bg-background px-3 py-2",
        "text-sm shadow-sm placeholder:text-muted-foreground",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  );
});
Textarea.displayName = "Textarea";

// ── Label ──
export const Label = forwardRef(({ className, ...props }, ref) => {
  return (
    <label
      ref={ref}
      className={cn(
        "text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70",
        className
      )}
      {...props}
    />
  );
});
Label.displayName = "Label";

// ── Toggle switch (like a light switch) ──
export function Switch({ checked, onCheckedChange, disabled, className, id }) {
  return (
    <button
      type="button"
      id={id}
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onCheckedChange?.(!checked)}
      className={cn(
        "peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full",
        "border-2 border-transparent transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-primary" : "bg-input",
        className
      )}
    >
      <span
        className={cn(
          "pointer-events-none block h-4 w-4 rounded-full bg-background shadow-lg ring-0 transition-transform",
          checked ? "translate-x-4" : "translate-x-0"
        )}
      />
    </button>
  );
}

// ── Slider (range input) ──
export function Slider({ value, onValueChange, min = 0, max = 100, step = 1, className, disabled }) {
  return (
    <input
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      disabled={disabled}
      onChange={(e) => onValueChange?.(Number(e.target.value))}
      className={cn(
        "w-full h-2 rounded-full appearance-none cursor-pointer",
        "bg-secondary accent-primary",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
    />
  );
}

// ── Select dropdown ──
export const Select = forwardRef(({ className, children, ...props }, ref) => {
  return (
    <select
      ref={ref}
      className={cn(
        "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1",
        "text-sm shadow-sm transition-colors",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    >
      {children}
    </select>
  );
});
Select.displayName = "Select";

// ── Checkbox ──
// `checked` is true, false or "mixed" (a group with some rows ticked); clicking
// a mixed box ticks it. Three visual states for Device Sync's selection lists
// (approved design spec §7, sync-temp/design/approved/spec.md):
//   pending — a change sent to main and not yet answered (55% opacity, still
//             clickable: the next click batches behind it);
//   locked  — muted and inert, e.g. every row while a sync job writes the target;
//   danger  — a destructive tick (removals, losses), filled red.
export function Checkbox({ checked, onCheckedChange, disabled, id, className, pending, locked, danger, title, "aria-label": ariaLabel }) {
  const mixed = checked === "mixed";
  const on = checked === true;
  return (
    <button
      type="button"
      id={id}
      role="checkbox"
      aria-checked={mixed ? "mixed" : !!on}
      aria-busy={pending || undefined}
      aria-label={ariaLabel}
      title={title}
      disabled={disabled || locked}
      onClick={() => onCheckedChange?.(mixed ? true : !on)}
      className={cn(
        "peer h-4 w-4 shrink-0 rounded border border-primary shadow-sm",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "flex items-center justify-center transition-opacity",
        locked
          ? "cursor-not-allowed border-muted-foreground/50 bg-muted text-primary-foreground"
          : "disabled:cursor-not-allowed disabled:opacity-50",
        on || mixed
          ? locked
            ? "bg-muted-foreground/50"
            : danger
              ? "bg-destructive border-destructive text-destructive-foreground"
              : "bg-primary text-primary-foreground"
          : locked
            ? ""
            : danger
              ? "bg-background border-destructive"
              : "bg-background",
        pending && "opacity-[.55]",
        className
      )}
    >
      {on && (
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">
          <path
            d="M1.5 5.5L3.5 7.5L8.5 2.5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      )}
      {mixed && (
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">
          <path d="M2 5h6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      )}
    </button>
  );
}

// ── Segmented control (one of a few short choices) ──
// options: [{value, label, title?, disabled?}]. A radiogroup of buttons; the
// chosen one fills primary. Used by Device Sync for Removals, kind, error
// budget, size warning (spec §7). size "sm" = 28px (the mockup), "xs" = 24px.
export function SegmentedControl({ options, value, onChange, disabled, size = "sm", className, "aria-label": ariaLabel }) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        "inline-flex rounded-md overflow-hidden shadow-[inset_0_0_0_1px_hsl(var(--input))]",
        disabled && "opacity-50",
        className
      )}
    >
      {options.map((o, i) => {
        const on = o.value === value;
        return (
          <button
            key={String(o.value)}
            type="button"
            role="radio"
            aria-checked={on}
            title={o.title}
            disabled={disabled || o.disabled}
            onClick={() => !on && onChange?.(o.value)}
            className={cn(
              "inline-flex items-center px-2.5 text-xs tabular-nums whitespace-nowrap transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
              "disabled:cursor-not-allowed",
              size === "xs" ? "h-6" : "h-7",
              i > 0 && "border-l border-input",
              on ? "bg-primary text-primary-foreground" : "hover:bg-accent"
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ── Card (container) ──
export function Card({ className, children, ...props }) {
  return (
    <div
      className={cn("rounded-lg border bg-card text-card-foreground shadow-sm", className)}
      {...props}
    >
      {children}
    </div>
  );
}

// ── Badge (small label) ──
// success / warning / destructive are the original literal-colour variants;
// existing surfaces keep them. The token-* variants are theme tokens (approved
// Device Sync design spec §2): warning and info text use the darker
// .text-warn-fg / .text-info-fg utilities (globals.css) because the raw tokens
// are too light for text on white. New surfaces use token-*. Sync UI never
// uses orange: orange is the Library "N new" badge.
export function Badge({ className, variant = "default", children, title }) {
  const variants = {
    default: "bg-primary/10 text-primary border-primary/20",
    secondary: "bg-secondary text-secondary-foreground border-secondary",
    success: "bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20",
    warning: "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 border-yellow-500/20",
    destructive: "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20",
    "token-success": "bg-success/10 text-success border-success/25",
    "token-warning": "bg-warning/[.12] text-warn-fg border-warning/30",
    "token-destructive": "bg-destructive/10 text-destructive border-destructive/25",
    "token-info": "bg-info/10 text-info-fg border-info/25",
    "token-muted": "bg-secondary text-secondary-foreground border-secondary",
  };
  const token = variant.startsWith("token-");

  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center rounded-full border font-medium whitespace-nowrap",
        token ? "gap-1 px-2 py-px text-[11px] leading-4 [&_svg]:w-[11px] [&_svg]:h-[11px]" : "px-2.5 py-0.5 text-xs",
        variants[variant],
        className
      )}
    >
      {children}
    </span>
  );
}

// ── Section header for form groups ──
export function SectionHeader({ children, className }) {
  return (
    <div className={cn("flex items-center gap-3 pt-4 pb-2", className)}>
      <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {children}
      </span>
      <div className="flex-1 h-px bg-border" />
    </div>
  );
}

// ── Collapsible section ──
export function Collapsible({ title, children, defaultOpen = false }) {
  const [open, setOpen] = React.useState(defaultOpen);

  return (
    <div className="border rounded-md">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center justify-between w-full px-3 py-2 text-sm font-medium hover:bg-accent/50 transition-colors rounded-md"
      >
        <span>{title}</span>
        <svg
          className={cn("w-4 h-4 transition-transform", open && "rotate-180")}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open && <div className="px-3 pb-3 pt-1 border-t">{children}</div>}
    </div>
  );
}
