"""Where a primary chapter sits in each alternative site's chapter list.

Owns, for the multi-source rescue (plan sync-temp/plans/multisource-offset.md
rev 3, D3):
- the alt CATALOG: every alt site's full readable list as an ordered `AltList`,
  one per (site, scanlation group) when a site interleaves groups;
- PAIRS (primary entry <-> alt entry, with the evidence tier that made them) and
  their cache on disk (`PairStore`, `<series>/.aio_fp/<primary>/pairings.json`);
- the PREDICTOR that says where to look first, and the PLACEMENT search: find a
  neighbour's copy on a list (`locate`: free anchors, else identifying probes),
  check the far neighbour sits where the count says (`confirm`), the guard of
  a chain that has a neighbour on one side only (`lockstep`, D4), and the list
  order placement tries (`order_lists`).
Read by: `aio-dl.py` (grep `_ms_catalog`, `_rescue_search`, `_place`). The
catalog records come from `aio_search_cli._catalog_from_records`.
Depends on: `strip_match.episode_number` (labels); nothing network-bound. The
probe itself is injected (`identify`), so this file runs in unit tests.

Rule of the module: everything here PREDICTS or PLACES. Nothing here proves a
fill; `chapter_fingerprint` does, on downloaded pages. A wrong prediction costs
probes, never correctness. Labels ("Episode 12") locate and veto, never fill on
their own: 120 of 413 mangakatana titles carry no episode number, and season
restarts reuse numbers. Where both sides carry one and content proves the pair,
they agreed 477/477 (M1, bench/ms-offset).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# Episode labels ("Ep. 12", "Chapter 199: Episode 193"): the same parser the
# search ranker uses, so the two never disagree on what a title says.
from .strip_match import episode_number as episode_label

# Evidence tiers, strongest first.
#   own-pages: the primary's own delivered pages were found inside the alt entry;
#   probe:     4 middle pages of the alt entry were found inside our copy of the
#              chapter (or our probe of it inside them), uniquely within +-20;
#   placement: a fill by position (D3). It passed every check but rests on list
#              position, so it only PREDICTS: a placement pair that anchored or
#              confirmed would let a saved placement confirm itself next run.
# Anchors (free locations, confirmations) come from ANCHOR_TIERS only.
TIER_RANK = {"own-pages": 3, "probe": 2, "placement": 1}
ANCHOR_TIERS = ("own-pages", "probe")
# Names that read as placement grade:
#   chain: a fill by the D4 chain (`lockstep`), a placement with one neighbour;
#          the name is kept in <AioRescue tier=> so the weaker kind shows;
#   elimination / head: tiers of the P2 build still in pairings.json on disk.
#          "head" pairs were placed by first-page probes that picked the wrong
#          entry 5 of 191 times on atsumaru's Gamma group.
_PLACEMENT_ALIASES = {"chain": "placement", "elimination": "placement", "head": "placement"}

# Degenerate numbering: fewer distinct numbers than this share of a list's
# NUMBERED entries means the numbers do not order it (mangapill lists Berserk as "Group 2
# Chapter N", every entry parsed as 2).
DISTINCT_SHARE = 0.9

_NUM = re.compile(r"\d+(?:\.\d+)?")


def first_number(text: Any) -> Optional[float]:
    m = _NUM.search(str(text or ""))
    return float(m.group(0)) if m else None


def last_number(text: Any) -> Optional[float]:
    ms = _NUM.findall(str(text or ""))
    return float(ms[-1]) if ms else None


def tier_of(raw: Any) -> str:
    t = str(raw or "placement")
    t = _PLACEMENT_ALIASES.get(t, t)
    return t if t in TIER_RANK else "placement"


# ---------------------------------------------------------------- catalog

@dataclass
class AltEntry:
    pos: int                       # position in the ordered list
    chapter: Dict[str, Any]        # the site's own chapter dict, untouched
    num: Optional[float]
    label: Optional[int]

    @property
    def url(self) -> Optional[str]:
        return self.chapter.get("url")

    @property
    def chap(self) -> Any:
        return self.chapter.get("chap")


@dataclass
class AltList:
    """One alt site's chapters (or one scanlation group's, `key` = site:group),
    in story order as far as the site's numbers can tell. `ordered` is False when
    neither numbers nor titles order it: T1 (content proof) may still search it,
    placement (which counts entries between neighbours) may not."""
    key: str
    site: str
    record: Dict[str, Any]         # catalog record: handler / scraper / context / url
    entries: List[AltEntry]
    ordered: bool = True
    browser: bool = False          # a browser-driven site: probed after HTTP ones
    _by_url: Dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._by_url = {e.url: e.pos for e in self.entries if e.url}

    def pos_of_url(self, url: Optional[str]) -> Optional[int]:
        return self._by_url.get(url) if url else None

    @property
    def labelled(self) -> bool:
        return any(e.label is not None for e in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def _order(chapters: List[Dict[str, Any]], num_of: Callable[[Any], Optional[float]]
           ) -> Tuple[List[Tuple[Dict[str, Any], Optional[float]]], bool]:
    """Story order for one list -> ([(chapter, num)], ordered?).

    Numbers come from `chap`; when those are degenerate, from the LAST number in
    the title ("Group 2 Chapter 15" -> 15). The list's own direction (sites list
    newest-first or oldest-first) is read from consecutive numbered entries;
    non-numeric entries keep their place after their list predecessor."""
    def nums_by(f):
        return [f(c) for c in chapters]

    def usable(ns) -> bool:
        numbered = [n for n in ns if n is not None]
        return bool(numbered) and len(set(numbered)) >= DISTINCT_SHARE * len(numbered)

    nums = nums_by(lambda c: num_of(c.get("chap")))
    if not usable(nums):
        alt = nums_by(lambda c: last_number(c.get("title")))
        if usable(alt):
            nums = alt
        else:
            return list(zip(chapters, nums)), False
    numbered = [n for n in nums if n is not None]
    ups = sum(1 for a, b in zip(numbered, numbered[1:]) if b > a)
    downs = sum(1 for a, b in zip(numbered, numbered[1:]) if b < a)
    seq = list(zip(chapters, nums))
    if downs > ups:
        seq.reverse()
    keyed = []
    last = float("-inf")
    for i, (c, n) in enumerate(seq):
        if n is not None:
            last = n
        keyed.append(((n if n is not None else last), 0 if n is not None else 1, i, c, n))
    keyed.sort(key=lambda t: (t[0], t[1], t[2]))
    return [(t[3], t[4]) for t in keyed], True


def build_catalog(records: Iterable[Dict[str, Any]], *,
                  group_of: Callable[[Dict[str, Any], Dict[str, Any]], str] = lambda r, c: "",
                  num_of: Callable[[Any], Optional[float]] = first_number,
                  browser_sites: Iterable[str] = ()) -> List[AltList]:
    """AltLists from catalog records ({site, url, chapters, handler, scraper,
    context}; aio_search_cli._catalog_from_records). A site that lists one
    number under two or more scanlation groups is split per group: one group's
    list follows the story, the interleaving does not (atsumaru, P0). Order of
    the result: the records' order with browser-driven sites last, so the cheap
    HTTP sites are probed first."""
    browser = {s.lower() for s in browser_sites}
    out: List[AltList] = []
    seen_keys: Dict[str, int] = {}
    for rec in records:
        chapters = [c for c in (rec.get("chapters") or []) if isinstance(c, dict)]
        if not chapters:
            continue
        site = str(rec.get("site") or "")
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for c in chapters:
            groups.setdefault(group_of(rec, c) or "", []).append(c)
        split = False
        if len(groups) > 1:
            owners: Dict[float, set] = {}
            for g, cs in groups.items():
                for c in cs:
                    n = num_of(c.get("chap"))
                    if n is not None:
                        owners.setdefault(n, set()).add(g)
            split = any(len(v) > 1 for v in owners.values())
        parts = groups.items() if split else [("", chapters)]
        for g, cs in parts:
            key = f"{site}:{g}" if split else site
            # A second record for the same site (two URLs): keep both lists.
            if key in seen_keys:
                seen_keys[key] += 1
                key = f"{key}#{seen_keys[key]}"
            else:
                seen_keys[key] = 1
            seq, ordered = _order(cs, num_of)
            entries = [AltEntry(pos=i, chapter=c, num=n, label=episode_label(c.get("title")))
                       for i, (c, n) in enumerate(seq)]
            out.append(AltList(key=key, site=site, record=rec, entries=entries,
                               ordered=ordered, browser=site.lower() in browser))
    out.sort(key=lambda a: a.browser)          # stable: records' order otherwise
    return out


# ---------------------------------------------------------------- pairs + cache

@dataclass
class Pair:
    primary_idx: int               # index in the FULL primary list
    alt_pos: int
    tier: str


class PairStore:
    """Pairs, durable across runs: the next run's best predictor, and a cached
    content pair is a free anchor.

    File: {"version": 1, "lists": {alt list key: {primary url: {alt_url,
    primary_chap, alt_chap, tier, list_len, saved_at}}}}. Keyed by URL only:
    chapter numbers are not identities (tapas `scene` numbers renumber when the
    publisher inserts an episode; LINE `episode_no` counts notices), so a pair is
    honoured only while both URLs are still in today's lists. `list_len` is the
    alt list's length when the pair was made: a content pair anchors only while
    the list is unchanged (resolve_pairs), since a site that re-indexes can keep
    a URL and move its content."""

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {"version": 1, "lists": {}}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and isinstance(d.get("lists"), dict):
                self.data = d
        except Exception:
            pass

    def raw(self, list_key: str) -> Dict[str, Dict[str, Any]]:
        return self.data["lists"].get(list_key, {})

    def put(self, list_key: str, primary_url: str, primary_chap: Any,
            alt_url: str, alt_chap: Any, tier: str, list_len: int) -> bool:
        """Record a pair; a weaker tier never replaces a stronger one for the same
        primary entry made on a list of the same length. Returns whether the
        store changed (and was saved)."""
        if not primary_url or not alt_url:
            return False
        lst = self.data["lists"].setdefault(list_key, {})
        old = lst.get(primary_url)
        # A stronger pair stays, whether the weaker one agrees with it (nothing to
        # add) or not (it contradicts content evidence and would mislead), unless
        # it was made on a list that has since changed and no longer anchors.
        if old and old.get("list_len") == list_len \
                and TIER_RANK[tier_of(old.get("tier"))] > TIER_RANK[tier_of(tier)]:
            return False
        lst[primary_url] = {"alt_url": alt_url, "primary_chap": primary_chap,
                            "alt_chap": alt_chap, "tier": tier, "list_len": list_len,
                            "saved_at": int(time.time())}
        self.save()
        return True

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self.path)
        except Exception:
            pass                   # a cache: losing it costs probes next run


def resolve_pairs(alt: AltList, store: PairStore, primary_index_by_url: Dict[str, int],
                  extra: Iterable[Pair] = ()) -> List[Pair]:
    """The store's pairs for this list that still resolve (both URLs present
    today), plus this run's in-memory ones (`extra`), one per primary entry,
    strongest tier kept. A stored content pair from a list of another length is
    read as placement grade: it still predicts, it no longer anchors."""
    best: Dict[int, Pair] = {}

    def keep(p: Pair):
        cur = best.get(p.primary_idx)
        if cur is None or TIER_RANK[p.tier] > TIER_RANK[cur.tier]:
            best[p.primary_idx] = p

    for purl, rec in store.raw(alt.key).items():
        i = primary_index_by_url.get(purl)
        q = alt.pos_of_url(rec.get("alt_url"))
        if i is None or q is None:
            continue
        t = tier_of(rec.get("tier"))
        if t in ANCHOR_TIERS and rec.get("list_len") != len(alt):
            t = "placement"
        keep(Pair(i, q, t))
    for p in extra:
        keep(Pair(p.primary_idx, p.alt_pos, tier_of(p.tier)))
    return sorted(best.values(), key=lambda p: p.primary_idx)


# ---------------------------------------------------------------- prediction

def predict(alt: AltList, i: int, primary: Sequence[Dict[str, Any]], pairs: Sequence[Pair],
            num_of: Callable[[Any], Optional[float]] = first_number) -> int:
    """Alt position to look at first for primary entry i (index in the FULL
    primary list). Nearest pair by index distance d -> its alt position + d (the
    offset restarts at every pair, so drift at notices/recaps costs at most
    the distance to the last pair); else the alt entry carrying i's episode
    label; else i's number; else the nearest number; else proportional position."""
    return predict_ex(alt, i, primary, pairs, num_of)[0]


