"""Tests for the parallel image prefetch chain introduced in Phase B
of the fast-download generalization (2026-05-13).

Pre-Phase-B: single in-flight prefetch with a threading.Lock + thread
handle. Post-Phase-B: queue + multi-worker pool dispatching prefetch
jobs in parallel, coordinated with main via per-chapter threading.Events
and the same `.download_prefetched` marker file on success.

These tests verify the queue/dedupe/marker contract by mocking the
download body. No network. Run from repo root with `pytest`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time

import pytest


# Load aio-dl.py as the module `aio_dl` (filename has a hyphen so a plain
# `import` doesn't work). Cached in sys.modules.
def _aio_dl():
    if "aio_dl" in sys.modules:
        return sys.modules["aio_dl"]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "aio_dl", os.path.join(here, "aio-dl.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aio_dl"] = mod
    spec.loader.exec_module(mod)
    return mod


# Reset module state between tests so one test's leftover state doesn't
# leak into the next. Workers stay alive across tests (daemon threads),
# but the dedupe sets and done events are reset.
@pytest.fixture(autouse=True)
def _reset_prefetch_state():
    mod = _aio_dl()
    with mod._image_prefetch_lock:
        mod._image_prefetch_seen.clear()
        mod._image_prefetch_done.clear()
    yield
    with mod._image_prefetch_lock:
        mod._image_prefetch_seen.clear()
        mod._image_prefetch_done.clear()


@pytest.fixture
def temp_main_tmp_dir():
    """Fresh temp directory for prefetch target_tdirs. Cleaned up after."""
    d = tempfile.mkdtemp(prefix="aio_test_prefetch_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────
# Dedupe: re-enqueueing the same chap is a no-op
# ────────────────────────────────────────────────────────────────────────

def test_dedupe_via_seen_set_prevents_double_enqueue(temp_main_tmp_dir):
    """Two _start_image_prefetch calls for the same chap should only
    result in one queue entry. Pre-load _image_prefetch_seen so we don't
    have to actually run the worker."""
    mod = _aio_dl()

    # Manually populate seen + done as if the first call already queued
    # the job (without actually running the worker — too racy for unit test).
    with mod._image_prefetch_lock:
        mod._image_prefetch_seen.add("5")
        mod._image_prefetch_done["5"] = threading.Event()

    initial_queue_size = mod._image_prefetch_queue.qsize()

    # Second call for the same chap should bail at the seen-set check
    # without touching the queue.
    next_ch = {"chap": "5", "url": "http://example/ch5"}
    target_tdir = os.path.join(temp_main_tmp_dir, "ch_5")
    mod._start_image_prefetch(
        next_ch, target_tdir, scraper=None, handler=None,
        image_workers=2, fast_concurrency=8,
    )

    assert mod._image_prefetch_queue.qsize() == initial_queue_size


def test_dedupe_after_completion_still_skips(temp_main_tmp_dir):
    """Even after the event is set (worker finished), re-enqueueing the
    same chap should be a no-op. _consume_image_prefetch clears the
    done entry but leaves the seen entry — so the next enqueue dedupe-skips."""
    mod = _aio_dl()
    with mod._image_prefetch_lock:
        mod._image_prefetch_seen.add("7")
        # Don't add to done — simulates "after consume cleared it."

    next_ch = {"chap": "7", "url": "http://example/ch7"}
    target_tdir = os.path.join(temp_main_tmp_dir, "ch_7")

    # Should be a no-op even though done is empty.
    mod._start_image_prefetch(
        next_ch, target_tdir, scraper=None, handler=None,
        image_workers=2, fast_concurrency=8,
    )

    # Done entry still absent (no event was created).
    with mod._image_prefetch_lock:
        assert "7" not in mod._image_prefetch_done


# ────────────────────────────────────────────────────────────────────────
# _consume_image_prefetch: blocks on event; no-op when nothing queued
# ────────────────────────────────────────────────────────────────────────

def test_consume_returns_immediately_when_no_job():
    """No prefetch queued for this chap → done dict has no entry → return
    immediately. Verifies the early-exit branch."""
    mod = _aio_dl()
    start = time.monotonic()
    mod._consume_image_prefetch("never_queued")
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"_consume should be instant for unqueued chap; took {elapsed:.2f}s"


def test_consume_blocks_until_event_set():
    """Event semantics: _consume_image_prefetch waits on the per-chap
    Event. Set the event from another thread and assert _consume returns
    within ~1s (i.e. the wait was unblocked, not timed out at 300s)."""
    mod = _aio_dl()
    chap_label = "test_consume_blocks"
    evt = threading.Event()
    with mod._image_prefetch_lock:
        mod._image_prefetch_done[chap_label] = evt

    # Schedule the set from a background thread after a short delay.
    def _delayed_set():
        time.sleep(0.05)
        evt.set()
    t = threading.Thread(target=_delayed_set, daemon=True)
    t.start()

    start = time.monotonic()
    mod._consume_image_prefetch(chap_label)
    elapsed = time.monotonic() - start

    assert evt.is_set()
    assert 0.04 < elapsed < 1.0, f"Expected ~0.05s wait; got {elapsed:.2f}s"


def test_consume_clears_done_entry_after_wake():
    """After _consume returns, the per-chap Event entry should be cleared
    so it doesn't leak memory across a long run."""
    mod = _aio_dl()
    chap_label = "test_consume_cleanup"
    evt = threading.Event()
    evt.set()  # Already set so _consume returns immediately
    with mod._image_prefetch_lock:
        mod._image_prefetch_done[chap_label] = evt

    mod._consume_image_prefetch(chap_label)

    with mod._image_prefetch_lock:
        assert chap_label not in mod._image_prefetch_done


