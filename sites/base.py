from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup


@dataclass
class SiteComicContext:
    comic: Dict
    title: str
    identifier: str
    soup: Optional[BeautifulSoup] = None


class BaseSiteHandler:
    """Base class for site-specific handlers."""

    name: str = "base"
    domains: tuple[str, ...] = ()

    def matches(self, url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(domain in netloc for domain in self.domains)

    # --- Session lifecycle -------------------------------------------------
    def configure_session(self, scraper, args) -> None:
        """Give the handler a chance to tweak the HTTP session."""
        return None

    # --- Initial comic retrieval ------------------------------------------
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        """Return the key comic data for downstream processing."""
        raise NotImplementedError

    def extract_additional_metadata(
        self, context: SiteComicContext
    ) -> Dict[str, List[str]]:
        """Optional metadata enrichment hook."""
        return {}

    # --- Chapter helpers ---------------------------------------------------
    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        raise NotImplementedError

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return None

    def select_best_chapter_version(
        self,
        versions: List[Dict],
        preferred_groups: List[str],
        mix_by_upvote: bool,
        log_debug_fn=None,
    ) -> Optional[Dict]:
        if not versions:
            return None

        def upvotes(v: Dict) -> int:
            return v.get("up_count", 0)

        def _debug(msg):
            if log_debug_fn:
                log_debug_fn(msg)

        chap_label = versions[0].get("chap", "?")
        best_by_upvote = max(versions, key=upvotes)

        if not preferred_groups:
            _debug(
                f"    Ch {chap_label}: No group specified. Selected by upvotes ({best_by_upvote.get('up_count', 0)})."
            )
            return best_by_upvote

        if mix_by_upvote:
            preferred = [
                v for v in versions if self.get_group_name(v) in preferred_groups
            ]
            if preferred:
                best = max(preferred, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Mix-by-upvote. Selected '{self.get_group_name(best)}' ({best.get('up_count', 0)} upvotes)."
                )
                return best
            _debug(
                f"    Ch {chap_label}: Mix-by-upvote. No preferred groups found. Fallback to upvotes."
            )
            return best_by_upvote

        for group_name in preferred_groups:
            candidates = [
                v for v in versions if self.get_group_name(v) == group_name
            ]
            if candidates:
                best = max(candidates, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Found in priority group '{group_name}'. Selected."
                )
                return best
        _debug(
            f"    Ch {chap_label}: No priority groups found. Fallback to upvotes."
        )
        return best_by_upvote

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        raise NotImplementedError


__all__ = [
    "BaseSiteHandler",
    "SiteComicContext",
]
