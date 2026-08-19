"""Coverage for sites/fuzzy_match — the rapidfuzz seam, and the Android port's
riskiest correctness claim.

WHY THIS EXISTS: rapidfuzz ships a compiled `*_cpp` backend and a pure-Python
`*_py` backend and picks whichever is importable. Desktop gets C++ from a PyPI
wheel; Android has no Chaquopy wheel and runs the pure-Python one. Every
threshold in this project is a specific number tuned against rapidfuzz's
scorers (WRatio >= 75 admission gate, token_set_ratio >= 85 author match,
_TITLE_BAND_DELTA = 8), so if the two backends disagree the phone silently
matches a different series than the desktop -- and the failure mode is a wrong
cover and wrong tags, never an exception.

These tests pin the disagreement. They re-derive it from the INSTALLED
rapidfuzz rather than trusting the constant, so a rapidfuzz upgrade or a Python
Unicode-table bump that widens the gap fails here instead of drifting.

Every test needs BOTH backends importable to compare them, so all of them skip
on a machine with only one (which includes Android itself -- run these on
desktop).

Cross-file: sites/fuzzy_match.py (the module), sites/search_orchestrator.py
:_best_title_match and sites/external_metadata.py:_score_candidate_detail /
:_author_match_score (the three call sites), android/wheels/ (the pure-Python
wheel this makes safe to ship).
"""

from __future__ import annotations

import random
import unicodedata

import pytest

from sites import fuzzy_match

# Both backends must be importable side by side to compare them at all.
fuzz_cpp = pytest.importorskip(
    "rapidfuzz.fuzz_cpp", reason="needs the compiled rapidfuzz backend to compare against"
)
fuzz_py = pytest.importorskip("rapidfuzz.fuzz_py")
utils_cpp = pytest.importorskip("rapidfuzz.utils_cpp")
utils_py = pytest.importorskip("rapidfuzz.utils_py")


# Real strings from the matcher's documented failure cases (the AniList
# disambiguation invariants in CLAUDE.md) plus the shapes that historically
# broke it: all-caps, subtitle prefixes, romanization drift, CJK/Hangul.
TITLES = [
    "Frieren", "Sousou no Frieren", "Frieren: Beyond Journey's End",
    "Solo Leveling", "Solo Leveling: Ragnarok", "나 혼자만 레벨업",
    "Fly Me to the Moon", "Tonikaku Kawaii", "Fairy Tail", "FAIRY TAIL",
    "unOrdinary", "Unordinary Life", "Kanojo Iro no Kanojo", "Eleceed",
    "FULL METAL ALCHEMIST", "Full Metal Alchemist", "Hagane no Renkinjutsushi",
    "Toaru Kagaku no Railgun", "One-Punch Man", "ワンパンマン",
    "The Beginning After the End", "İblis Keser", "Kaguya-sama: Love is War",
    "[Oneshot] Some Doujin (Fairy Tail) [English]", "", "   ", "-", "A",
]

AUTHORS = [
    "Hata Kenjirou", "Kenjirou Hata", "MASHIMA Hiro", "Hiro Mashima",
    "ZHENA", "Hye-Jin Kim", "Jeho Son", "Jae-Ho Son", "Redice Studio",
    "uru-chan", "Great H", "Roots", "Key (Company)", "Unknown", "n/a", "",
    "ONE", "Murata Yuusuke", "藤本タツキ",
]


def _random_pairs(n, seed=20260812):
    """Adversarial sweep over the general string space, not just plausible
    titles. Seeded so a failure is reproducible from the test name alone."""
    rng = random.Random(seed)
    alphabet = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        " .,:;!?-–—'\"()[]/\\&#@+*~_"
        "àéîõüçñßØæ" "の一二人日本語漫画少年女神" "가나다라마바사아자차"
        "\t\n\r\x0b\x0c \u00a0\u0085\u3000\u2009\u202f"
    )
    out = []
    for _ in range(n):
        out.append((
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))),
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))),
        ))
    return out


def _corpus_pairs():
    pairs = [(a, b) for a in TITLES for b in TITLES]
    pairs += [(a, b) for a in AUTHORS for b in AUTHORS]
    pairs += [(a, b) for a in TITLES for b in AUTHORS]
    return pairs


def _has_unicode_skew(s):
    """True when `s` carries a codepoint the two backends classify differently
    for reasons normalization cannot fix (see fuzzy_match's header, cause 2).

    Derived from the installed rapidfuzz rather than hardcoded, so this keeps
    partitioning correctly after an upgrade. The corpus deliberately contains
    one such string -- the real library's Turkish "Iblis Keser" -- and the
    agreement tests must exclude it rather than pretend normalization fixes
    skew; the cost of that skew is pinned by its own tests below.
    """
    for ch in s:
        if ch in fuzzy_match._MATCH_SEPARATORS:
            continue
        probe = "a" + ch + "b"
        if utils_cpp.default_process(probe) != utils_py.default_process(probe):
            return True
    return False


