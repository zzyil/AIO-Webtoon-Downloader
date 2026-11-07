"""Shared pytest fixtures + sys.path bootstrap.

Adds the project root to sys.path so `from sites.chapter_merger import ...`
works when running pytest from the project root. We can't rely on the
package being installed via pip — the project ships as a single-file
script (`aio-dl.py`) plus the `sites/` package in-place.
"""

from __future__ import annotations

import os
import sys

import pytest

# Project root = parent of this tests/ directory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# No test may launch a browser. mangafire.to signs every /api/* call with a
# `vrf=` token minted by driving Chromium (sites/mangafire_vrf.py), so ANY test
# that calls a MangaFire handler method would otherwise boot a real browser and
# hit the network — silently passing on a dev machine and failing in CI, where
# no browser binary is installed. Tests that need signing to succeed must stub
# `mangafire_vrf.sign_api_query` (see tests/test_mangafire_vrf.py's fake_signer
# fixture and the MangaFire cases in tests/test_komikku_metadata.py).
os.environ["AIO_MANGAFIRE_NO_SIGNER"] = "1"


@pytest.fixture(autouse=True)
def _no_interactive_solve_leak():
    """Every test starts with the Cloudflare-solve permission off, as a fresh
    desktop process does. aio-dl.py main() grants it for the rest of its context
    with no undo (sites/crawlee_utils.allow_interactive_solving_for_this_run),
    which is safe in the shipped hosts (one process per download; Android scopes
    every main() call) but leaked across tests here: after any in-process
    main() test, tests/test_mangafire_vrf.py's background-op and permission
    tests read the grant and failed only in a full-suite run. reset() restores
    the value from before the test, whatever the test set after it."""
    from sites.crawlee_utils import _INTERACTIVE_SOLVE
    token = _INTERACTIVE_SOLVE.set(False)
    try:
        yield
    finally:
        _INTERACTIVE_SOLVE.reset(token)
