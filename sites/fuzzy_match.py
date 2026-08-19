"""The single seam between this project and rapidfuzz.

What this module owns: the lazy rapidfuzz import, the input normalization that
keeps rapidfuzz's C++ and pure-Python backends in agreement, and thin wrappers
for the only three scorer shapes the project uses.

Who reads from it:
  - sites/search_orchestrator.py:_best_title_match  -> wratio()          (cross-site search)
  - sites/external_metadata.py:_score_candidate_detail -> processed_wratio()
  - sites/external_metadata.py:_author_match_score  -> processed_token_set_ratio()

Depends on: rapidfuzz only, imported lazily so a packager who strips it breaks
only search + AniList enrichment (with a clear message) rather than downloads.

--------------------------------------------------------------------------
WHY THIS MODULE EXISTS: rapidfuzz ships TWO backends
--------------------------------------------------------------------------
rapidfuzz's public `fuzz`/`utils` modules dispatch to a compiled `*_cpp`
extension when one is present and fall back to the pure-Python `*_py` reference
modules when it is not (see rapidfuzz/fuzz.py). Desktop gets the C++ backend
from a PyPI wheel. **Android does not** -- Chaquopy's index publishes no
rapidfuzz wheel, so the port installs rapidfuzz's own pure-Python wheel
(android/wheels/, built from the official sdist with wheel.cmake=false, which
is upstream's supported "CMake unavailable, falling back to pure Python
Extension" path). Same library, same version, same algorithms.

The two backends are NOT bit-identical, and the thresholds in this project are
calibrated to specific numbers (WRatio >= 75 admission gate, token_set_ratio
>= 85 author match, _TITLE_BAND_DELTA = 8 in external_metadata.py). An
exhaustive sweep of all 1,114,112 codepoints found exactly two causes:

  1. SEPARATOR DISAGREEMENT -- 3 codepoints, and the reason this module
     normalizes. `utils_py.default_process` filters with `re.compile(r"(?ui)\\W")`
     and in Python regex `_` is a WORD character, so the Python backend keeps
     underscores that the C++ backend replaces with a space. Separately, the
     Python tokenizer is `str.split()`, which splits on Unicode whitespace,
     while the C++ tokenizer splits on ASCII whitespace only -- so U+00A0 and
     U+0085 are token separators to one backend and ordinary characters to the
     other. U+00A0 matters in practice: scraped HTML titles carry `&nbsp;`.
     Mapping these three to a plain space makes BOTH backends see the identical
     string, which is why normalization fixes this class completely.

  2. UNICODE VERSION SKEW -- 5536 codepoints, NOT fixable here and deliberately
     left alone. rapidfuzz's C++ character tables are older than the Unicode
     database Python 3.13 ships, so characters added in newer Unicode versions
     (4824 of them CJK extensions, plus Vithkuqi/Kawi/Tangsa/... and U+0130
     LATIN CAPITAL LETTER I WITH DOT ABOVE, whose Python `.lower()` expands to
     `i` + U+0307 while C++ gives plain `i`) are alphanumeric to one backend and
     punctuation to the other. Mapping these to spaces would corrupt real text
     -- a CJK ideograph is not a separator. Measured worst-case cost is 4.35
     score points (real case: the library's "Iblis Keser" with U+0130 scores
     100.00 cpp / 95.65 py against its ASCII spelling), far from the 75/85
     gates, and 1 of 130 real series contains any such codepoint at all.

`tests/test_fuzzy_match.py` re-derives both sets from the installed rapidfuzz
and fails if either grows -- so a rapidfuzz or Python upgrade that widens the
gap is a failing test, not a silent scoring drift on one platform.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

# The exact codepoints the two backends disagree on as *separators*. Derived by
# exhaustive sweep, not by intuition -- and deliberately NOT "every Unicode
# space": U+2000-U+200A, U+2028/9, U+202F, U+205F and U+3000 all behave
# identically in both backends and are left untouched so this stays the
# smallest edit that closes the gap.
#
#   U+005F  LOW LINE            -- `\W` treats `_` as a word char; C++ does not
#   U+0085  NEXT LINE           -- str.split() splits here; C++ does not
#   U+00A0  NO-BREAK SPACE      -- str.split() splits here; C++ does not
#
# Written as escapes on purpose: two of the three are invisible in an editor,
# and a stray literal NBSP in this file would be undebuggable.
#
# grep target: _MATCH_SEPARATORS. Keep in sync with tests/test_fuzzy_match.py,
# which recomputes this set against the installed rapidfuzz.
_MATCH_SEPARATORS = ("_", '\x85', '\xa0')

# str.translate is length-preserving and one-for-one: a string containing none
# of the three is returned unchanged and therefore scores exactly as it always
# has. Runs of spaces are NOT collapsed, on purpose -- collapsing would change
# scores for the far more common "double space" case, which both backends
# already agree on.
_NORMALIZE_TABLE = {ord(c): " " for c in _MATCH_SEPARATORS}


def normalize_for_match(value: Any) -> str:
    """Make `value` score identically under rapidfuzz's C++ and Python backends.

    Callers should not need this directly -- the wrappers below apply it. It is
    public because tests assert on it and because a future call site that needs
    a scorer this module does not wrap still has to normalize.
    """
    if not value:
        return ""
    return str(value).translate(_NORMALIZE_TABLE)


def load_rapidfuzz() -> Tuple[Any, Callable[[str], str]]:
    """(fuzz, default_process), or ImportError naming the fix.

    Lazy so that stripping rapidfuzz breaks only search and enrichment. Callers
    that want a score should prefer the wrappers below; this stays exported for
    sites/external_metadata.py:_load_rapidfuzz, which keeps its own name because
    its ImportError message names the feature the user was actually using.
    """
    try:
        from rapidfuzz import fuzz
        from rapidfuzz.utils import default_process
    except ImportError as exc:  # pragma: no cover - exercised by the CLI, not tests
        raise ImportError(
            "rapidfuzz is required for cross-site search and metadata "
            "enrichment. Install with: pip install rapidfuzz"
        ) from exc
    return fuzz, default_process


def wratio(a: Any, b: Any) -> float:
    """WRatio, 0..100, no processor -- the cross-site search shape.

    Search compares a user query against scraped site titles, so it is the call
    site most likely to meet a `&nbsp;` and the reason normalization is not
    limited to the enrichment path.
    """
    fuzz, _ = load_rapidfuzz()
    return float(fuzz.WRatio(normalize_for_match(a), normalize_for_match(b)))


def processed_wratio(a: Any, b: Any) -> float:
    """WRatio with rapidfuzz's default_process, 0..100 -- the AniList title shape.

    default_process lowercases, trims and replaces non-alphanumerics; without it
    WRatio is brutally case-sensitive (see _score_candidate_detail).
    """
    fuzz, default_process = load_rapidfuzz()
    return float(
        fuzz.WRatio(
            normalize_for_match(a),
            normalize_for_match(b),
            processor=default_process,
        )
    )


def processed_token_set_ratio(a: Any, b: Any) -> float:
    """token_set_ratio with default_process, 0..100 -- the author-match shape.

    Order- and extra-token-insensitive, which is what makes "Hata Kenjirou" and
    "Kenjirou Hata" score 100.
    """
    fuzz, default_process = load_rapidfuzz()
    return float(
        fuzz.token_set_ratio(
            normalize_for_match(a),
            normalize_for_match(b),
            processor=default_process,
        )
    )


def rapidfuzz_backend() -> Optional[str]:
    """"cpp", "python", or None when rapidfuzz is missing entirely.

    Diagnostics only -- aio_android.diagnostics() reports it so a device that
    silently lost the wheel is one log line to spot rather than a mystery
    "search found nothing".
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None
    return "python" if fuzz.WRatio.__module__.endswith("fuzz_py") else "cpp"
