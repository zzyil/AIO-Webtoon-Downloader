"""Machine-translation (MTL) detection for scanlation group names.

What this module owns:
  - `classify_mtl(name, description=...)` → (verdict, reason). Verdict is one of
    MTL_NONE / MTL_SUSPECT / MTL_CONFIRMED.

What reads from it:
  - sites/base.py — the composite chapter-version ranker (grep `_mtl_rank`);
    MTL versions sink below human ones but are still selected when they are the
    ONLY version of a chapter (the `--mtl avoid` default).
  - aio-dl.py — the `--mtl {avoid,allow,exclude}` flag's exclude path.

Deliberately REGEX-ONLY: no catalog file, no network, no state (user decision
2026-08-02). The alternative considered and rejected was a curated
group_quality.json mirroring official_publishers.json — rejected because a
hand-maintained MTL roster ages badly and the escape hatch below covers the
misfire case without one.

FALSE-POSITIVE POLICY — read this before adding a pattern:
  A misfire only DEMOTES a group (it never drops a chapter under the default
  `avoid` policy), and naming the group in `--group "<name>"` bypasses ranking
  entirely via the priority branch. So the cost of a false positive is a
  suboptimal pick, not data loss. Even so, every pattern here must be
  word-boundary anchored and must not fire on ordinary group names:
    - "AI" is the whole risk surface. 愛 romanizes as `Ai`, never `AI`, and
      title-case is universal for names — so the bare-token pattern is
      CASE-SENSITIVE and runs against the RAW name. Casefolding it is a bug:
      it would flag "Aiden Scans", "Rainbow Ai", "Aiko TL".
    - Bare `AI` is only ever SUSPECT. Only `AI` adjacent to a translation word
      ("AI Translations", "AI-TL") is CONFIRMED.
    - `\b` anchors also keep `\bmtl\b` off "Formatl" and `\bbot\b` off "Botan".
  If a real group ever gets flagged, the fix is to tighten the pattern here —
  there is intentionally no per-group override list to fall back on.

Cross-file: sites/base.py:normalize_group_name produces the normalized form the
lowercase patterns expect (separators collapsed to single spaces, casefolded).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

MTL_NONE = "none"
MTL_SUSPECT = "suspect"
MTL_CONFIRMED = "confirmed"

# Ranked worst-to-best; sites/base.py maps these onto the `mtl_rank` tier.
_VERDICT_ORDER = {MTL_CONFIRMED: 0, MTL_SUSPECT: 1, MTL_NONE: 2}


# Matched against the NORMALIZED name (casefolded, separators → spaces), so
# "MTL-Scans" / "MTL_Scans" / "mtl.scans" all boundary-split identically.
_MTL_CONFIRMED = (
    (r"\bmtl\b", "name says MTL"),
    # `machine\w*` (not a bare `machine`) so "Machiner Translate" and
    # "MachineTranslated" hit. The trailing `translat` must still follow
    # immediately, so "Machine Doll Translations" (a series title, not a
    # method) does NOT match.
    (r"machine\w*\s*translat", "name says machine translation"),
    (r"\bgoogle\s*translat", "name says Google Translate"),
    (r"\bdeepl\b", "name says DeepL"),
    (r"\bpapago\b", "name says Papago"),
    (r"\bchatgpt\b", "name says ChatGPT"),
    (r"\bgpt\s*-?\s*[0-9]", "name says GPT-n"),
    (r"\bauto\s*translat", "name says auto-translated"),
    (r"\btranslated\s+by\s+(?:ai|machine|bot)\b", "name says translated by AI/machine"),
)

# The "AI" token is matched CASE-SENSITIVELY against the RAW name (see the
# FALSE-POSITIVE POLICY above); the translation word that must follow it is
# matched case-insensitively, since "AI Translations" / "AI-tl" / "AI TL" are
# all the same claim. Split into two patterns rather than one regex because a
# single `re.IGNORECASE` pass would also fold `\bAI\b` down to `Ai` and flag a
# group named after the given name 愛.
# NB: no `^` anchor on the tail — it is applied via `.match(text, pos)`, which
# already anchors at pos, whereas `^` would only ever match at offset 0.
_AI_TOKEN_CASED = re.compile(r"\bAI\b")
_AI_TRANSLATION_TAIL = re.compile(r"\s*(?:translat|tl\b)", re.IGNORECASE)

_MTL_SUSPECT = (
    (r"\bneural\b", "name mentions neural"),
    (r"\bbot\b", "name mentions bot"),
)

# Bare uppercase AI token — plausible as an acronym, so SUSPECT only.
_MTL_SUSPECT_CASED = (
    (r"\bAI\b", "name contains a bare 'AI' token"),
)


def _first_match(patterns, text: str, flags: int = 0) -> Optional[str]:
    """Return the reason string of the first pattern that hits, else None."""
    for pattern, reason in patterns:
        if re.search(pattern, text, flags):
            return reason
    return None


def _collapse_separators(text: str) -> str:
    """Collapse `_./-` to spaces WITHOUT casefolding.

    The AI checks need both properties at once: separators normalized (so
    `\\b` sees a boundary in "AI_Translated", where `_` is a word character and
    would otherwise swallow it) and original casing intact (so `Ai` never
    matches the case-sensitive `AI` token).
    """
    return re.sub(r"\s+", " ", re.sub(r"[_./-]+", " ", text)).strip()


def _ai_translation_claim(text: str) -> bool:
    """True when a case-sensitive 'AI' token is directly followed by a
    translation word ("AI Translations", "AI-TL"). A bare 'AI' is not enough —
    that stays SUSPECT via _MTL_SUSPECT_CASED."""
    spaced = _collapse_separators(text)
    for match in _AI_TOKEN_CASED.finditer(spaced):
        if _AI_TRANSLATION_TAIL.match(spaced, match.end()):
            return True
    return False


def _normalize_for_match(text: str) -> str:
    """Lowercase + collapse separators so word boundaries land predictably.

    Mirrors sites/base.py:normalize_group_name's cleanup without importing it
    (this module must stay dependency-free so it can be unit-tested standalone
    and imported from anywhere in sites/ without a cycle).
    """
    cleaned = re.sub(r"[_./-]+", " ", text.casefold())
    return re.sub(r"\s+", " ", cleaned).strip()


def classify_mtl(
    name: Optional[str],
    *,
    description: Optional[str] = None,
) -> Tuple[str, str]:
    """Classify a scanlation group as machine-translated from its metadata.

    Args:
        name: the group's display name, RAW (not pre-normalized) — the
            case-sensitive "AI" guard depends on original casing.
        description: the group's self-description where the site exposes one.
            MangaDex is the only site that does today (it rides the
            `includes[]=scanlation_group` relationship the handler already
            fetches). Everything else passes None and degrades to name-only.

    Returns:
        (verdict, reason). `reason` is a short human string for the selection
        debug log; empty string when the verdict is MTL_NONE.

    Description promotion: a group whose NAME is clean but whose description
    says "all machine translated" is confirmed — that class of group is
    invisible to name matching and is exactly what descriptions are for. A
    description can only ever RAISE the verdict, never lower it.
    """
    if not isinstance(name, str) or not name.strip():
        return MTL_NONE, ""

    raw = name.strip()
    normalized = _normalize_for_match(raw)

    reason = _first_match(_MTL_CONFIRMED, normalized)
    if reason:
        return MTL_CONFIRMED, reason
    if _ai_translation_claim(raw):
        return MTL_CONFIRMED, "name says AI translation"

    verdict = MTL_NONE
    # The cased set runs on the separator-collapsed name for the same
    # word-boundary reason _ai_translation_claim does ("AI_Scans").
    suspect_reason = _first_match(_MTL_SUSPECT, normalized) or _first_match(
        _MTL_SUSPECT_CASED, _collapse_separators(raw)
    )
    if suspect_reason:
        verdict = MTL_SUSPECT

    # Description promotion. Only the CONFIRMED patterns are consulted — the
    # suspect set ("bot", "neural") is far too loose for free prose, where
    # "our bot posts releases to Discord" is a routine sentence.
    if isinstance(description, str) and description.strip():
        desc_normalized = _normalize_for_match(description)
        desc_reason = _first_match(_MTL_CONFIRMED, desc_normalized)
        if desc_reason is None and _ai_translation_claim(description):
            desc_reason = "name says AI translation"
        if desc_reason:
            return MTL_CONFIRMED, desc_reason.replace("name says", "description says")

    return verdict, suspect_reason or ""


def mtl_rank(verdict: str) -> int:
    """Map a verdict onto the ranker's tier value (higher = better)."""
    return _VERDICT_ORDER.get(verdict, 2)


__all__ = [
    "MTL_NONE",
    "MTL_SUSPECT",
    "MTL_CONFIRMED",
    "classify_mtl",
    "mtl_rank",
]
