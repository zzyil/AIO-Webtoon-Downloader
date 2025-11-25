from __future__ import annotations

from typing import List, Optional
from urllib.parse import urljoin

from .base import SiteComicContext
from .mangathemesia import MangaThemesiaSiteHandler


class TecnoxmoonSiteHandler(MangaThemesiaSiteHandler):
    """Custom handler for Tecnoxmoon / TercoScans to enrich metadata."""

    def __init__(self) -> None:
        super().__init__(
            name="tercoscans",
            display_name="TercoScans",
            base_url="https://tecnoxmoon.xyz",
            domains=(
                "tecnoxmoon.xyz",
                "www.tecnoxmoon.xyz",
                "tecnocomic1.xyz",
                "www.tecnocomic1.xyz",
            ),
        )

    def fetch_comic_context(self, url, scraper, make_request) -> SiteComicContext:
        context = super().fetch_comic_context(url, scraper, make_request)
        self._ensure_real_cover(context)
        self._enrich_metadata(context)
        return context

    def _enrich_metadata(self, context: SiteComicContext) -> None:
        soup = context.soup
        if not soup:
            return

        authors = context.comic.get("authors") or []
        if not authors:
            authors = self._extract_label_values(soup, "Author")
        if not authors:
            authors = self._extract_label_values(soup, "Posted By")
        if authors:
            context.comic["authors"] = authors

        status = self._extract_label_values(soup, "Status")
        if status:
            context.comic["status"] = status[0]

        comic_type = self._extract_label_values(soup, "Type")
        if comic_type:
            context.comic["type"] = comic_type[0]

    def _ensure_real_cover(self, context: SiteComicContext) -> None:
        """Replace lazy-loaded placeholder covers with the actual image URL."""
        soup = context.soup
        if not soup:
            return

        cover = (context.comic or {}).get("cover")
        needs_fix = not cover or cover.lower().startswith("data:image/svg")
        if not needs_fix:
            return

        cover_node = soup.select_one(".thumb img, .summary_image img")
        if not cover_node:
            return

        cover_url = self._extract_cover_url(cover_node)
        if cover_url:
            context.comic["cover"] = cover_url

    def _extract_cover_url(self, node) -> Optional[str]:
        srcset_attrs = ("data-srcset", "data-lazy-srcset", "srcset")
        for attr in srcset_attrs:
            raw_value = node.get(attr)
            if not raw_value:
                continue
            first = raw_value.split(",")[0].strip().split()[0]
            if first and not first.lower().startswith("data:image/svg"):
                return self._normalize_cover_url(first)

        attr_order = (
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-url",
            "src",
        )
        for attr in attr_order:
            value = node.get(attr)
            if not value:
                continue
            value = value.strip()
            if not value or value.lower().startswith("data:image/svg"):
                continue
            return self._normalize_cover_url(value)
        return None

    def _normalize_cover_url(self, value: str) -> str:
        if value.startswith("//"):
            return f"https:{value}"
        if value.startswith("/"):
            return urljoin(self.base_url, value)
        return value

    def _extract_label_values(self, soup, label: str) -> List[str]:
        for row in soup.select(".imptdt"):
            text = row.get_text(" ", strip=True)
            if not text.lower().startswith(label.lower()):
                continue
            values: List[str] = []
            for node in row.select("a, i"):
                value = node.get_text(strip=True)
                if value:
                    values.append(value)
            if not values:
                remainder = text[len(label) :].strip()
                if remainder:
                    values.append(remainder)
            if values:
                seen = []
                for val in values:
                    if val not in seen:
                        seen.append(val)
                return seen
        return []


__all__ = ["TecnoxmoonSiteHandler"]
