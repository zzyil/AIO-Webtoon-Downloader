"""Coverage for aio_android's resource-limit resolver.

Port of UI-source/electron/resource-limits.js. There are now THREE copies of
the preset tables — that module (the source of truth), its renderer mirror at
UI-source/src/lib/resourceLimits.js, and the Python one here — so the last test
in this file is a real drift guard: it parses both JS files and asserts the
numbers match. Without it, a tuning change on the desktop would silently leave
Android throttling at the old values.

Semantics under test: HARD OVERRIDE. A level other than "unlimited" REPLACES
the manual concurrency knobs; it is not a ceiling and not a min().
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import json

from aio_android import (
    _CPU_PRESETS,
    _MOBILE_SEARCH_PARALLELISM,
    _NET_DEFAULTS,
    _NETWORK_PRESETS,
    apply_network_limit,
    build_argv,
    cpu_percent_for_level,
    effective_limits,
    effective_limits_json,
    is_network_limited,
    resume_throttle_flags,
    resume_throttle_flags_json,
    search_parallelism_for_level,
)

_REPO = Path(__file__).resolve().parent.parent
_JS_MAIN = _REPO / "UI-source" / "electron" / "resource-limits.js"
_JS_MIRROR = _REPO / "UI-source" / "src" / "lib" / "resourceLimits.js"


def _pairs(argv):
    out = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            out[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


# --------------------------------------------------------------------------
# Level normalization — must fail OPEN
# --------------------------------------------------------------------------

@pytest.mark.parametrize("level", ["unlimited", None, "", "nonsense", 5, True, "UNLIMITED"])
def test_unknown_levels_read_as_unlimited(level):
    # Failing open matters: a corrupt or future-versioned settings value must
    # never silently pin a user to 2 connections with no visible cause.
    assert is_network_limited(level) is False
    assert cpu_percent_for_level(level) == 100
    assert apply_network_limit({"imageConcurrency": 9}, level) == {"imageConcurrency": 9}


@pytest.mark.parametrize("level", ["low", "LOW", "Low", "lOw"])
def test_levels_are_case_insensitive(level):
    assert is_network_limited(level) is True
    assert apply_network_limit({}, level)["imageConcurrency"] == 2


# --------------------------------------------------------------------------
# Hard override
# --------------------------------------------------------------------------

@pytest.mark.parametrize("level,expected", [
    ("high", {"imageConcurrency": 6, "imageWorkers": 3, "imagePrefetchParallel": 2, "imagePrefetchDepth": 2}),
    ("balanced", {"imageConcurrency": 4, "imageWorkers": 2, "imagePrefetchParallel": 1, "imagePrefetchDepth": 1}),
    ("low", {"imageConcurrency": 2, "imageWorkers": 1, "imagePrefetchParallel": 1, "imagePrefetchDepth": 1}),
])
def test_preset_replaces_manual_knobs(level, expected):
    # Manual values are deliberately HIGHER than every preset, so a min()/ceiling
    # implementation would pass a plain "is it capped" check. Only a true
    # override produces the preset numbers exactly.
    manual = {"imageConcurrency": 16, "imageWorkers": 9, "imagePrefetchParallel": 8, "imagePrefetchDepth": 7}
    assert {k: apply_network_limit(manual, level)[k] for k in expected} == expected


def test_preset_overrides_upward_too():
    # The other direction: manual values BELOW the preset must also be replaced.
    got = apply_network_limit({"imageConcurrency": 1, "imageWorkers": 1}, "high")
    assert got["imageConcurrency"] == 6 and got["imageWorkers"] == 3


def test_apply_network_limit_does_not_mutate_input():
    original = {"imageConcurrency": 16, "other": "kept"}
    result = apply_network_limit(original, "low")
    assert original == {"imageConcurrency": 16, "other": "kept"}
    assert result["imageConcurrency"] == 2 and result["other"] == "kept"


def test_unlimited_returns_the_same_object():
    # Documented contract: callers may pass the result straight through.
    settings = {"imageConcurrency": 16}
    assert apply_network_limit(settings, "unlimited") is settings


def test_search_parallelism_follows_the_network_level():
    assert search_parallelism_for_level(12, "unlimited") == 12
    assert search_parallelism_for_level(None, "unlimited") is None
    assert search_parallelism_for_level(12, "high") == 5
    assert search_parallelism_for_level(12, "balanced") == 3
    assert search_parallelism_for_level(12, "low") == 2


# --------------------------------------------------------------------------
# Integration with build_argv (Android's spawn chokepoint)
# --------------------------------------------------------------------------

def test_unlimited_run_emits_no_resource_flags():
    # An unlimited run must produce the command line it always did — no stray
    # --max-cpu-percent 100, which would look like an intentional throttle.
    assert build_argv({"format": "cbz", "networkLimit": "unlimited", "cpuLimit": "unlimited"}) == [
        "--format", "cbz",
    ]


def test_network_level_rewrites_the_four_knobs_in_argv():
    argv = build_argv({"imageConcurrency": 16, "imageWorkers": 9, "networkLimit": "balanced"})
    assert _pairs(argv) == {
        "--image-concurrency": "4",
        "--image-workers": "2",
        "--image-prefetch-parallel": "1",
        "--image-prefetch-depth": "1",
    }


@pytest.mark.parametrize("level,percent", [("high", "75"), ("balanced", "50"), ("low", "25")])
def test_cpu_level_emits_max_cpu_percent(level, percent):
    assert _pairs(build_argv({"cpuLimit": level}))["--max-cpu-percent"] == percent


def test_build_argv_does_not_mutate_caller_settings():
    settings = {"imageConcurrency": 16, "cpuLimit": "low", "networkLimit": "low"}
    build_argv(settings)
    assert settings == {"imageConcurrency": 16, "cpuLimit": "low", "networkLimit": "low"}


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------

def test_resume_flags_always_emit_every_knob():
    # ALL five, ALWAYS — that is what makes "current wins" hold in both
    # directions. An omitted flag lets run_params.json's persisted value stand.
    flags = resume_throttle_flags({})
    assert _pairs(flags) == {
        "--image-concurrency": "8",
        "--image-workers": "3",
        "--image-prefetch-parallel": "2",
        "--image-prefetch-depth": "2",
        "--max-cpu-percent": "100",
    }


def test_resume_flags_prefer_the_users_manual_values_when_unlimited():
    flags = resume_throttle_flags({"imageConcurrency": 12, "imageWorkers": 5})
    assert _pairs(flags)["--image-concurrency"] == "12"
    assert _pairs(flags)["--image-workers"] == "5"
    # Untouched knobs still fall back to the Python defaults, never to nothing.
    assert _pairs(flags)["--image-prefetch-depth"] == "2"


def test_resume_flags_reflect_the_current_level_not_the_persisted_one():
    flags = resume_throttle_flags({"imageConcurrency": 12, "networkLimit": "low", "cpuLimit": "balanced"})
    assert _pairs(flags)["--image-concurrency"] == "2"
    assert _pairs(flags)["--max-cpu-percent"] == "50"


def test_resume_flags_json_round_trip():
    import json

    assert json.loads(resume_throttle_flags_json("{}")) == resume_throttle_flags({})
    with pytest.raises(ValueError):
        resume_throttle_flags_json("not json")


# --------------------------------------------------------------------------
# Drift guard — the Python tables vs BOTH JavaScript copies
# --------------------------------------------------------------------------

def _js_object_body(source: str, name: str) -> str:
    """Text between the braces of `<name> = {...}` / `= Object.freeze({...})`.

    Brace-matched rather than regexed to the first `}` — NETWORK_PRESETS nests
    one level, and a lazy match would stop inside `high: { ... }`.
    """
    match = re.search(rf"\b{name}\s*=\s*(?:Object\.freeze\()?\s*\{{", source)
    assert match, f"{name} not found"
    depth, start = 1, match.end()
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i]
    raise AssertionError(f"unbalanced braces in {name}")


def _js_flat(source: str, name: str) -> dict:
    return {k: int(v) for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", _js_object_body(source, name))}


def _js_nested(source: str, name: str) -> dict:
    body = _js_object_body(source, name)
    return {
        level: {k: int(v) for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", inner)}
        for level, inner in re.findall(r"(\w+)\s*:\s*\{([^}]*)\}", body)
    }


@pytest.mark.parametrize("js_path", [_JS_MAIN, _JS_MIRROR], ids=["electron", "renderer"])
def test_preset_tables_match_the_javascript(js_path):
    """The tables are triplicated; this is what keeps them equal.

    If this fails, the JavaScript is the source of truth — update the Python
    tables in aio_android.py to match, not the other way round.
    """
    source = js_path.read_text(encoding="utf-8")
    assert _js_nested(source, "NETWORK_PRESETS") == _NETWORK_PRESETS
    assert _js_flat(source, "CPU_PRESETS") == _CPU_PRESETS
    assert _js_flat(source, "NET_DEFAULTS") == _NET_DEFAULTS


# --------------------------------------------------------------------------
# effective_limits — the D9 display lie
#
# apply_network_limit is a HARD OVERRIDE, and until this existed nothing could
# ask what it had overridden. The Download screen kept rendering the user's
# typed imageWorkers while every run used the preset's. These tests pin the
# property the UI needs: BOTH numbers, always, so it can show the real one and
# still restore the typed one when the level goes back to unlimited.
# --------------------------------------------------------------------------

def test_unlimited_reports_the_stored_values_unchanged():
    limits = effective_limits({"imageWorkers": 9, "imageConcurrency": 12})
    assert limits["networkManaged"] is False
    assert limits["knobs"]["imageWorkers"] == {"effective": 9, "stored": 9}
    assert limits["knobs"]["imageConcurrency"] == {"effective": 12, "stored": 12}
    assert limits["networkPreview"] is None
    assert limits["cpuPreview"] is None
    assert limits["maxCpuPercent"] == 100


def test_a_preset_overrides_the_effective_value_but_preserves_the_stored_one():
    """The exact shape of D9: 9 is what the user typed, 1 is what will run, and
    the UI has to be able to say both."""
    limits = effective_limits({"networkLimit": "low", "imageWorkers": 9})
    assert limits["networkManaged"] is True
    assert limits["knobs"]["imageWorkers"]["effective"] == 1
    assert limits["knobs"]["imageWorkers"]["stored"] == 9


def test_effective_values_equal_what_build_argv_actually_emits():
    """The report and the command line must not be able to disagree — that
    disagreement IS the defect. Checked against the real emitter rather than
    against the preset table, so a change in either is caught."""
    settings = {"networkLimit": "balanced", "imageWorkers": 9, "imageConcurrency": 12}
    limits = effective_limits(settings)
    emitted = _pairs(build_argv(dict(settings)))
    for knob, flag in (
        ("imageConcurrency", "--image-concurrency"),
        ("imageWorkers", "--image-workers"),
        ("imagePrefetchDepth", "--image-prefetch-depth"),
        ("imagePrefetchParallel", "--image-prefetch-parallel"),
    ):
        assert emitted[flag] == str(limits["knobs"][knob]["effective"]), knob


def test_an_unset_knob_reports_the_python_default_not_none():
    """A UI cannot render None in a number field. An untouched knob still has a
    concrete value, and it is aio-dl.py's argparse default."""
    limits = effective_limits({})
    for knob, default in _NET_DEFAULTS.items():
        if knob == "searchParallelism":
            continue
        assert limits["knobs"][knob]["stored"] == default


