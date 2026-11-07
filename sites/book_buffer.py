"""Reorder buffer for the combined book (CBZ / PDF / EPUB, whole or split into parts).

Owns: the order chapters take inside the final file(s). One slot per selected
chapter, in list order; a slot is FILLED (its content is in hand), a HOLE (the
chapter failed for now and may still arrive: a deferred multi-source rescue or a
miss the end-of-run replay will retry) or DROPPED (it will not arrive). Split
parts are cut only from the filled prefix before the first hole, so a chapter that
arrives late always lands in its own place: misplacement is not representable.
Read by: `aio-dl.py` main() (grep `_book =`): the chapter loop adds slots, cuts
parts as they close, and the end-of-run pass fills/drops holes, then `finish()`
hands over the last part for the final-file gate.
Depends on: nothing (pure); the caller computes each chapter's EPUB page count.

Replaces index bookkeeping (insert positions shifted by every earlier late
insert). That could not place a late chapter once a split part had been written,
so main() switches --split off whenever the end-of-run replay is on (grep "split
is disabled while missed-chapter retry"). Deferred multi-source rescues arrive
late in every run, so placement had to become correct by construction (plan
sync-temp/plans/multisource-offset.md, "P2 rev 2", P2.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

FILLED, HOLE, DROPPED = "filled", "hole", "dropped"


@dataclass
class Part:
    """One output file's worth: what build_book_part / the final gate consume.
    `markers` are EPUB chapter markers ({'ch', 'page_index'}), page_index counted
    from the part's first page, only for chapters with at least one EPUB page."""
    content: List[Any] = field(default_factory=list)
    chapters: List[Dict[str, Any]] = field(default_factory=list)
    groups: Set[str] = field(default_factory=set)
    size: int = 0
    markers: List[Dict[str, Any]] = field(default_factory=list)
    pages: int = 0                   # EPUB pages so far: the next marker's page_index


@dataclass
class _Slot:
    key: str
    ch: Dict[str, Any]
    state: str
    content: List[Any] = field(default_factory=list)
    size: int = 0
    group: str = ""
    pages: int = 0


class BookBuffer:
    """Slots in chapter-list order plus the split rule.

    Split rule (the flat-list code's): before chapter X joins the open part, the
    part closes if it already has `split_count` chapters, or if it has a chapter
    and its size plus X's would exceed `split_size`. 0 disables either test. The
    preamble (the CBZ cover) opens the first part and counts toward its size; it
    never closes a part on its own (the flat-list code did, and a part with no
    chapter cannot be named: build_book_part reads its first chapter)."""

    def __init__(self, split_size: int = 0, split_count: int = 0,
                 preamble: Optional[List[Any]] = None, preamble_size: int = 0):
        self.split_size = int(split_size or 0)
        self.split_count = int(split_count or 0)
        self._slots: List[_Slot] = []
        self._index: Dict[str, int] = {}
        self._start = 0                  # first slot not yet in a cut part
        self._pre = list(preamble or [])
        self._pre_size = int(preamble_size or 0)

    # ------------------------------------------------------------ slots
    def _new(self, key: str, ch: Dict[str, Any], state: str) -> _Slot:
        if key in self._index:
            raise KeyError(f"chapter slot {key!r} already exists")
        s = _Slot(key=key, ch=ch, state=state)
        self._index[key] = len(self._slots)
        self._slots.append(s)
        return s

    def add(self, key: str, ch: Dict[str, Any], content: List[Any], size: int,
            group: str = "", pages: int = 0) -> None:
        s = self._new(key, ch, FILLED)
        s.content, s.size, s.group, s.pages = list(content), int(size), group or "", int(pages)

    def hole(self, key: str, ch: Dict[str, Any]) -> None:
        self._new(key, ch, HOLE)

    def drop_new(self, key: str, ch: Dict[str, Any]) -> None:
        """A chapter that will never arrive (permanent miss): keeps list order
        without blocking any flush."""
        self._new(key, ch, DROPPED)

    def fill(self, key: str, ch: Dict[str, Any], content: List[Any], size: int,
             group: str = "", pages: int = 0) -> None:
        """A hole's chapter arrived. `ch` is the dict to record for it (the
        primary's identity, also for a rescue)."""
        s = self._slots[self._index[key]]
        if s.state != HOLE:
            raise ValueError(f"chapter slot {key!r} is {s.state}, not a hole")
        if self._index[key] < self._start:
            raise ValueError(f"chapter slot {key!r} lies in a part already cut")
        s.ch, s.state = ch, FILLED
        s.content, s.size, s.group, s.pages = list(content), int(size), group or "", int(pages)

    def drop(self, key: str) -> None:
        s = self._slots[self._index[key]]
        if s.state == HOLE:
            s.state = DROPPED

    def state(self, key: str) -> Optional[str]:
        i = self._index.get(key)
        return None if i is None else self._slots[i].state

    def holes(self) -> List[str]:
        return [s.key for s in self._slots if s.state == HOLE]

    def chapters(self) -> List[Dict[str, Any]]:
        """Every filled chapter, cut parts included, in list order."""
        return [s.ch for s in self._slots if s.state == FILLED]

    # ------------------------------------------------------------ parts
    def _open_part(self) -> Part:
        p = Part()
        if self._start == 0 and self._pre:
            p.content.extend(self._pre)
            p.size += self._pre_size
        return p

    @staticmethod
    def _join(p: Part, s: _Slot) -> None:
        if s.pages > 0:
            p.markers.append({"ch": s.ch, "page_index": p.pages})
        p.pages += s.pages
        p.content.extend(s.content)
        p.chapters.append(s.ch)
        if s.group:
            p.groups.add(s.group)
        p.size += s.size

    def _closes_before(self, p: Part, s: _Slot) -> bool:
        by_size = self.split_size > 0 and bool(p.chapters) and p.size + s.size > self.split_size
        by_count = self.split_count > 0 and len(p.chapters) >= self.split_count
        return by_size or by_count

    def cut_parts(self) -> List[Part]:
        """Parts that are closed for good: a later chapter in the filled prefix
        already forced them shut. Returned once each; the caller writes them."""
        out: List[Part] = []
        if not (self.split_size or self.split_count):
            return out
        p = self._open_part()
        part_start = self._start
        for i in range(self._start, len(self._slots)):
            s = self._slots[i]
            if s.state == HOLE:
                break
            if s.state == DROPPED:
                continue
            if self._closes_before(p, s):
                out.append(p)
                part_start = i
                self._start = i
                p = self._open_part()
            self._join(p, s)
        self._start = part_start
        return out

    def finish(self) -> List[Part]:
        """End of run: holes still open become drops (their chapters are in the
        missed log), then every remaining part, the last one possibly empty of
        chapters (a CBZ cover alone). For an unsplit book that is one part."""
        for s in self._slots:
            if s.state == HOLE:
                s.state = DROPPED
        out = self.cut_parts()
        p = self._open_part()
        for s in self._slots[self._start:]:
            if s.state == FILLED:
                self._join(p, s)
        self._start = len(self._slots)
        out.append(p)
        return out
