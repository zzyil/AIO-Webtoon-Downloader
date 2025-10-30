"""Site handlers for the downloader."""

from __future__ import annotations

from typing import Iterable, Optional

from .base import BaseSiteHandler
from .mangataro import MangataroSiteHandler
from .asura import AsuraSiteHandler
from .batoto import BatoToSiteHandler

_REGISTERED_HANDLERS: Iterable[BaseSiteHandler] = (
    MangataroSiteHandler(),
    AsuraSiteHandler(),
    BatoToSiteHandler(),
)


def get_handler_by_name(name: str) -> Optional[BaseSiteHandler]:
    lowered = name.lower()
    for handler in _REGISTERED_HANDLERS:
        if handler.name == lowered:
            return handler
    return None


def get_handler_for_url(url: str) -> Optional[BaseSiteHandler]:
    for handler in _REGISTERED_HANDLERS:
        if handler.matches(url):
            return handler
    return None


__all__ = [
    "get_handler_by_name",
    "get_handler_for_url",
    "BaseSiteHandler",
]