def test_search_parallelism_reports_the_mobile_baseline_not_the_desktop_one():
    """build_search_argv defaults to 4 on Android where aio-dl.py's argparse
    defaults to 6. Reporting 6 would be a second display lie dressed up as a
    fix for the first."""
    assert _NET_DEFAULTS["searchParallelism"] == 6
    limits = effective_limits({})
    assert limits["knobs"]["searchParallelism"]["stored"] == _MOBILE_SEARCH_PARALLELISM
    assert limits["knobs"]["searchParallelism"]["effective"] == _MOBILE_SEARCH_PARALLELISM


def test_search_parallelism_effective_matches_the_search_argv():
    from aio_android import build_search_argv

    settings = {"networkLimit": "low"}
    limits = effective_limits(dict(settings))
    argv = build_search_argv("x", dict(settings))
    emitted = argv[argv.index("--search-parallelism") + 1]
    assert emitted == str(limits["knobs"]["searchParallelism"]["effective"])


def test_cpu_level_is_reported_independently_of_the_network_level():
    limits = effective_limits({"cpuLimit": "high"})
    assert limits["networkManaged"] is False
    assert limits["maxCpuPercent"] == 75
    assert limits["cpuPreview"] == "~75% of CPU cores"
    assert limits["cpuLimitLabel"] == "High"


