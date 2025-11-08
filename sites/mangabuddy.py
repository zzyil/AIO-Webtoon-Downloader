from __future__ import annotations

from .madara import MadaraSiteHandler


class MangaBuddySiteHandler(MadaraSiteHandler):
    def __init__(self) -> None:
        super().__init__("mangabuddy", "https://mangabuddy.com")


__all__ = ["MangaBuddySiteHandler"]
