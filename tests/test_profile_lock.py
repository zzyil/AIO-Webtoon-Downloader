"""Offline coverage for the cross-process browser-profile lock.

WHY THIS MODULE EXISTS AT ALL (measured 2026-08-20): Chromium locks a
user-data-dir, and the desktop library sweep runs up to 8 concurrent
`aio-dl.py --list-chapters` PROCESSES, several landing on the same site. The
losers could not take the `channel="chromium"` launch (full Chromium, which
respects the lock) but the channel-less fallback got in anyway — because it is a
DIFFERENT binary, `chromium_headless_shell`. So contention silently swapped the
clean identity for one advertising HeadlessChrome in Sec-CH-UA,
userAgentData.brands and fullVersionList; Cloudflare then refused to auto-clear,
and background operations may not ask a human. That is the whole reason
mangafire update checks failed while ordinary downloads worked.

These lock down the properties the fix depends on:
  * mutual exclusion ACROSS PROCESSES (the only kind that matters here),
  * a bounded wait — a waiter must fail honestly rather than block forever,
  * reentrancy WITHIN a process, since the signer tears its context down and
    relaunches it in place and must not self-deadlock or drop the claim,
  * the owner pid stays readable while the lock is held (a Windows byte-range
    lock blocks reads of the locked range, which is why the pid is stamped at
    offset 1, not 0).

Cross-file: sites/profile_lock.py, sites/mangafire_vrf.py:_acquire_profile.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from sites import profile_lock


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def lock_dir(tmp_path):
    """A profile path that does not exist yet — the real first-run shape."""
    return str(tmp_path / "profile")


# ────────────────────────────────────────────────────────────────────────
# Basics
# ────────────────────────────────────────────────────────────────────────

def test_lock_file_is_a_sibling_not_a_child(lock_dir):
    """Chromium owns everything under a user-data-dir and may recreate it, so a
    lock living inside is one profile reset away from vanishing while held."""
    path = profile_lock.lock_path_for(lock_dir)
    assert not path.startswith(os.path.abspath(lock_dir) + os.sep)
    assert path == os.path.abspath(lock_dir) + ".lock"


def test_trailing_separator_resolves_to_the_same_lock(tmp_path):
    """`_profile_dir()` results get passed around with and without a trailing
    separator; both must name ONE lock or the exclusion silently splits in two."""
    base = str(tmp_path / "profile")
    assert profile_lock.lock_path_for(base) == profile_lock.lock_path_for(base + os.sep)


def test_registry_returns_one_object_per_path(lock_dir):
    """Two sessions in one process must share a lock object — otherwise the
    second would block on a byte range its own process already holds."""
    assert profile_lock.get(lock_dir) is profile_lock.get(lock_dir)


def test_acquire_release_round_trip(lock_dir):
    lock = profile_lock.get(lock_dir)
    assert lock.held() is False
    assert lock.acquire(1.0) is True
    assert lock.held() is True
    lock.release()
    assert lock.held() is False


def test_acquire_creates_the_parent_directory(tmp_path):
    """First run: neither the profile nor its parent exists yet."""
    nested = str(tmp_path / "deep" / "nested" / "profile")
    lock = profile_lock.get(nested)
    assert lock.acquire(1.0) is True
    try:
        assert os.path.exists(profile_lock.lock_path_for(nested))
    finally:
        lock.release()


# ────────────────────────────────────────────────────────────────────────
# Reentrancy — the signer relaunches its context in place
# ────────────────────────────────────────────────────────────────────────

def test_reentrant_acquire_does_not_deadlock(lock_dir):
    lock = profile_lock.get(lock_dir)
    assert lock.acquire(1.0) is True
    assert lock.acquire(0.0) is True, "a reentrant acquire must be free"
    lock.release()
    assert lock.held() is True, "released too early — the claim was dropped mid-relaunch"
    lock.release()
    assert lock.held() is False


def test_extra_releases_are_harmless(lock_dir):
    """Release is called from teardown paths that may run twice; it must never
    drive the depth negative and hand out a lock that is still in use."""
    lock = profile_lock.get(lock_dir)
    lock.acquire(1.0)
    lock.release()
    lock.release()
    lock.release()
    assert lock.held() is False
    assert lock.acquire(1.0) is True
    lock.release()


# ────────────────────────────────────────────────────────────────────────
# Cross-process exclusion — the only kind that matters here
# ────────────────────────────────────────────────────────────────────────

_HOLDER = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from sites import profile_lock
    lock = profile_lock.get(sys.argv[2])
    got = lock.acquire(2.0)
    print("HELD" if got else "MISSED", flush=True)
    time.sleep(float(sys.argv[3]))
    lock.release()
    """
)