def test_preview_strings_mirror_the_renderer_reference():
    """Same text as networkPreviewText / cpuPreviewText in
    UI-source/src/lib/resourceLimits.js, so the two apps describe a preset the
    same way."""
    limits = effective_limits({"networkLimit": "low", "cpuLimit": "low"})
    assert limits["networkPreview"] == "curl_cffi 2 · workers 1 · prefetch 1×1 · search 2"
    assert limits["cpuPreview"] == "~25% of CPU cores"


def test_unknown_levels_fail_open_here_too():
    limits = effective_limits({"networkLimit": "nonsense", "cpuLimit": 7})
    assert limits["networkLimit"] == "unlimited"
    assert limits["cpuLimit"] == "unlimited"
    assert limits["networkManaged"] is False


def test_effective_limits_json_never_raises_on_a_bad_blob():
    """A settings screen that cannot ask this question would have to guess, and
    guessing is the defect it exists to close."""
    for bad in ("", "not json", "[1,2]", "null"):
        payload = json.loads(effective_limits_json(bad))
        assert payload["networkLimit"] == "unlimited"
        assert payload["networkManaged"] is False


def test_effective_limits_json_round_trips():
    payload = json.loads(effective_limits_json(json.dumps({"networkLimit": "high"})))
    assert payload == effective_limits({"networkLimit": "high"})
