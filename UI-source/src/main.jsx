import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles/globals.css";

const root = ReactDOM.createRoot(document.getElementById("root"));

// DEV-only Device Sync gallery (src/dev/SyncGallery.jsx). The import sits
// inside the import.meta.env.DEV branch so a production build drops it and
// its fixtures entirely; tools/_check_sync_dist.js greps dist for the
// fixtures' "__aioSyncFixture" marker after a build.
if (import.meta.env.DEV && new URLSearchParams(window.location.search).get("gallery") === "sync") {
  import("./dev/SyncGallery").then(({ default: SyncGallery }) =>
    root.render(
      <React.StrictMode>
        <SyncGallery />
      </React.StrictMode>
    )
  );
} else {
  root.render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}