def predict_ex(alt: AltList, i: int, primary: Sequence[Dict[str, Any]], pairs: Sequence[Pair],
               num_of: Callable[[Any], Optional[float]] = first_number
               ) -> Tuple[int, str, Optional[float]]:
    """`predict` plus how it chose: (pos, "pair" | "label" | "number" |
    "proportional", number gap). The gap is |alt entry's number - i's number|
    for "number", else None. A large gap means the list does not reach i's
    number at all (a scanlation group that only did late chapters: MangaDex
    "Binktopia" starting at 262 for our Ch 72), so walking outward from it can
    only probe unrelated chapters; the rescue search skips such a list
    (aio-dl.py, grep _RESCUE_NUM_RADIUS)."""
    n = len(alt)
    if n == 0:
        return 0, "proportional", None
    clamp = lambda q: max(0, min(n - 1, q))
    if pairs:
        p = min(pairs, key=lambda p: (abs(p.primary_idx - i), -TIER_RANK[p.tier]))
        return clamp(p.alt_pos + (i - p.primary_idx)), "pair", None
    ch = primary[i] if 0 <= i < len(primary) else {}
    lab = episode_label(ch.get("title"))
    if lab is not None:
        hits = [e.pos for e in alt.entries if e.label == lab]
        if len(hits) == 1:
            return hits[0], "label", None
    num = num_of(ch.get("chap"))
    numbered = [e for e in alt.entries if e.num is not None]
    if num is not None and numbered:
        e = min(numbered, key=lambda e: (abs(e.num - num), e.pos))
        return e.pos, "number", abs(e.num - num)
    return clamp(int(round(i * n / max(1, len(primary))))), "proportional", None


