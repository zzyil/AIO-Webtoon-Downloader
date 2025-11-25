from __future__ import annotations

from .madara import MadaraSiteHandler


_MANGABUDDY_MIRRORS = (
    "mangacute.com",
    "mangaforest.com",
    "mangamirror.com",
    "mangapuma.com",
    "mangaxyz.com",
    "truemanga.com",
)


def _expand_domains(domains):
    expanded = set()
    for domain in domains:
        expanded.add(domain)
        if not domain.startswith("www."):
            expanded.add(f"www.{domain}")
    return tuple(sorted(expanded))


class MangaBuddySiteHandler(MadaraSiteHandler):
    def __init__(self) -> None:
        extra_domains = _expand_domains(_MANGABUDDY_MIRRORS)
        super().__init__("mangabuddy", "https://mangabuddy.com", extra_domains=extra_domains)


__all__ = ["MangaBuddySiteHandler"]
