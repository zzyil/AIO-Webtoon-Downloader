from __future__ import annotations

from .madara import MadaraSiteHandler


class MangaBinSiteHandler(MadaraSiteHandler):
    def __init__(self) -> None:
        super().__init__("mangabin", "https://mangabin.com")


__all__ = ["MangaBinSiteHandler"]