def outward(start: int, n: int) -> List[int]:
    """start, start-1, start+1, start-2, ... within [0, n)."""
    order = [start] if 0 <= start < n else []
    for d in range(1, n):
        order += [q for q in (start - d, start + d) if 0 <= q < n]
    return order


# ---------------------------------------------------------------- placement (D3)
# Measured offline in M1 (bench/ms-offset/replay_m1.py, unOrdinary over three alt
# lists): 0 wrong over every config; this module mirrors that harness's rules.

# Neighbours (L before the chapter, R after its block) at most this many entries
# out: the widest bracket the design agent's count over truth.json found 0 wrong
# at (6 out made 2 wrong fills on atsumaru's Gamma group).
REACH = 3
# Identifying alt-entry probes allowed for locating ONE neighbour's copy. User
# pick 2026-10-27: 8 fills 248 of 375 lone failures vs 244 at 4, 0 wrong at both,
# for +1.6 alt probe images per fill.
PROBE_BUDGET = 8
# Entries with no images cost a list request, not budget; this caps how many of
# them one search walks past (mangakatana: 169 of 413 entries have none).
EXTRA_STEPS = 12
# A probe names the one chapter of ours within this many entries of where we
# look that its pages lie in (M1: 812/812 named nothing when the true chapter
# was hidden; the identify_eval window).
IDENTIFY_WINDOW = 20

