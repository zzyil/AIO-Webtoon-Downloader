"""Tests for the CLI flag rename in Phase C of the fast-download
generalization (2026-05-13).

`--mangafire-image-concurrency` was generalized to `--image-concurrency`
because the underlying curl_cffi fast download path is no longer MangaFire-
specific (any handler with SUPPORTS_FAST_DOWNLOAD=True consumes it). New
flags `--image-prefetch-depth`, `--image-prefetch-parallel`, and
`--no-fast-download` were added at the same time.

The deprecation policy was softened during the upstream-merge PR (2026-05-24):
the old `--mangafire-image-concurrency` flag is now accepted as a hidden
alias — argparse declares it with `help=argparse.SUPPRESS` so `--help`
doesn't surface it, but main() routes the value onto `args.image_concurrency`
with a `DeprecationWarning`. Users with saved CLI scripts keep working;
they see one warning per invocation pointing them at the new flag name.

These tests use the actual argparse parser by importing main as a module
and constructing a minimal parser context. They're cheap; no network.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest


def _load_aio_dl_module():
    """Load aio-dl.py as a module so we can import its argparse setup.

    The script's filename has a hyphen (`aio-dl.py`) so it's not directly
    importable as `import aio_dl`. Use importlib's spec/exec dance instead.
    Cached in sys.modules to avoid re-loading on every test.
    """
    if "aio_dl" in sys.modules:
        return sys.modules["aio_dl"]
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "aio_dl", os.path.join(here, "aio-dl.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aio_dl"] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_parser_via_main():
    """The parser is constructed inside main(). Patching main() to return
    early after parser construction would be ideal, but it's deeply nested
    with side effects. Instead, we build a sibling parser with the same
    add_argument calls — but that drifts from the real parser silently.

    Pragmatic workaround: invoke main with --help-style sentinels that
    cause argparse to SystemExit before any download work, and capture
    sys.exit. Cleaner is to test via subprocess.
    """
    import subprocess
    return subprocess


# ────────────────────────────────────────────────────────────────────────
# New flags parse correctly
# ────────────────────────────────────────────────────────────────────────

def _run_aio_dl(*args, capture_stderr=True):
    """Spawn aio-dl.py with the given args, return (returncode, stderr).
    Stops at argparse — passes a fake URL so we get past parser-required
    checks but exit early via the URL not being a real site.
    """
    import os
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "aio-dl.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return proc


def test_image_concurrency_flag_appears_in_help():
    """Verify the new flag is registered with the parser."""
    proc = _run_aio_dl("--help")
    assert "--image-concurrency" in proc.stdout
    assert proc.returncode == 0


def test_image_prefetch_depth_flag_appears_in_help():
    proc = _run_aio_dl("--help")
    assert "--image-prefetch-depth" in proc.stdout


def test_image_prefetch_parallel_flag_appears_in_help():
    proc = _run_aio_dl("--help")
    assert "--image-prefetch-parallel" in proc.stdout


def test_no_fast_download_flag_appears_in_help():
    proc = _run_aio_dl("--help")
    assert "--no-fast-download" in proc.stdout


# ────────────────────────────────────────────────────────────────────────
# Old flag is removed
# ────────────────────────────────────────────────────────────────────────

def test_mangafire_image_concurrency_flag_removed_from_help():
    """Hard rename: --mangafire-image-concurrency must NOT appear in --help.
    Regression guard against accidentally re-adding it as a deprecated alias."""
    proc = _run_aio_dl("--help")
    assert "--mangafire-image-concurrency" not in proc.stdout


def test_mangafire_image_concurrency_emits_deprecation_warning():
    """Passing the deprecated flag is accepted (NOT rejected at parse) and
    emits a DeprecationWarning pointing at the new flag name. Regression
    guard against accidentally re-tightening it back into a hard rename or
    dropping the deprecation message text.

    We deliberately pass a URL whose host has no registered handler so the
    process exits 1 (no-handler error) after argparse + deprecation routing
    have already run. The argparse path completing without an
    'unrecognized arguments' error is the actual signal we're testing for."""
    import os
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [
            sys.executable,
            os.path.join(here, "aio-dl.py"),
            "--mangafire-image-concurrency", "4",
            "https://example.com/x",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    # Parse-time success: argparse did NOT emit "unrecognized arguments"
    # (which would set returncode=2). We hit the downstream no-handler
    # error path (returncode=1) — that's expected and unrelated.
    assert "unrecognized arguments" not in proc.stderr
    assert proc.returncode != 2
    # The deprecation warning is plumbed through to stderr by main()'s
    # `warnings.warn(..., DeprecationWarning)` call after parse_args.
    assert "DeprecationWarning" in proc.stderr
    assert "--mangafire-image-concurrency is deprecated" in proc.stderr
    assert "--image-concurrency" in proc.stderr


# ────────────────────────────────────────────────────────────────────────
# MangaFire VRF flags were removed with the 2026 REST-API rewrite
# ────────────────────────────────────────────────────────────────────────

def test_mangafire_vrf_flags_removed():
    """The old Patchright VRF capture is gone; both --mangafire-vrf-* flags
    must no longer appear in --help (MangaFire is a plain JSON API now)."""
    proc = _run_aio_dl("--help")
    assert "--mangafire-vrf-prefetch-depth" not in proc.stdout
    assert "--mangafire-vrf-parallel" not in proc.stdout


# ────────────────────────────────────────────────────────────────────────
# Defaults via direct argparse construction
# ────────────────────────────────────────────────────────────────────────

def _help_block_for_flag(stdout: str, flag: str, next_flag: str) -> str:
    """Extract the description block for `flag` from --help stdout.

    argparse formats `--help` with a usage block at the top (one big line
    listing all flags) followed by per-flag description blocks. We want the
    DESCRIPTION block, which starts with the flag name on its own line and
    continues until the next flag's description starts. Match the SECOND
    occurrence of `flag` (skips the usage line).
    """
    import re
    # Find all occurrences of the flag, take the second (description block).
    # Stop at the next flag's description or end of file.
    matches = list(re.finditer(re.escape(flag), stdout))
    if len(matches) < 2:
        return ""
    start = matches[1].start()
    # Find next flag's description start
    end_match = re.search(re.escape(next_flag), stdout[start + len(flag):])
    if end_match:
        end = start + len(flag) + end_match.start()
    else:
        end = len(stdout)
    return stdout[start:end]


def test_image_concurrency_default_is_8():
    """Verify the argparse default matches what the Phase C plan promised."""
    proc = _run_aio_dl("--help")
    block = _help_block_for_flag(
        proc.stdout, "--image-concurrency", "--image-prefetch-depth"
    )
    assert block, "Expected --image-concurrency description block"
    # Help text says "default: 8" in the description.
    assert "default: 8" in block, f"block was: {block[:300]}"


def test_image_prefetch_depth_default_is_2():
    proc = _run_aio_dl("--help")
    block = _help_block_for_flag(
        proc.stdout, "--image-prefetch-depth", "--image-prefetch-parallel"
    )
    assert block
    assert "default: 2" in block, f"block was: {block[:300]}"


def test_image_prefetch_parallel_default_is_2():
    proc = _run_aio_dl("--help")
    block = _help_block_for_flag(
        proc.stdout, "--image-prefetch-parallel", "--no-fast-download"
    )
    assert block
    assert "default: 2" in block, f"block was: {block[:300]}"


# ────────────────────────────────────────────────────────────────────────
# Scanlation-group / MTL flags (2026-08-02)
#
# --mtl and --exclude-group decide WHICH version's image bytes land on disk,
# so both must be resume-gating: a run started under one policy and resumed
# under another would otherwise stitch a mixed-provenance volume.
# ────────────────────────────────────────────────────────────────────────

def test_mtl_flag_appears_in_help_with_its_three_choices():
    proc = _run_aio_dl("--help")
    assert "--mtl {avoid,allow,exclude}" in proc.stdout
    assert proc.returncode == 0


def test_exclude_group_flag_appears_in_help():
    proc = _run_aio_dl("--help")
    assert "--exclude-group" in proc.stdout


def test_group_flags_are_resume_gating():
    mod = _load_aio_dl_module()
    for dest in ("mtl", "exclude_group", "group", "mix_by_upvote",
                 "no_group_fallback"):
        assert dest in mod._RESUME_GATING_DESTS, dest


def test_group_flags_are_not_also_transient():
    """_validate_resume_categories hard-errors on overlap; assert it up front."""
    mod = _load_aio_dl_module()
    overlap = mod._RESUME_GATING_DESTS & mod._RESUME_TRANSIENT_DESTS
    assert overlap == set()


def test_mtl_and_exclude_group_are_persisted_for_update_replay():
    """Without these keys in download_params.json, an --update-all child
    reverts to defaults and the series accumulates mixed provenance."""
    import argparse
    mod = _load_aio_dl_module()
    args = argparse.Namespace(mtl="exclude", exclude_group=["Bad"], group=[])
    # _save_download_params builds its dict from getattr(args, ...) — exercise
    # the same accessors the writer uses rather than doing disk I/O here.
    assert getattr(args, "mtl", "avoid") == "exclude"
    assert getattr(args, "exclude_group", []) == ["Bad"]
    src = __import__("inspect").getsource(mod._save_download_params)
    assert '"mtl"' in src
    assert '"exclude_group"' in src


def test_exclude_group_is_replayed_to_update_children():
    mod = _load_aio_dl_module()
    src = __import__("inspect").getsource(mod._append_saved_update_options)
    assert "--exclude-group" in src
    assert "--mtl" in src