# ────────────────────────────────────────────────────────────────────────
# Chain dispatch
# ────────────────────────────────────────────────────────────────────────

def test_chain_pushes_up_to_depth_chapters(temp_main_tmp_dir, monkeypatch):
    """_start_image_prefetch_chain should enqueue up to `depth` chapters.
    Mock _start_image_prefetch to record calls without running workers."""
    mod = _aio_dl()
    calls = []

    def _mock_start(next_ch, target_tdir, scraper, handler, iw, fc):
        calls.append(next_ch.get("chap"))

    monkeypatch.setattr(mod, "_start_image_prefetch", _mock_start)

    upcoming = [
        {"chap": "10", "url": "http://example/10"},
        {"chap": "11", "url": "http://example/11"},
        {"chap": "12", "url": "http://example/12"},
        {"chap": "13", "url": "http://example/13"},
    ]
    mod._start_image_prefetch_chain(
        upcoming, temp_main_tmp_dir, scraper=None, handler=None,
        image_workers=2, fast_concurrency=8, depth=3, no_processing=False,
    )

    assert calls == ["10", "11", "12"]


def test_chain_skips_already_cached(temp_main_tmp_dir, monkeypatch):
    """When a chapter's target_tdir already has the success marker
    (.processed_complete for the normal path, .download_complete for
    --no-processing), the chain should skip enqueuing it. Tests the
    cache check at the start of _start_image_prefetch_chain."""
    mod = _aio_dl()
    calls = []

    def _mock_start(next_ch, *_args, **_kwargs):
        calls.append(next_ch.get("chap"))

    monkeypatch.setattr(mod, "_start_image_prefetch", _mock_start)

    # Pre-create the .processed_complete marker for ch_11.
    ch11_tdir = os.path.join(temp_main_tmp_dir, "ch_11")
    os.makedirs(ch11_tdir, exist_ok=True)
    with open(os.path.join(ch11_tdir, ".processed_complete"), "w"):
        pass

    upcoming = [
        {"chap": "10", "url": "http://example/10"},
        {"chap": "11", "url": "http://example/11"},  # already cached
        {"chap": "12", "url": "http://example/12"},
    ]
    mod._start_image_prefetch_chain(
        upcoming, temp_main_tmp_dir, scraper=None, handler=None,
        image_workers=2, fast_concurrency=8, depth=3, no_processing=False,
    )

    # ch_11 is cached → skipped; ch_10 and ch_12 enqueued.
    assert "11" not in calls
    assert "10" in calls
    assert "12" in calls


def test_chain_no_processing_uses_download_complete_marker(temp_main_tmp_dir, monkeypatch):
    """When --no-processing is on, the cache check uses
    .download_complete instead of .processed_complete. Cross-checks the
    marker_name selection in _start_image_prefetch_chain."""
    mod = _aio_dl()
    calls = []

    def _mock_start(next_ch, *_args, **_kwargs):
        calls.append(next_ch.get("chap"))

    monkeypatch.setattr(mod, "_start_image_prefetch", _mock_start)

    # Pre-create .download_complete (not .processed_complete) for ch_5.
    ch5_tdir = os.path.join(temp_main_tmp_dir, "ch_5")
    os.makedirs(ch5_tdir, exist_ok=True)
    with open(os.path.join(ch5_tdir, ".download_complete"), "w"):
        pass

    upcoming = [
        {"chap": "5", "url": "http://example/5"},
        {"chap": "6", "url": "http://example/6"},
    ]
    mod._start_image_prefetch_chain(
        upcoming, temp_main_tmp_dir, scraper=None, handler=None,
        image_workers=2, fast_concurrency=8, depth=2,
        no_processing=True,
    )

    assert "5" not in calls
    assert "6" in calls