NOIMG = "noimg"
# identify(alt position, centre primary index) -> the primary index the entry's
# probe names (unique within IDENTIFY_WINDOW), None (nothing or several), or
# NOIMG (the entry serves no images).
Identify = Callable[[int, int], Union[int, None, str]]


def label_anchor(alt: AltList, label: Optional[int]) -> Optional[int]:
    """The one entry carrying `label`; None when none or several do."""
    if label is None:
        return None
    hits = [e.pos for e in alt.entries if e.label == label]
    return hits[0] if len(hits) == 1 else None


def free_anchor(alt: AltList, k: int, primary: Sequence[Dict[str, Any]],
                pairs: Sequence[Pair]) -> Optional[Tuple[int, str]]:
    """Primary entry k's copy on `alt` at no probe cost -> (position, tier): a
    content pair (ANCHOR_TIERS), else a unique episode-label match. Placement
    pairs never anchor."""
    for p in pairs:
        if p.primary_idx == k and p.tier in ANCHOR_TIERS:
            return p.alt_pos, p.tier
    q = label_anchor(alt, episode_label((primary[k] if 0 <= k < len(primary) else {}).get("title")))
    return (q, "label") if q is not None else None


@dataclass
class Located:
    pos: Optional[int]
    tier: Optional[str]            # own-pages / probe / label, None when not found
    note: str
    found: List[Pair] = field(default_factory=list)   # content pairs met on the way
    probes: int = 0                # image probes spent


