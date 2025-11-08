"""Site handlers for the downloader."""

from __future__ import annotations

from typing import Iterable, Optional

from .base import BaseSiteHandler
from .atsumaru import AtsumaruSiteHandler
from .asura import AsuraSiteHandler
from .artlapsa import ArtlapsaSiteHandler
from .asmotoon import AsmotoonSiteHandler
from .assortedscans import AssortedScansSiteHandler
from .batoto import BatoToSiteHandler
from .dynasty import DynastySiteHandler
from .likemanga import LikeMangaSiteHandler
from .kagane import KaganeSiteHandler
from .madara import MadaraSiteHandler
from .madara_extra_sites import MADARA_EXTRA_SITES
from .mangadex import MangaDexSiteHandler
from .mangabuddy import MangaBuddySiteHandler
from .mangabin import MangaBinSiteHandler
from .mangahub import MangaHubSiteHandler
from .mangafox import MangaFoxSiteHandler
from .mangakakalot import MangaKakalotSiteHandler
from .mangareader import MangaReaderSiteHandler
from .mangakatana import MangaKatanaSiteHandler
from .mangapark import MangaParkSiteHandler
from .mangataro import MangataroSiteHandler
from .summanga import SumMangaSiteHandler
from .weebcentral import WeebCentralSiteHandler

_BASE_HANDLERS: Iterable[BaseSiteHandler] = (
    MangataroSiteHandler(),
    AsuraSiteHandler(),
    ArtlapsaSiteHandler(),
    AsmotoonSiteHandler(),
    AssortedScansSiteHandler(),
    BatoToSiteHandler(),
    MangaParkSiteHandler(),
    MangaBuddySiteHandler(),
    MangaBinSiteHandler(),
    MangaReaderSiteHandler(),
    SumMangaSiteHandler(),
    WeebCentralSiteHandler(),
    AtsumaruSiteHandler(),
    MangaKatanaSiteHandler(),
    MangaKakalotSiteHandler(),
    LikeMangaSiteHandler(),
    MangaFoxSiteHandler(),
    KaganeSiteHandler(),
    DynastySiteHandler(),
    MangaDexSiteHandler(),
    MangaHubSiteHandler(),
)

_REGISTERED_HANDLERS: Iterable[BaseSiteHandler] = tuple(
    list(_BASE_HANDLERS)
    + [MadaraSiteHandler(name, url) for name, url in MADARA_EXTRA_SITES]
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
