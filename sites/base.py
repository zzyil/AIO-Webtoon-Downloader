from __future__ import annotations

from dataclasses import dataclass
import re
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

    def normalize_group_name(self, group_name: Optional[str]) -> Optional[str]:
        if not isinstance(group_name, str):
            return None
        cleaned = group_name.strip().casefold()
        if not cleaned:
            return None
        cleaned = re.sub(r"[_./-]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if re.search(r"\bofficial\b", cleaned):
            return "official"
        return cleaned

    def get_group_match_key(self, group_name: Optional[str]) -> Optional[str]:
        normalized = self.normalize_group_name(group_name)
        if not normalized:
            return None
        squashed = re.sub(r"[^0-9a-z]+", "", normalized)
        return squashed or normalized

    def select_best_chapter_version(
        self,
        versions: List[Dict],
        preferred_groups: List[str],
        mix_by_upvote: bool,
        allow_group_fallback: bool = True,
        log_debug_fn=None,
    ) -> Optional[Dict]:
        if not versions:
            return None

        def upvotes(v: Dict) -> int:
            return v.get("up_count", 0)

        def _debug(msg):
            if log_debug_fn:
                log_debug_fn(msg)

        def _available_groups() -> str:
            groups: List[str] = []
            for version in versions:
                group_name = self.get_group_name(version)
                if not isinstance(group_name, str):
                    continue
                cleaned = group_name.strip()
                if cleaned and cleaned not in groups:
                    groups.append(cleaned)
            return ", ".join(groups) if groups else "none"

        def _annotate_selection(
            version: Dict,
            *,
            selection_kind: str,
            requested_group: Optional[str] = None,
        ) -> Dict:
            annotated = dict(version)
            annotated["_selection_kind"] = selection_kind
            annotated["_requested_group"] = requested_group
            annotated["_available_groups"] = _available_groups()
            return annotated

        chap_label = versions[0].get("chap", "?")
        best_by_upvote = max(versions, key=upvotes)

        if not preferred_groups:
            _debug(
                f"    Ch {chap_label}: No group specified. Selected by upvotes ({best_by_upvote.get('up_count', 0)})."
            )
            return _annotate_selection(best_by_upvote, selection_kind="upvote_no_group")

        preferred_entries = [
            (group_name, self.get_group_match_key(group_name))
            for group_name in preferred_groups
        ]
        preferred_entries = [
            (group_name, match_key)
            for group_name, match_key in preferred_entries
            if match_key
        ]
        if not preferred_entries:
            _debug(
                f"    Ch {chap_label}: Group filter contained no usable names. Selected by upvotes ({best_by_upvote.get('up_count', 0)})."
            )
            return _annotate_selection(best_by_upvote, selection_kind="upvote_invalid_group_filter")

        if mix_by_upvote:
            preferred = [
                v
                for v in versions
                if self.get_group_match_key(self.get_group_name(v))
                in {match_key for _, match_key in preferred_entries}
            ]
            if preferred:
                best = max(preferred, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Mix-by-upvote. Selected '{self.get_group_name(best)}' ({best.get('up_count', 0)} upvotes)."
                )
                return _annotate_selection(best, selection_kind="preferred_mix_by_upvote")
            if not allow_group_fallback:
                _debug(
                    f"    Ch {chap_label}: Mix-by-upvote. None of the requested groups were present. Skipping chapter. Available groups: {_available_groups()}."
                )
                return None
            _debug(
                f"    Ch {chap_label}: Mix-by-upvote. None of the requested groups were present. Falling back to upvotes with '{self.get_group_name(best_by_upvote)}'. Available groups: {_available_groups()}."
            )
            return _annotate_selection(
                best_by_upvote,
                selection_kind="fallback_missing_group",
            )

        for group_name, match_key in preferred_entries:
            candidates = [
                v
                for v in versions
                if self.get_group_match_key(self.get_group_name(v)) == match_key
            ]
            if candidates:
                best = max(candidates, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Found in priority group '{group_name}'. Selected '{self.get_group_name(best)}'."
                )
                return _annotate_selection(
                    best,
                    selection_kind="preferred_priority",
                    requested_group=group_name,
                )
        if not allow_group_fallback:
            _debug(
                f"    Ch {chap_label}: None of the requested groups were present. Skipping chapter. Available groups: {_available_groups()}."
            )
            return None
        _debug(
            f"    Ch {chap_label}: None of the requested groups were present. Falling back to upvotes with '{self.get_group_name(best_by_upvote)}'. Available groups: {_available_groups()}."
        )
        return _annotate_selection(
            best_by_upvote,
            selection_kind="fallback_missing_group",
        )

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        raise NotImplementedError


__all__ = [
    "BaseSiteHandler",
    "SiteComicContext",
]
