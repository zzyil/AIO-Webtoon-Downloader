from __future__ import annotations

import json
import re
from typing import Dict, List

from bs4 import BeautifulSoup, FeatureNotFound

from .madara import MadaraSiteHandler


class ArcaneScansSiteHandler(MadaraSiteHandler):
    name = "arcanescans"
    domains = ("arcanescans.org", "www.arcanescans.org")

    _TS_READER_RE = re.compile(r"ts_reader\.run\((\{.*?\})\);?", re.DOTALL)

    def __init__(self) -> None:
        super().__init__("arcanescans", "https://arcanescans.org")

    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,
    ) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            return super().get_chapter_images(chapter, scraper, make_request)

        response = make_request(chapter_url, scraper)
        html = response.text

        ts_data = self._extract_ts_reader_data(html)
        if ts_data:
            images = self._extract_images_from_ts_reader(ts_data)
            if images:
                return images

        return super().get_chapter_images(chapter, scraper, make_request)

    def _extract_ts_reader_data(self, html: str) -> Dict:
        match = self._TS_READER_RE.search(html)
        if not match:
            return {}
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}

    def _extract_images_from_ts_reader(self, data: Dict) -> List[str]:
        sources = data.get("sources")
        if not isinstance(sources, list):
            return []

        for source in sources:
            images = source.get("images")
            if isinstance(images, list) and images:
                return [img for img in images if isinstance(img, str)]
        return []


__all__ = ["ArcaneScansSiteHandler"]