def locate(alt: AltList, k: int, primary: Sequence[Dict[str, Any]], pairs: Sequence[Pair],
           identify: Identify, *, budget: int = PROBE_BUDGET, extra_steps: int = EXTRA_STEPS,
           num_of: Callable[[Any], Optional[float]] = first_number) -> Located:
    """Find primary entry k's copy on `alt`: a free anchor, else identifying
    probes outward from the prediction. A probe that names another of our
    chapters m is still a fact (saved in `found`) and moves the search by k - m
    instead of walking on, which is why a cold list usually needs 1-3 probes."""
    a = free_anchor(alt, k, primary, pairs)
    if a:
        return Located(a[0], a[1], f"free {a[1]} anchor")
    n = len(alt)
    cur: Optional[int] = predict(alt, k, primary, pairs, num_of)
    order = iter(outward(cur, n))
    tried: set = set()
    found: List[Pair] = []
    spent = steps = 0
    while spent < budget and steps < budget + extra_steps:
        while cur is None or cur in tried or not (0 <= cur < n):
            cur = next(order, None)
            if cur is None:
                return Located(None, None, f"list exhausted after {spent} probe(s)", found, spent)
        tried.add(cur)
        steps += 1
        m = identify(cur, k)
        if m != NOIMG:
            spent += 1
        if m == k:
            return Located(cur, "probe", f"found by {spent} probe(s)", found, spent)
        if isinstance(m, int):
            found.append(Pair(m, cur, "probe"))
            cur = cur + (k - m)
            order = iter(outward(cur, n))
        else:
            cur = None
    return Located(None, None, f"not found in {spent} probe(s)", found, spent)


def confirm(alt: AltList, k: int, qk: int, primary: Sequence[Dict[str, Any]],
            pairs: Sequence[Pair], identify: Identify,
            next_evidence: Callable[[], bool]) -> Tuple[bool, str, List[Pair]]:
    """Is primary entry k's copy at `qk`, where the count from the left
    neighbour puts it? -> (ok, note, content pairs found). A free anchor decides
    (one that disagrees fails the list); else ONE probe at qk; an image-less
    entry there hands the question to the entry after it against our k+1
    (`next_evidence()` makes sure we hold evidence for k+1, probing if needed).
    This is the check that catches an extra or missing entry in the slot (a
    recap, a notice, a side story)."""
    if not (0 <= qk < len(alt)):
        return False, "beyond the list", []
    a = free_anchor(alt, k, primary, pairs)
    if a:
        if a[0] == qk:
            return True, f"free {a[1]} anchor agrees", []
        return False, f"its {a[1]} anchor is entry {a[0]}, not {qk}", []
    m = identify(qk, k)
    if m == k:
        return True, "probe", [Pair(k, qk, "probe")]
    if m == NOIMG and qk + 1 < len(alt) and next_evidence():
        m2 = identify(qk + 1, k + 1)
        if m2 == k + 1:
            return True, "probe of the entry after it", [Pair(k + 1, qk + 1, "probe")]
        return False, f"entry {qk} has no images and the next one is not our next chapter", []
    return False, ("the entry there has no images" if m == NOIMG
                   else "the entry there is not it"), []


