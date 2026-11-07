// PostCSS for Vite (dev server and build). Tailwind's config is named by its
// path next to this file: the plugin otherwise looks for tailwind.config.js
// in the process cwd, and a Vite started from another folder (the E2E
// launcher's in-process dev server, cwd = repo root) got Tailwind's empty
// default config and CSS with no utilities. tailwind.config.js's
// content.relative does the same for its source globs (grep relative).
import { fileURLToPath } from "url";

export default {
  plugins: {
    tailwindcss: { config: fileURLToPath(new URL("./tailwind.config.js", import.meta.url)) },
    autoprefixer: {},
  },
};