def _spawn_holder(lock_dir, hold_s):
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, REPO_ROOT, lock_dir, str(hold_s)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Block until it reports, so the test never races the child's startup.
    line = proc.stdout.readline().strip()
    assert line == "HELD", f"holder failed to take the lock: {line!r}"
    return proc


def test_a_peer_process_is_excluded(lock_dir):
    """THE property the whole fix rests on."""
    holder = _spawn_holder(lock_dir, 3.0)
    try:
        lock = profile_lock.get(lock_dir)
        assert lock.acquire(0.5) is False, "took a lock another process holds"
    finally:
        holder.wait(timeout=30)


def test_a_waiter_gets_it_once_the_peer_exits(lock_dir):
    """Waiting is the whole point — a peer's check takes seconds, and queueing
    behind it is what keeps every process on the good launch."""
    holder = _spawn_holder(lock_dir, 1.5)
    try:
        lock = profile_lock.get(lock_dir)
        t0 = time.monotonic()
        assert lock.acquire(30.0) is True, "never got the lock after the peer exited"
        waited = time.monotonic() - t0
        assert waited >= 0.2, "returned before the peer could possibly have released"
        lock.release()
    finally:
        holder.wait(timeout=30)


def test_the_wait_is_bounded(lock_dir):
    """A waiter must fail honestly rather than hang: the desktop sweep kills a
    check at 60s, so an unbounded wait would surface as a timeout with no cause."""
    holder = _spawn_holder(lock_dir, 4.0)
    try:
        lock = profile_lock.get(lock_dir)
        t0 = time.monotonic()
        assert lock.acquire(0.75) is False
        waited = time.monotonic() - t0
        assert 0.5 <= waited < 3.0, f"wait budget not honoured (waited {waited:.2f}s)"
    finally:
        holder.wait(timeout=30)


def test_owner_pid_is_readable_while_held(lock_dir):
    """Diagnostics must survive the lock itself.

    On Windows a byte-range lock blocks READS of the locked range from other
    processes, so a pid written at offset 0 would be unreadable by exactly the
    peer that wants to name the holder. It is stamped at offset 1 instead.
    """
    holder = _spawn_holder(lock_dir, 2.5)
    try:
        lock = profile_lock.get(lock_dir)
        owner = lock.peek_owner()
        assert owner == holder.pid, f"expected pid {holder.pid}, read {owner!r}"
    finally:
        holder.wait(timeout=30)


def test_a_dead_holder_does_not_strand_the_profile(lock_dir):
    """The Electron sweep KILLS these procs on cancel (grep onAbort in
    main.js). An OS-level lock is used precisely so the kernel drops it — a pid
    file would need stale-lock collection and would get pid reuse wrong."""
    holder = _spawn_holder(lock_dir, 30.0)
    holder.kill()
    holder.wait(timeout=30)
    lock = profile_lock.get(lock_dir)
    assert lock.acquire(10.0) is True, "a killed process stranded the profile"
    lock.release()


def test_unwritable_lock_location_degrades_instead_of_failing(tmp_path, monkeypatch):
    """A lock we cannot create must not make the browser unusable — that would
    turn a permissions quirk into a dead handler. Degrade to the old
    no-exclusion behaviour instead."""
    lock = profile_lock.ProfileLock(str(tmp_path / "nope" / "x.lock"))
    monkeypatch.setattr(
        profile_lock.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    assert lock.acquire(0.1) is True
    assert lock.held() is True
    lock.release()
    assert lock.held() is False
