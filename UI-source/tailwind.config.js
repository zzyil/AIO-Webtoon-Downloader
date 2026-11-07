/** @type {import('tailwindcss').Config} */
export default {
  // "class" means dark mode activates when a parent element has class="dark"
  // The Electron main process will add/remove this class based on Windows theme
  darkMode: "class",

  // Tell Tailwind which files contain class names to scan. relative: the
  // globs resolve against THIS file's folder, not the process cwd. Without it
  // a Vite started from another folder (tools/_e2e_launch.js's dev server
  // runs in the harness process, cwd = repo root) matched nothing and served
  // CSS with no utilities at all: an unstyled UI with 0x0 checkboxes.
  content: { relative: true, files: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"] },

  theme: {
    extend: {
      // ── Custom colors ──
      // These CSS variables are defined in globals.css and change
      // between light and dark mode automatically
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        // Custom status colors for download states
        success: "hsl(var(--success))",
        warning: "hsl(var(--warning))",
        info: "hsl(var(--info))",
      },

      // ── Border radius uses a CSS variable so shadcn components stay consistent ──
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },

      // ── Font families ──
      fontFamily: {
        sans: ['"DM Sans"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', '"Cascadia Code"', "monospace"],
      },

      // ── Animations for UI transitions ──
      keyframes: {
        "slide-in-right": {
          from: { transform: "translateX(10px)", opacity: "0" },
          to: { transform: "translateX(0)", opacity: "1" },
        },
        "slide-up": {
          from: { transform: "translateY(4px)", opacity: "0" },
          to: { transform: "translateY(0)", opacity: "1" },
        },
        "pulse-subtle": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.7" },
        },
        // Dialogs and popovers appear without movement (Device Sync spec §6).
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
      },
      animation: {
        "slide-in": "slide-in-right 0.2s ease-out",
        "slide-up": "slide-up 0.15s ease-out",
        "pulse-subtle": "pulse-subtle 2s ease-in-out infinite",
        "in-fade": "fade-in 120ms ease-out",
      },
    },
  },

  plugins: [],
};
