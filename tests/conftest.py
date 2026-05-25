"""Shared pytest fixtures + sys.path bootstrap.

Adds the project root to sys.path so `from sites.chapter_merger import ...`
works when running pytest from the project root. We can't rely on the
package being installed via pip — the project ships as a single-file
script (`aio-dl.py`) plus the `sites/` package in-place.

Test-side ML rating policy: production defaults to ML rating OFF (the
2026-05-20 lazy-import refactor — Windows WMI hangs on torch import),
but several tests in this suite exist specifically to exercise torch-
backed code paths (T2 CLIP-IQA/NIQE, T3 paired-DISTS, watermark
detection). We re-enable ML rating once at conftest load so those tests
behave as they did before the gate landed. The probes also populate the
module-level `_PYIQA_AVAILABLE` / `_T2_DEVICE` / etc. constants so test
modules that read them via `from sites.search_orchestrator import ...`
get the post-probe values (not the pre-probe `None` sentinel).

Cross-file: sites/search_orchestrator.py owns `set_ml_rating_enabled`
and the lazy `_pyiqa_available()` family.
"""

from __future__ import annotations

import os
import sys

# Project root = parent of this tests/ directory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Enable ML rating for tests, then prime every lazy availability getter so
# the legacy module-level `_PYIQA_AVAILABLE` / `_T2_DEVICE` / `_CV2_AVAILABLE`
# / `_TORCHMETRICS_AVAILABLE` / `_EASYOCR_AVAILABLE` / `_PIQ_AVAILABLE`
# constants are populated from None → real value BEFORE any test module
# does `from sites.search_orchestrator import _PYIQA_AVAILABLE` at its own
# module-import time. Without this, the imported value would be `None` and
# `is False` assertions or monkeypatch.setattr targets would silently
# misbehave. Each getter is idempotent — repeat calls are O(1).
from sites.search_orchestrator import (  # noqa: E402
    set_ml_rating_enabled,
    _pyiqa_available,
    _torchmetrics_available,
    _easyocr_available,
    _piq_available,
    _cv2_available,
    _t2_device,
)

set_ml_rating_enabled(True)
_pyiqa_available()
_torchmetrics_available()
_easyocr_available()
_piq_available()
_cv2_available()
_t2_device()
