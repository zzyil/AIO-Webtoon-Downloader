"""atsu.moe's search index mixes machine-made spellings into `otherNames`.

Next to the real alternate titles, every search document carries each name with
its spaces removed and with one pair of adjacent words fused, often cut off after
the first few words. Two series that OPEN with the same words therefore share
identical junk, and the cross-site merge (sites/search_orchestrator.py, grep
`_union(`) joins any two hits sharing a title key — which is how "A Certain
Scientific Railgun: Astral Buddy" got welded into the main Railgun candidate and
mangahub's spin-off entry was offered as the main series (2026-10-17).

The strings below are copied from the live search response for that query.
Cross-file: sites/atsumaru.py:_drop_generated_name_variants.
"""

from __future__ import annotations

from sites.atsumaru import _drop_generated_name_variants
from sites.search_orchestrator import _normalize_title

MAIN_TITLE = "A Certain Scientific Railgun"
MAIN_NAMES = [
    MAIN_TITLE,
    "a certain scientific railgun",
    "A Certain Vending Machine's Proof of Existence",
    "To Aru Kagaku no Choudenjihou",
    "Toaru Kagaku no Railgun",
    "어떤 과학의 초전자포",
    "とある科学の超電磁砲（レールガン）",
    "acertainscientificrailgun",
    "acertainvendingmachinesproofofexistence",
    "toarukagakunochoudenjihou",
    "어떤과학의초전자포",
    "とある科学の超電磁砲レールガン",
    "acertain scientific railgun",
    "a certainscientific railgun",
    "a certain scientificrailgun",
    "a certain vending machines proofof existence",
    "toaru kagaku no choudenjihou",
    "to arukagaku no choudenjihou",
]

SPINOFF_TITLE = "A Certain Scientific Railgun: Astral Buddy"
SPINOFF_NAMES = [
    SPINOFF_TITLE,
    "a certain scientific railgun astral buddy",
    "Astral Buddy",
    "Toaru Kagaku no Railgun Gaiden: Astral Body",
    "acertainscientificrailgunastralbuddy",
    "astralbuddy",
    "toarukagakunorailgungaidenastralbody",
    "acertain scientific railgun",
    "a certainscientific railgun",
    "a certain scientificrailgun",
    "toarukagaku no railgun gaiden",
    "astralbody",
]


def test_generated_variants_are_dropped_and_real_names_kept():
    kept = _drop_generated_name_variants(MAIN_NAMES)
    assert kept == [
        MAIN_TITLE,
        "a certain scientific railgun",
        "A Certain Vending Machine's Proof of Existence",
        "To Aru Kagaku no Choudenjihou",
        "Toaru Kagaku no Railgun",
        "어떤 과학의 초전자포",
        "とある科学の超電磁砲（レールガン）",
    ]


def test_spinoff_no_longer_shares_a_title_key_with_the_main_series():
    """The regression itself: after filtering, no normalized key links them."""
    main = {_normalize_title(n) for n in _drop_generated_name_variants(MAIN_NAMES)}
    spin = {_normalize_title(n) for n in _drop_generated_name_variants(SPINOFF_NAMES)}
    assert main & spin == set()
    # ...and the spin-off keeps the names that DO legitimately identify it.
    assert "Toaru Kagaku no Railgun Gaiden: Astral Body" in _drop_generated_name_variants(SPINOFF_NAMES)


def test_two_respacings_never_eliminate_each_other():
    """A variant needs strictly MORE words in its source, so of two spellings
    that differ only in spacing, the spaced one always survives."""
    assert _drop_generated_name_variants(["나 혼자만 레벨업", "나혼자만레벨업"]) == ["나 혼자만 레벨업"]
    assert _drop_generated_name_variants(["Solo Leveling", "solo leveling"]) == [
        "Solo Leveling", "solo leveling",
    ]


def test_capitalised_and_unrelated_short_names_are_kept():
    names = ["Solo Leveling", "나혼렙", "Only I Level Up", "ONLYILEVELUP"]
    assert _drop_generated_name_variants(names) == names