def _both_agree(a, b):
    """(cpp, py) for all three scorer shapes the project uses, as one tuple."""
    return (
        (fuzz_cpp.WRatio(a, b),
         fuzz_py.WRatio(a, b)),
        (fuzz_cpp.WRatio(a, b, processor=utils_cpp.default_process),
         fuzz_py.WRatio(a, b, processor=utils_py.default_process)),
        (fuzz_cpp.token_set_ratio(a, b, processor=utils_cpp.default_process),
         fuzz_py.token_set_ratio(a, b, processor=utils_py.default_process)),
        (utils_cpp.default_process(a),
         utils_py.default_process(a)),
    )


# --- The normalizer itself -------------------------------------------------

def test_normalizer_maps_exactly_the_three_separators():
    assert fuzzy_match._MATCH_SEPARATORS == ("_", "\u0085", "\u00a0")
    for ch in fuzzy_match._MATCH_SEPARATORS:
        assert fuzzy_match.normalize_for_match(f"a{ch}b") == "a b"


def test_normalizer_is_length_preserving_and_never_collapses_runs():
    """Collapsing whitespace runs would change scores for the common
    double-space case, which both backends already agree on. The normalizer
    must be a one-for-one translation and nothing more."""
    assert fuzzy_match.normalize_for_match("a  b") == "a  b"
    assert fuzzy_match.normalize_for_match("  a  ") == "  a  "
    assert fuzzy_match.normalize_for_match("a__b") == "a  b"
    for s in TITLES + AUTHORS:
        assert len(fuzzy_match.normalize_for_match(s)) == len(s)


def test_normalizer_leaves_ordinary_titles_byte_identical():
    """The whole desktop-safety argument: a string with none of the three
    characters is returned unchanged, so it scores exactly as it always did."""
    for s in TITLES + AUTHORS:
        if not any(c in s for c in fuzzy_match._MATCH_SEPARATORS):
            assert fuzzy_match.normalize_for_match(s) == s


def test_unicode_spaces_that_already_agree_are_left_alone():
    """Deliberately NOT 'every Unicode space' -- U+3000 and friends behave
    identically in both backends, and mapping them would be a gratuitous
    desktop scoring change."""
    for ch in "\u3000\u2000\u2009\u202f\u205f\u1680\u2028\u2029":
        assert fuzzy_match.normalize_for_match(f"a{ch}b") == f"a{ch}b"


def test_normalize_handles_none_and_non_strings():
    assert fuzzy_match.normalize_for_match(None) == ""
    assert fuzzy_match.normalize_for_match("") == ""
    assert fuzzy_match.normalize_for_match(0) == ""
    assert fuzzy_match.normalize_for_match(12) == "12"


# --- The equivalence claim -------------------------------------------------

def test_backends_agree_on_the_real_corpus_after_normalization():
    compared = 0
    for a, b in _corpus_pairs():
        if _has_unicode_skew(a) or _has_unicode_skew(b):
            continue
        na, nb = fuzzy_match.normalize_for_match(a), fuzzy_match.normalize_for_match(b)
        for cpp, py in _both_agree(na, nb):
            assert cpp == py, f"{a!r} vs {b!r}"
        compared += 1
    # Guards against the exclusion silently swallowing the whole corpus.
    assert compared > 1500, compared


def test_backends_agree_on_random_pairs_after_normalization():
    compared = 0
    for a, b in _random_pairs(3000):
        if _has_unicode_skew(a) or _has_unicode_skew(b):
            continue
        na, nb = fuzzy_match.normalize_for_match(a), fuzzy_match.normalize_for_match(b)
        for cpp, py in _both_agree(na, nb):
            assert cpp == py, f"{a!r} vs {b!r}"
        compared += 1
    assert compared > 2500, compared


def test_the_divergence_is_real_without_normalization():
    """Guards the guard: if this ever stops failing, upstream fixed the
    backends and the normalizer became dead weight -- worth knowing, and worth
    deleting deliberately rather than by accident."""
    raw_disagreements = 0
    for a, b in _random_pairs(3000):
        for cpp, py in _both_agree(a, b):
            if cpp != py:
                raw_disagreements += 1
    assert raw_disagreements > 0, (
        "rapidfuzz's C++ and Python backends now agree unnormalized; "
        "sites/fuzzy_match's normalization may no longer be needed"
    )


def test_underscore_is_the_documented_default_process_divergence():
    assert utils_cpp.default_process("a_b") == "a b"
    assert utils_py.default_process("a_b") == "a_b"


def test_nbsp_is_the_documented_token_split_divergence():
    assert fuzz_cpp.token_sort_ratio("a\u00a0b", "b a") != fuzz_py.token_sort_ratio("a\u00a0b", "b a")
    a, b = fuzzy_match.normalize_for_match("a\u00a0b"), fuzzy_match.normalize_for_match("b a")
    assert fuzz_cpp.token_sort_ratio(a, b) == fuzz_py.token_sort_ratio(a, b)


