from __future__ import annotations

from .mangathemesia import MangaThemesiaSiteHandler


class RizzComicSiteHandler(MangaThemesiaSiteHandler):
    """Dedicated rizzcomic handler. Inherits MangaThemesia framework behavior
    (search, fetch_comic_context, get_chapters, get_chapter_images) unchanged.

    History: it once overrode the old image-quality probe so rizzcomic never
    fell back to scoring its high-resolution cover (its CDN, rizzchoros.cloud,
    failed real chapter downloads while the cover ranked the site #1; user
    feedback 2026-05-07). The matched-page probe (sites/match_session.py) never
    looks at covers and rates a source that serves no pages `unreachable` for
    every site, which is that rule generalised, so the overrides were removed
    with the old probe.

    Kept as a dedicated class because it is registered in
    sites/__init__.py:_BASE_HANDLERS, which causes the auto-registered
    MangaThemesia entry for rizzcomic in sites/mangathemesia_sites.py to be
    skipped via _MT_DEDICATED_NAMES dedup (name="rizzcomic" wins).
    """

    def __init__(self) -> None:
        super().__init__(
            name="rizzcomic",
            display_name="RizzComic",
            base_url="https://rizzcomic.com",
            domains=("rizzcomic.com", "www.rizzcomic.com"),
        )


__all__ = ["RizzComicSiteHandler"]
