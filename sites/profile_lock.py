from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Cross-process exclusive lock over a Chromium user-data-dir.
#
# What this module owns: deciding WHICH process may hold a persistent browser
# profile right now, and making the losers WAIT instead of racing.
#
# Who reads from it: sites/mangafire_vrf.py (grep `profile_lock`). Written to be
# reusable by sites/comix.py, which keeps a persistent profile the same way and
# has the same hazard — so keep mangafire specifics out of here.
#
# Depends on: nothing outside the stdlib.
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS (measured 2026-08-20 — not a theoretical race)
#
# Chromium locks a user-data-dir, so exactly one browser may hold a profile.
# The desktop app runs the library update sweep as a POOL of up to 8 concurrent
# `aio-dl.py --list-chapters` PROCESSES (UI-source/electron/main.js, grep
# check-all-updates), several of which land on the same site — so several
# processes reach for one profile at the same instant.
#
# The trap that made this urgent: the two launches are NOT the same binary.
#
#   launch                         binary                   on a locked profile
#   channel="chromium"             chrome.exe (full)        REFUSES (TargetClosedError)
#   default headless (no channel)  chromium_headless_shell  GETS IN ANYWAY
#
# So the "degrade gracefully" fallback was, under contention, GUARANTEED to be
# taken and GUARANTEED to succeed — quietly swapping the one launch that
# presents a clean identity for the one that advertises HeadlessChrome in
# Sec-CH-UA, navigator.userAgentData.brands AND fullVersionList (measured; see
# the table in sites/browser_identity.py). Cloudflare's Managed Challenge then
# refuses to auto-clear, and a background operation may not ask a human — which
# is exactly why update checks failed while ordinary downloads (one uncontended
# process) kept working.
#
# It also meant two browsers writing one cookie jar, which is how a hard-won
# cf_clearance gets clobbered.
#
# The fix is to stop racing: acquire this lock BEFORE launching, so peers queue
# and each in turn gets the good launch. Waiting costs seconds; the fallback
# cost the run its identity.
#
# WHY AN OS-LEVEL LOCK rather than a pid file: the kernel drops it when the
# holder dies, so a killed proc (the Electron app kills these on cancel — grep
# onAbort in main.js) can never strand the profile. There is no stale-lock path
# to garbage-collect, which a pid file would need and would get wrong on pid
# reuse.
# ---------------------------------------------------------------------------


# Lock objects are per PROFILE PATH, not per caller: two sessions in one process
# must share one lock object, or the second would deadlock against a byte range
# its own process already holds.
_REGISTRY: Dict[str, "ProfileLock"] = {}
_REGISTRY_LOCK = threading.Lock()

# Poll interval while waiting. Short enough that a queue of update-check
# processes drains promptly, long enough not to spin a core.
_POLL_S = 0.25


def lock_path_for(profile_dir: str) -> str:
    """Sibling lock file for *profile_dir*.

    Deliberately BESIDE the profile rather than inside it: Chromium owns
    everything under a user-data-dir and is entitled to rewrite it (and a first
    run creates it from scratch), so a lock file living in there is one profile
    reset away from being deleted while held.
    """
    return os.path.abspath(profile_dir.rstrip("\\/")) + ".lock"


class ProfileLock:
    """Reentrant-per-process, exclusive-across-processes lock on one profile.

    Reentrancy is load-bearing, not decoration: the mangafire signer tears its
    context down and relaunches it in place (mode switch for the verification
    window, plus the one permitted UA relaunch). Those must happen INSIDE one
    continuous hold — releasing between them would let a peer steal the profile
    mid-handoff and hand the user a solved challenge for a browser that no
    longer exists.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd = -1
        self._depth = 0
        # Guards _fd/_depth against two threads in one process driving the same
        # profile. The OS lock provides only the cross-process half; an
        # exclusive lock needs intra-process exclusion too. Same reasoning as
        # aio-dl.py:_AIOFileLock's RACE-1 note.
        self._guard = threading.Lock()

    # ---------------------------------------------------------------

    def _try_lock_fd(self) -> bool:
        """One non-blocking attempt at the OS lock. True when it is ours.

        Non-blocking on BOTH platforms so the caller owns the deadline:
        msvcrt's blocking LK_LOCK spins for a fixed ~10s and then raises, which
        is neither the timeout the caller asked for nor a value it can act on.
        """
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            return False
        return True

    def _unlock_fd(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:
            pass

    def _stamp_owner(self) -> None:
        """Record who holds it, for the waiter's diagnostics.

        Written at offset 1 because byte 0 is the LOCKED range: on Windows a
        byte-range lock blocks reads of that range from other processes, so a
        pid stored there would be unreadable by exactly the peer that wants it.
        """
        try:
            os.lseek(self._fd, 1, os.SEEK_SET)
            os.write(self._fd, f"{os.getpid()}".ljust(24).encode("ascii", "replace"))
        except Exception:
            pass

    def peek_owner(self) -> Optional[int]:
        """Best-effort pid of the current holder, or None. Diagnostics only —
        inherently racy, so never branch on it."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(1)
                raw = (fh.read(24) or b"").decode("ascii", "replace").strip()
            return int(raw) if raw.isdigit() else None
        except Exception:
            return None

    # ---------------------------------------------------------------

    def acquire(self, timeout_s: float, *, strict: bool = False) -> bool:
        """Take the lock, waiting up to *timeout_s*. True when held.

        A reentrant acquire succeeds immediately and does NOT re-take the OS
        lock: Windows byte-range locks conflict with the same process on a
        second handle, so re-taking would self-deadlock.

        ``strict`` refuses the unwritable-location degrade below: True then
        means "the OS lock is really ours", never "we could not tell". Callers
        that DELETE things on a True (mangafire_vrf's temporary-profile sweep,
        grep _sweep_ephemeral_profiles) must pass it.
        """
        with self._guard:
            if self._depth > 0:
                self._depth += 1
                return True
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
            except Exception:
                if strict:
                    return False
                # An unwritable lock location must not make the browser
                # unusable — degrade to "no cross-process exclusion", which is
                # exactly the old behaviour, rather than failing the run.
                self._depth = 1
                self._fd = -1
                return True
            self._fd = fd
            try:
                # Byte 0 must exist before it can be locked.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
            except Exception:
                pass

            deadline = time.monotonic() + max(0.0, float(timeout_s))
            while True:
                if self._try_lock_fd():
                    self._depth = 1
                    self._stamp_owner()
                    return True
                if time.monotonic() >= deadline:
                    break
                time.sleep(_POLL_S)

            try:
                os.close(fd)
            except Exception:
                pass
            self._fd = -1
            return False

    def release(self) -> None:
        with self._guard:
            if self._depth <= 0:
                return
            self._depth -= 1
            if self._depth > 0:
                return
            if self._fd >= 0:
                self._unlock_fd()
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = -1

    def held(self) -> bool:
        with self._guard:
            return self._depth > 0


def get(profile_dir: str) -> ProfileLock:
    """The one ProfileLock for *profile_dir* in this process."""
    path = lock_path_for(profile_dir)
    with _REGISTRY_LOCK:
        lock = _REGISTRY.get(path)
        if lock is None:
            lock = ProfileLock(path)
            _REGISTRY[path] = lock
        return lock