# --- The residue we are knowingly accepting --------------------------------

# Characters added to Unicode after rapidfuzz's bundled C++ tables were built.
# Normalization cannot fix these -- a CJK ideograph is not a separator -- so the
# test pins the COST instead. U+0130 is the one that occurs in the real library
# ("Iblis Keser"); the rest are representative of the 5536-codepoint set.
_SKEW_SAMPLES = ["İ", "ࡰ", "鿽", "Ꟁ", "Ⱟ", "\U00010570"]

# Measured worst case is 4.35 points (U+0130 against an ASCII spelling of the
# same title). The bound is deliberately far below the smallest gate margin
# that matters, so a rapidfuzz/Unicode bump that makes the skew genuinely
# dangerous trips this rather than shipping quietly.
_MAX_SKEW_POINTS = 8.0


def test_unicode_skew_stays_within_the_documented_bound():
    worst = 0.0
    for ch in _SKEW_SAMPLES:
        for base in ("Solo Leveling", "Frieren", "Fairy Tail"):
            for other in (base, base + ch, base.upper()):
                a = fuzzy_match.normalize_for_match(base + ch)
                b = fuzzy_match.normalize_for_match(other)
                worst = max(
                    worst,
                    abs(fuzz_cpp.WRatio(a, b, processor=utils_cpp.default_process)
                        - fuzz_py.WRatio(a, b, processor=utils_py.default_process)),
                )
    assert worst <= _MAX_SKEW_POINTS, f"unicode skew widened to {worst:.2f} points"


def test_the_real_librarys_turkish_title_still_clears_the_admission_gate():
    """The only divergent codepoint present in 130 real series. Both backends
    must stay well clear of the 75 gate, which is what makes the skew
    acceptable rather than merely small."""
    for scorer_cpp, scorer_py in (
        (lambda a, b: fuzz_cpp.WRatio(a, b, processor=utils_cpp.default_process),
         lambda a, b: fuzz_py.WRatio(a, b, processor=utils_py.default_process)),
    ):
        a = fuzzy_match.normalize_for_match("İblis Keser")
        b = fuzzy_match.normalize_for_match("Iblis Keser")
        assert scorer_cpp(a, b) >= 90.0
        assert scorer_py(a, b) >= 90.0


# --- The wrappers actually normalize ---------------------------------------

def test_wrappers_route_through_the_normalizer():
    """A call site that used rapidfuzz directly would silently skip
    normalization; these assert the wrappers do not."""
    assert fuzzy_match.wratio("Solo\u00a0Leveling", "Solo Leveling") == \
        fuzzy_match.wratio("Solo Leveling", "Solo Leveling")
    assert fuzzy_match.processed_wratio("Kaguya_sama", "Kaguya sama") == \
        fuzzy_match.processed_wratio("Kaguya sama", "Kaguya sama")
    assert fuzzy_match.processed_token_set_ratio("Hata_Kenjirou", "Kenjirou Hata") == 100.0


def test_wrappers_return_plain_floats():
    """aio-dl.py compares these against int thresholds and json-serializes
    them; a numpy scalar or Decimal leaking through would only show up at the
    JSON boundary."""
    for value in (
        fuzzy_match.wratio("a", "b"),
        fuzzy_match.processed_wratio("a", "b"),
        fuzzy_match.processed_token_set_ratio("a", "b"),
    ):
        assert type(value) is float


def test_backend_reporter_names_the_compiled_backend_on_desktop():
    assert fuzzy_match.rapidfuzz_backend() == "cpp"


# --- The exhaustive proof --------------------------------------------------

def test_exhaustive_codepoint_sweep_finds_no_new_separator_divergence():
    """The real guarantee behind _MATCH_SEPARATORS: sweep every one of the
    1,114,112 codepoints and assert the set of SEPARATOR disagreements is
    exactly the three the normalizer maps.

    This is the test that makes shipping the pure-Python wheel defensible, and
    at ~5s for the full sweep it runs by default rather than behind a marker.
    """
    separators = []
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        probe = "a" + ch + "b"
        proc_differs = utils_cpp.default_process(probe) != utils_py.default_process(probe)
        tok_differs = (fuzz_cpp.token_sort_ratio("xx" + ch + "yy", "yy xx")
                       != fuzz_py.token_sort_ratio("xx" + ch + "yy", "yy xx"))
        if not (proc_differs or tok_differs):
            continue
        # Separator-like = the two backends disagree about whether this splits
        # tokens. Everything else is Unicode version skew (letters/digits one
        # side's tables know), which normalization must not touch.
        if ch == "_" or ch.isspace() or unicodedata.category(ch) in ("Zs", "Zl", "Zp", "Cc"):
            separators.append(ch)
    assert tuple(separators) == fuzzy_match._MATCH_SEPARATORS, (
        f"separator divergence changed: {[hex(ord(c)) for c in separators]}"
    )
