from __future__ import annotations

import os
import threading
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Shared browser-identity levers for the Patchright-driven handlers.
#
# What this module owns: the two settings that decide whether a headless
# Chromium presents ONE coherent identity or contradicts itself — the launch
# `channel` and the pinned User-Agent — plus the CDP probe that reads the
# browser's REAL UA past any override, and the per-profile UA cache file.
#
# Who reads from it: sites/comix.py (grep _COMIX_BROWSER_CHANNEL) and
# sites/mangafire_vrf.py (grep _launch_with_identity). Both hit the same wall
# independently; the table below was measured ONCE and must not be re-derived
# per handler, which is the whole reason this module exists.
#
# Depends on: nothing at import. probe_true_user_agent takes a live Playwright
# context+page, so the Patchright import stays in the caller.
#
# ---------------------------------------------------------------------------
# THE MEASUREMENT (2026-08-02, Patchright's bundled Chromium against a local
# server, reading request headers AND navigator.userAgentData):
#
#   launch                        UA header          Sec-CH-UA / userAgentData
#   headless, no pin              HeadlessChrome     HeadlessChrome
#   headless, UA pinned           Chrome  (fixed)    HeadlessChrome  (LEAKS)
#   headless, channel, no pin     HeadlessChrome     Chromium        (LEAKS)
#   headless, channel + UA pin    Chrome             Chromium        <- clean
#   headed,  channel + UA pin     Chrome             Chromium        <- identical
#
# NEITHER LEVER IS SUFFICIENT ALONE. Playwright's `user_agent=` calls
# Emulation.setUserAgentOverride WITHOUT userAgentMetadata, so it can only ever
# move the header; the channel governs the client hints but leaves
# `HeadlessChrome` in the UA string. Shipping the pin alone (comix commit
# b014f12) left the context announcing HeadlessChrome in the hints while its UA
# claimed Chrome — a contradiction no real browser emits, arguably a WORSE
# signal than the plain headless it replaced.
#
# `--headless=new` as a bare arg does NOT work (measured: hints still leak); it
# has to be the channel.
# ---------------------------------------------------------------------------

BROWSER_CHANNEL = "chromium"

UA_CACHE_FILENAME = "aio-stable-ua.txt"

# Used only when no browser has run yet in this profile, so there is no probed
# UA to reuse. Kept in step with Patchright's bundled Chromium; being a little
# behind is harmless (plenty of real clients are), being a DECADE behind is not.
#
# The trailing `.0.0` is not laziness: Chrome's UA reduction freezes the minor
# fields, so a real Chrome 147 reports exactly `Chrome/147.0.0.0` and the full
# build number lives only in Sec-CH-UA-Full-Version-List. Writing a real build
# here (e.g. 147.0.7727.15) would be the one UA no genuine Chrome ever sends.
FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# profile dir -> UA. Saves re-reading the cache file on every launch in a
# process that drives several sessions.
_UA_MEMO: Dict[str, str] = {}
_UA_LOCK = threading.Lock()


def stabilize_user_agent(raw: Optional[str]) -> Optional[str]:
    """Return *raw* with the headless giveaway removed.

    Only rewrites the product token — the version string and everything else
    stay exactly as the real browser reports them, so the result is still a
    truthful description of the engine actually making the request.

    NOT SUFFICIENT ON ITS OWN — see the table above. This rewrites the
    User-Agent STRING; the Sec-CH-UA client hints are generated independently by
    the browser and keep saying HeadlessChrome without `channel=`.
    """
    if not raw:
        return None
    return raw.replace("HeadlessChrome/", "Chrome/")


def probe_true_user_agent(context, page) -> Optional[str]:
    """The browser's REAL User-Agent, seen past any page-level override.

    `page.evaluate("navigator.userAgent")` is useless for this once
    `user_agent=` is set on the context: Emulation.setUserAgentOverride makes it
    report our own pin straight back, so a wrong pin looks self-consistent and
    survives forever — probe sees the pin, agrees with the pin, re-caches the
    pin. CDP `Browser.getVersion` reports the browser itself, override or not
    (verified 2026-08-02: page said the pinned `Chrome/999.0.1.2`, getVersion
    said the true `HeadlessChrome/147.0.0.0`).

    Falls back to the page's own view when CDP is unavailable — which is correct
    precisely in the case that makes CDP necessary impossible, i.e. when nothing
    is overriding the UA.
    """
    if context is None or page is None:
        return None
    cdp = None
    try:
        cdp = context.new_cdp_session(page)
        version = cdp.send("Browser.getVersion") or {}
        ua = version.get("userAgent")
        if ua:
            return ua
    except Exception:
        pass
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass
    try:
        return page.evaluate("navigator.userAgent")
    except Exception:
        return None


def cached_stable_user_agent(profile_dir: str) -> Optional[str]:
    """The UA this profile's browser presented last time, or None.

    None (rather than FALLBACK_UA) is the useful answer for a launch site: it
    means "nothing pinned yet", which is what tells the caller to probe the
    launched browser and relaunch once with the real value.
    """
    if not profile_dir:
        return None
    with _UA_LOCK:
        memo = _UA_MEMO.get(profile_dir)
    if memo:
        return memo
    try:
        with open(os.path.join(profile_dir, UA_CACHE_FILENAME), "r", encoding="utf-8") as fh:
            cached = (fh.read() or "").strip()
    except Exception:
        cached = ""
    if not cached:
        return None
    with _UA_LOCK:
        _UA_MEMO[profile_dir] = cached
    return cached


def remember_stable_user_agent(profile_dir: str, ua: str) -> None:
    """Persist the stabilized UA beside the profile it belongs to, so later
    processes launch with it directly instead of re-probing."""
    if not profile_dir or not ua:
        return
    with _UA_LOCK:
        _UA_MEMO[profile_dir] = ua
    try:
        os.makedirs(profile_dir, exist_ok=True)
        with open(os.path.join(profile_dir, UA_CACHE_FILENAME), "w", encoding="utf-8") as fh:
            fh.write(ua)
    except Exception:
        # A read-only profile dir just means we re-probe next process.
        pass
