// ============================================================
// Which site handler a URL belongs to, and whether it offers native volumes.
//
// Source of truth: `aio-dl.py --site-capabilities` (sites.handler_capabilities),
// fetched once per app session through the "site-capabilities" IPC
// (electron/main.js caches it). Matching is by host against each handler's
// `domains`, done here, rather than by asking Python about the URL: a MangaFire
// probe would launch its signer browser on every keystroke.
//
// Consumers: components/DownloadTab.jsx (Chapters | Volumes switch),
// components/LibraryTab.jsx ("Download volumes" on a series page).
// ============================================================

import { useEffect, useState } from "react";

let _cache = null; // Promise<Array<{name, domains, volumes}>> for the session

export function loadSiteCapabilities() {
  if (!_cache) {
    const api = typeof window !== "undefined" ? window.electronAPI : null;
    if (!api?.siteCapabilities) return Promise.resolve([]);
    _cache = api.siteCapabilities().then((r) => {
      if (!r?.ok || !Array.isArray(r.sites)) {
        _cache = null; // main didn't cache a failure either; retry next time
        return [];
      }
      return r.sites;
    }, () => {
      _cache = null;
      return [];
    });
  }
  return _cache;
}

// [] until loaded (and on failure), so callers simply offer no volume option.
export function useSiteCapabilities() {
  const [sites, setSites] = useState([]);
  useEffect(() => {
    let live = true;
    loadSiteCapabilities().then((s) => { if (live) setSites(s); });
    return () => { live = false; };
  }, []);
  return sites;
}

function hostOf(url) {
  try {
    return new URL(String(url).trim()).hostname.toLowerCase();
  } catch {
    return null;
  }
}

// The handler whose domain list covers `url`'s host (exact or a subdomain of
// a listed domain), or null.
export function siteForUrl(url, sites) {
  const host = hostOf(url);
  if (!host || !Array.isArray(sites)) return null;
  for (const s of sites) {
    for (const d of s.domains || []) {
      const dom = String(d).toLowerCase();
      if (host === dom || host.endsWith(`.${dom}`)) return s;
    }
  }
  return null;
}

export function volumesSupportedForUrl(url, sites) {
  return !!siteForUrl(url, sites)?.volumes;
}

// By handler name, as stored in .aio_series.json "site" (Library entries).
export function volumesSupportedForSite(siteName, sites) {
  if (!siteName || !Array.isArray(sites)) return false;
  return !!sites.find((s) => s.name === siteName)?.volumes;
}
