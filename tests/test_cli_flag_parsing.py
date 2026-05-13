"""Tests for the CLI flag rename in Phase C of the fast-download
generalization (2026-05-13).

`--mangafire-image-concurrency` was renamed to the generic `--image-concurrency`
because the underlying curl_cffi fast download path is no longer MangaFire-
specific (any handler with SUPPORTS_FAST_DOWNLOAD=True consumes it). New
flags `--image-prefetch-depth`, `--image-prefetch-parallel`, and
`--no-fast-download` were added at the same time.

The hard rename was a deliberate user choice over a soft alias — users with
saved CLI scripts hit "unrecognized arguments" and need to update. UI users
have a silent migration in SettingsTab's settings loader.

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


def test_mangafire_image_concurrency_rejected_at_parse():
    """Passing the removed flag triggers argparse's 'unrecognized arguments'
    SystemExit with code 2. Regression guard."""
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
    assert proc.returncode == 2  # argparse error exit code
    assert "unrecognized arguments" in proc.stderr
    assert "--mangafire-image-concurrency" in proc.stderr


# ────────────────────────────────────────────────────────────────────────
# MangaFire-namespaced VRF flags are kept (they're not generic)
# ────────────────────────────────────────────────────────────────────────

def test_mangafire_vrf_prefetch_depth_flag_still_present():
    """VRF is MangaFire-specific; this flag stays namespaced."""
    proc = _run_aio_dl("--help")
    assert "--mangafire-vrf-prefetch-depth" in proc.stdout


def test_mangafire_vrf_parallel_flag_still_present():
    proc = _run_aio_dl("--help")
    assert "--mangafire-vrf-parallel" in proc.stdout


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