def test_chain_returns_early_on_depth_zero(temp_main_tmp_dir, monkeypatch):
    """depth=0 (user-opted-out) → chain enqueues nothing."""
    mod = _aio_dl()
    calls = []

    def _mock_start(*_args, **_kwargs):
        calls.append("called")

    monkeypatch.setattr(mod, "_start_image_prefetch", _mock_start)
    mod._start_image_prefetch_chain(
        [{"chap": "10"}], temp_main_tmp_dir, None, None, 2, 8,
        depth=0, no_processing=False,
    )
    assert calls == []


def test_chain_handles_empty_upcoming_list(temp_main_tmp_dir, monkeypatch):
    """Defensive: empty upcoming list (e.g. last chapter in the run)
    should not crash, just no-op."""
    mod = _aio_dl()
    calls = []
    monkeypatch.setattr(mod, "_start_image_prefetch",
                        lambda *_a, **_kw: calls.append("called"))
    mod._start_image_prefetch_chain(
        [], temp_main_tmp_dir, None, None, 2, 8,
        depth=4, no_processing=False,
    )
    assert calls == []


# ────────────────────────────────────────────────────────────────────────
# Marker contract preserved
# ────────────────────────────────────────────────────────────────────────

def test_write_prefetched_marker_creates_file(temp_main_tmp_dir):
    """_write_prefetched_marker creates the success marker at the
    expected path. Idempotent on existing dirs."""
    mod = _aio_dl()
    target = os.path.join(temp_main_tmp_dir, "ch_42")
    os.makedirs(target)
    mod._write_prefetched_marker(target)
    assert os.path.exists(os.path.join(target, ".download_prefetched"))


def test_write_prefetched_marker_idempotent(temp_main_tmp_dir):
    """Calling _write_prefetched_marker twice should not raise."""
    mod = _aio_dl()
    target = os.path.join(temp_main_tmp_dir, "ch_42")
    os.makedirs(target)
    mod._write_prefetched_marker(target)
    mod._write_prefetched_marker(target)  # Second call should not raise
    assert os.path.exists(os.path.join(target, ".download_prefetched"))


# ────────────────────────────────────────────────────────────────────────
# _start_image_prefetch edge cases
# ────────────────────────────────────────────────────────────────────────

def test_start_with_none_chapter_is_noop(temp_main_tmp_dir):
    """Passing next_chapter=None (last chapter) should bail without
    queueing or touching state."""
    mod = _aio_dl()
    initial_seen = len(mod._image_prefetch_seen)
    mod._start_image_prefetch(
        None, temp_main_tmp_dir, None, None, image_workers=2, fast_concurrency=8,
    )
    assert len(mod._image_prefetch_seen) == initial_seen


def test_start_with_question_mark_chap_is_noop(temp_main_tmp_dir):
    """A chapter dict missing the `chap` key (or set to "?") should bail
    — we can't dedupe or coordinate without a stable label."""
    mod = _aio_dl()
    initial_seen = len(mod._image_prefetch_seen)
    mod._start_image_prefetch(
        {"chap": "?"}, temp_main_tmp_dir, None, None,
        image_workers=2, fast_concurrency=8,
    )
    assert len(mod._image_prefetch_seen) == initial_seen


# ────────────────────────────────────────────────────────────────────────
# Worker pool spawning
# ────────────────────────────────────────────────────────────────────────

def test_ensure_workers_spawns_target_count():
    """_ensure_image_prefetch_workers should spawn up to
    _image_prefetch_parallel daemon threads. Already-alive workers count."""
    mod = _aio_dl()
    # Set parallel count for this test.
    saved = mod._image_prefetch_parallel
    try:
        mod._image_prefetch_parallel = 3
        # Workers may be alive from prior tests; clear to force respawn.
        with mod._image_prefetch_lock:
            # Don't kill existing workers (they're daemons); just track them.
            initial_alive = sum(1 for t in mod._image_prefetch_workers if t.is_alive())
        mod._ensure_image_prefetch_workers()
        alive_after = sum(1 for t in mod._image_prefetch_workers if t.is_alive())
        assert alive_after >= 3, f"Expected >=3 alive workers, got {alive_after}"
    finally:
        mod._image_prefetch_parallel = saved
