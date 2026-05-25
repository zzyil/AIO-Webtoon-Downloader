"""Tests for the automatic CDN-error backoff added in Phase D of the
fast-download generalization (2026-05-13).

When a CDN starts failing during a download (rate_limit / retryable
classes), `_record_failure` calls `_record_host_failure_for_backoff`
which reduces `_HOST_CONCURRENCY_CAP[host]`. Subsequent fetches against
that host use `_effective_concurrency` to respect the cap.

Per-run scope: caps are cleared by `_reset_host_concurrency_caps()` at
the start of each run via `_apply_runtime_tunables`. No auto-recovery
within a run — once dialed down, stays dialed down until the next run
or until the user passes a lower concurrency explicitly.

Cross-process backoff (sibling worker processes via `_COORD`) is handled
separately by `_record_rate_limit`; this module's tests don't cover that.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest


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


@pytest.fixture(autouse=True)
def _reset_caps():
    """Reset _HOST_CONCURRENCY_CAP between tests so one test's failures
    don't bleed into the next. _reset_host_concurrency_caps() is the
    same function called by _apply_runtime_tunables at run start."""
    mod = _aio_dl()
    mod._reset_host_concurrency_caps()
    yield
    mod._reset_host_concurrency_caps()


# ────────────────────────────────────────────────────────────────────────
# _record_host_failure_for_backoff: class-based behavior
# ────────────────────────────────────────────────────────────────────────

def test_rate_limit_halves_cap_from_default_8():
    """First rate_limit failure: 8 // 2 = 4. Verifies the default-base
    assumption built into _record_host_failure_for_backoff."""
    mod = _aio_dl()
    mod._record_host_failure_for_backoff("cdn.example", "rate_limit")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 4


def test_rate_limit_floors_at_one():
    """When the cap is already 2, halving gives 1 (not 0 — max(1, 2//2) = 1).
    Subsequent rate_limit failures should keep it at 1, not go to 0."""
    mod = _aio_dl()
    with mod._HOST_CAP_LOCK:
        mod._HOST_CONCURRENCY_CAP["cdn.example"] = 2
    mod._record_host_failure_for_backoff("cdn.example", "rate_limit")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 1
    # Another rate_limit at cap=1 should keep it at 1 (max(1, 1//2) = 1).
    mod._record_host_failure_for_backoff("cdn.example", "rate_limit")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 1


def test_retryable_decrements_by_one():
    """First retryable failure on a fresh host: 8 - 1 = 7. Light decrement."""
    mod = _aio_dl()
    mod._record_host_failure_for_backoff("cdn.example", "retryable")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 7


def test_retryable_floors_at_one():
    """retryable with cap already at 1 keeps it at 1 (max(1, 1-1) = 1)."""
    mod = _aio_dl()
    with mod._HOST_CAP_LOCK:
        mod._HOST_CONCURRENCY_CAP["cdn.example"] = 1
    mod._record_host_failure_for_backoff("cdn.example", "retryable")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 1


def test_origin_error_is_noop():
    """CF 520-527 = upstream sickness; concurrency reduction doesn't help
    and may slow recovery. The function should leave the cap untouched."""
    mod = _aio_dl()
    mod._record_host_failure_for_backoff("cdn.example", "origin_error")
    with mod._HOST_CAP_LOCK:
        assert "cdn.example" not in mod._HOST_CONCURRENCY_CAP


def test_permanent_is_noop():
    """4xx = caller's problem, not throttle. Already filtered by
    _record_failure before reaching here, but the function still no-ops
    defensively as a regression guard."""
    mod = _aio_dl()
    mod._record_host_failure_for_backoff("cdn.example", "permanent")
    with mod._HOST_CAP_LOCK:
        assert "cdn.example" not in mod._HOST_CONCURRENCY_CAP


def test_empty_host_is_noop():
    """No host context (rare — usually means a malformed URL parse).
    Function should bail without writing to the cap dict."""
    mod = _aio_dl()
    mod._record_host_failure_for_backoff("", "rate_limit")
    with mod._HOST_CAP_LOCK:
        # Cap dict should be empty AND not contain an "" key.
        assert "" not in mod._HOST_CONCURRENCY_CAP
        assert len(mod._HOST_CONCURRENCY_CAP) == 0


def test_repeat_rate_limit_continues_dialing_down():
    """Three sequential rate_limit failures starting from default base=8:
    cap should go 8 → 4 → 2 → 1 (halving each step, floored at 1).
    This is the real CDN-getting-angrier scenario."""
    mod = _aio_dl()
    for expected in [4, 2, 1, 1]:
        mod._record_host_failure_for_backoff("cdn.example", "rate_limit")
        with mod._HOST_CAP_LOCK:
            assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == expected


def test_mixed_rate_limit_and_retryable_continue_dialing_down():
    """rate_limit halves; retryable decrements by 1. Mixed sequence:
    8 (default) -> 4 (rate_limit) -> 3 (retryable) -> 1 (rate_limit, 3//2)."""
    mod = _aio_dl()
    mod._record_host_failure_for_backoff("cdn.example", "rate_limit")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 4
    mod._record_host_failure_for_backoff("cdn.example", "retryable")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 3
    mod._record_host_failure_for_backoff("cdn.example", "rate_limit")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 1


# ────────────────────────────────────────────────────────────────────────
# _effective_concurrency: caps vs base
# ────────────────────────────────────────────────────────────────────────

def test_effective_concurrency_no_cap_returns_base():
    """Healthy CDN (no cap entry) → return base unchanged. Zero overhead."""
    mod = _aio_dl()
    assert mod._effective_concurrency("never.failed", 8) == 8


def test_effective_concurrency_caps_to_min():
    """When cap is set, _effective_concurrency returns min(base, cap)."""
    mod = _aio_dl()
    with mod._HOST_CAP_LOCK:
        mod._HOST_CONCURRENCY_CAP["cdn.example"] = 3
    assert mod._effective_concurrency("cdn.example", 8) == 3


def test_effective_concurrency_base_lower_than_cap():
    """User-set base lower than the cap → base wins. Don't promote
    concurrency above what the user asked for, even if the CDN was OK."""
    mod = _aio_dl()
    with mod._HOST_CAP_LOCK:
        mod._HOST_CONCURRENCY_CAP["cdn.example"] = 8
    assert mod._effective_concurrency("cdn.example", 3) == 3


def test_effective_concurrency_empty_host_returns_base():
    """No host context → return base. Defensive; shouldn't happen in
    practice since urlparse usually produces a netloc."""
    mod = _aio_dl()
    assert mod._effective_concurrency("", 8) == 8


# ────────────────────────────────────────────────────────────────────────
# Reset semantics
# ────────────────────────────────────────────────────────────────────────

def test_reset_clears_all_caps():
    """_reset_host_concurrency_caps wipes the dict. Called at run start
    so each run begins with fresh CDN trust."""
    mod = _aio_dl()
    with mod._HOST_CAP_LOCK:
        mod._HOST_CONCURRENCY_CAP["a"] = 3
        mod._HOST_CONCURRENCY_CAP["b"] = 5
    mod._reset_host_concurrency_caps()
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP == {}


# ────────────────────────────────────────────────────────────────────────
# Integration with _record_failure
# ────────────────────────────────────────────────────────────────────────

def test_record_failure_triggers_backoff():
    """The full chain: _record_failure(host, url, "rate_limit") should
    update both _HOST_FAIL_COUNT (existing host-poison machinery) AND
    _HOST_CONCURRENCY_CAP (new backoff). Verifies the wiring."""
    mod = _aio_dl()
    # Reset both pieces of state for a clean baseline.
    with mod._HOST_FAIL_LOCK:
        mod._HOST_FAIL_COUNT.clear()
        mod._HOST_FAIL_URLS.clear()
    mod._reset_host_concurrency_caps()

    mod._record_failure("cdn.example", "https://cdn.example/page1.jpg", "rate_limit")

    # Host-poison counter: 1 failed URL.
    assert mod._host_fail_count("cdn.example") == 1
    # Concurrency cap: dialed from default 8 to 4.
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn.example"] == 4


def test_record_failure_permanent_does_not_touch_cap():
    """_record_failure already filters permanent (4xx). Even when called
    with cls=permanent, the cap should stay clean. Regression guard
    against accidentally widening the trigger surface."""
    mod = _aio_dl()
    mod._record_failure("cdn.example", "https://cdn.example/page1.jpg", "permanent")
    with mod._HOST_CAP_LOCK:
        assert "cdn.example" not in mod._HOST_CONCURRENCY_CAP


def test_record_failure_origin_error_does_not_touch_cap():
    """CF 520-527 origin_error: should hit _HOST_FAIL_COUNT (for the
    chapter-abort threshold) but NOT _HOST_CONCURRENCY_CAP."""
    mod = _aio_dl()
    with mod._HOST_FAIL_LOCK:
        mod._HOST_FAIL_COUNT.clear()
        mod._HOST_FAIL_URLS.clear()
    mod._reset_host_concurrency_caps()
    mod._record_failure("cdn.example", "https://cdn.example/page1.jpg", "origin_error")
    # Host-poison counter incremented (existing behavior).
    assert mod._host_fail_count("cdn.example") == 1
    # But the concurrency cap stays clean.
    with mod._HOST_CAP_LOCK:
        assert "cdn.example" not in mod._HOST_CONCURRENCY_CAP


# ────────────────────────────────────────────────────────────────────────
# Per-host isolation
# ────────────────────────────────────────────────────────────────────────

def test_caps_are_per_host_isolated():
    """A failure on cdn-a.example should not affect cdn-b.example's cap.
    Verifies the dict is keyed by host, not global."""
    mod = _aio_dl()
    mod._record_host_failure_for_backoff("cdn-a.example", "rate_limit")
    with mod._HOST_CAP_LOCK:
        assert mod._HOST_CONCURRENCY_CAP["cdn-a.example"] == 4
        assert "cdn-b.example" not in mod._HOST_CONCURRENCY_CAP
    # cdn-b still gets the base concurrency unchanged.
    assert mod._effective_concurrency("cdn-b.example", 8) == 8
