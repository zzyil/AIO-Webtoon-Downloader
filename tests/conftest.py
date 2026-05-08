"""Shared pytest fixtures + sys.path bootstrap.

Adds the project root to sys.path so `from sites.chapter_merger import ...`
works when running pytest from the project root. We can't rely on the
package being installed via pip — the project ships as a single-file
script (`aio-dl.py`) plus the `sites/` package in-place.
"""

from __future__ import annotations

import os
import sys

# Project root = parent of this tests/ directory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
