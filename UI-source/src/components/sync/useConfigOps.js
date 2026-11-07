// ============================================================
// useConfigOps — a surface's handle on the app's config-op client
//
// The client itself (serialization across every surface, the newest config
// version) lives in useDeviceSync's actions.configOp (see its CONFIG-OP
// CLIENT header): one queue for the whole app, so an edit on the connect
// card and one in the Sync tab can't race each other into a
// version-conflict. This hook only counts the ops THIS surface has in
// flight, so its buttons can disable while one runs.
//
// Every op is an intent (entry-named removals, edit-list add/remove, scalar
// patches; service.js OPS), never a list rebuilt from a render, which is
// what makes sending the newest version safe (review R12).
//
// A genuine conflict still refuses; the caller shows copy.js
// configRefusalCopy and the next config event re-renders current values.
//
// Consumers: App.jsx, SettingsTab.jsx, SyncTab.jsx, ConnectPrompt.jsx (each
// passes `ops` down to TargetEditor, SeriesSheet, FindSourcesSheet, …).
// Depends on: useDeviceSync's actions.configOp (App.jsx passes the hook down
// as `sync`).
// ============================================================

import { useCallback, useMemo, useState } from "react";

export function useConfigOps(sync) {
  const [pending, setPending] = useState(0);
  const actions = sync && sync.actions;
  const run = useCallback(
    (op, payload) => {
      setPending((n) => n + 1);
      const p = actions ? actions.configOp({ op, ...(payload || {}) }) : Promise.resolve({ ok: false, code: "disabled", message: "Device Sync isn't running" });
      return Promise.resolve(p).finally(() => setPending((n) => n - 1));
    },
    // actions is memoised for the hook's lifetime, so run is stable and a
    // memo'd row that takes a callback built on it stays memo'd.
    [actions],
  );
  const busy = pending > 0;
  return useMemo(() => ({ run, pending: busy }), [run, busy]);
}

/** The field a refusal is about (validateTarget errors[0].field), else null. */
export function refusalField(r) {
  return r && Array.isArray(r.errors) && r.errors[0] ? r.errors[0].field : null;
}