def lockstep(alt: AltList, primary: Sequence[Dict[str, Any]], k_from: int, q_from: int,
             k_to: int, num_of: Callable[[Any], Optional[float]] = first_number
             ) -> Tuple[bool, str]:
    """Chain guard (plan D4): may primary entries k_from -> k_to be paired with
    alt entries q_from -> q_from + (k_to - k_from), one for one? Every step, in
    either direction, must advance BOTH our number and the alt entry's by
    exactly one; a jump, a repeat or a missing number on either side stops the
    chain. -> (ok, note).
    A chain has no far neighbour to confirm the count, so the numbers are all
    that can catch an entry one side lists and the other does not. Both sides,
    because M1 (bench/ms-offset/replay_m1.py) filled 8 chapters wrong when only
    the alt's numbers were checked: our list lacked an episode mangakatana has,
    and every later fill shifted by one. With both sides, 0 wrong."""
    step = 1 if k_to >= k_from else -1
    for k in range(k_from + step, k_to + step, step):
        q = q_from + (k - k_from)
        if not (0 <= q < len(alt)):
            return False, f"the list ends before Ch {primary[k].get('chap')}"
        a0, a1 = alt.entries[q - step].num, alt.entries[q].num
        o0, o1 = num_of(primary[k - step].get("chap")), num_of(primary[k].get("chap"))
        if (a0 is None or a1 is None or o0 is None or o1 is None
                or abs(a1 - a0 - step) > 1e-6 or abs(o1 - o0 - step) > 1e-6):
            return False, (f"not one-for-one: Ch {primary[k - step].get('chap')} -> "
                           f"Ch {primary[k].get('chap')} here, chapter "
                           f"{alt.entries[q - step].chap} -> {alt.entries[q].chap} there")
    return True, f"one-for-one over {abs(k_to - k_from)} step(s)"


def order_lists(catalog: Sequence[AltList], pairs_of: Callable[[AltList], Sequence[Pair]]
                ) -> List[AltList]:
    """The lists placement tries, one at a time: ordered lists only (counting
    entries means nothing on an unordered one), HTTP sites before browser-driven
    ones, then lists holding more content pairs (cheap to locate on), then
    labelled lists (free label anchors), then catalog order."""
    def key(ia):
        idx, alt = ia
        content = sum(1 for p in pairs_of(alt) if p.tier in ANCHOR_TIERS)
        return (alt.browser, -content, not alt.labelled, idx)
    return [a for _, a in sorted(enumerate(catalog), key=key) if a.ordered and len(a)]


def length_check(cand_widths: float, pages_total: int,
                 neighbours: Sequence[Tuple[float, int]]) -> Tuple[Optional[bool], str]:
    """Is the candidate the right length for the chapter? `neighbours` are
    (length in widths, page count) of fingerprinted primary chapters near it.
    With the chapter's own page count: expected = pages x the neighbours' median
    widths-per-page, band [0.80, 1.35] (P0: 0.879-1.308 for 1-99% of chapters).
    Without: the neighbours' median length, band [0.50, 2.00] (P0 1-99%:
    0.59-1.67; it only catches gross mismatches). None when there are no
    neighbours to compare with."""
    nb = [(w, p) for w, p in neighbours if w > 0]
    if not nb:
        return None, "no neighbour lengths"
    srt = lambda xs: sorted(xs)[len(xs) // 2]
    if pages_total > 0:
        wpp = [w / p for w, p in nb if p > 0]
        if wpp:
            exp = pages_total * srt(wpp)
            r = cand_widths / exp
            return 0.80 <= r <= 1.35, f"length {r:.2f}x the expected {exp:.0f} widths"
    exp = srt([w for w, _ in nb])
    r = cand_widths / exp
    return 0.50 <= r <= 2.00, f"length {r:.2f}x the neighbours' {exp:.0f} widths"
