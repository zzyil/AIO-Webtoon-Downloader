from __future__ import annotations

from .madara import MadaraSiteHandler


class SumMangaSiteHandler(MadaraSiteHandler):
    def __init__(self) -> None:
        super().__init__("summanga", "https://summanga.com")


__all__ = ["SumMangaSiteHandler"]
