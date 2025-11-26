"""Site handlers for the downloader."""

from __future__ import annotations

from typing import Iterable, Optional

from .base import BaseSiteHandler
from .atsumaru import AtsumaruSiteHandler
from .asura import AsuraSiteHandler
from .artlapsa import ArtlapsaSiteHandler
from .asmotoon import AsmotoonSiteHandler
from .assortedscans import AssortedScansSiteHandler
from .arc_relight import ArcRelightSiteHandler
from .arcanescans import ArcaneScansSiteHandler
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
from .manganato import ManganatoSiteHandler
from .mangareader import MangaReaderSiteHandler
from .mangakatana import MangaKatanaSiteHandler
from .mangapark import MangaParkSiteHandler
from .mangataro import MangataroSiteHandler
from .summanga import SumMangaSiteHandler
from .weebcentral import WeebCentralSiteHandler
from .mangafire import MangaFireSiteHandler
from .mangafire import MangaFireSiteHandler
from .comix import ComixSiteHandler
from .flamecomics import FlameComicsSiteHandler
from .tcbscans import TCBScansSiteHandler
from .voyceme import VoyceMeSiteHandler
from .webtoonxyz import WebtoonXYZSiteHandler
from .toonily import ToonilySiteHandler
from .zeroscans import ZeroScansSiteHandler
from .mangapill import MangaPillSiteHandler
from .omegascans import OmegaScansSiteHandler
from .rizzfables import RizzFablesSiteHandler
from .tecnoxmoon import TecnoxmoonSiteHandler
from .violetscans import VioletScansSiteHandler
from .boratscans import BoratScansSiteHandler

# MangaThemesia sites - unified handler
from .mangathemesia import MangaThemesiaSiteHandler
from .mangathemesia_sites import MANGATHEMESIA_SITES

from .manhuaplus import ManhuaPlusSiteHandler
from .manhuaus import ManhuaUSSiteHandler

_BASE_HANDLERS: Iterable[BaseSiteHandler] = (
    MangataroSiteHandler(),
    MangaFireSiteHandler(),
    AsuraSiteHandler(),
    ArtlapsaSiteHandler(),
    AsmotoonSiteHandler(),
    AssortedScansSiteHandler(),
    ArcRelightSiteHandler(),
    ArcaneScansSiteHandler(),
    BatoToSiteHandler(),
    MangaParkSiteHandler(),
    MangaBuddySiteHandler(),
    MangaBinSiteHandler(),
    MangaReaderSiteHandler(),
    SumMangaSiteHandler(),
    WeebCentralSiteHandler(),
    AtsumaruSiteHandler(),
    MangaKatanaSiteHandler(),
    ManganatoSiteHandler(),
    LikeMangaSiteHandler(),
    MangaFoxSiteHandler(),
    KaganeSiteHandler(),
    DynastySiteHandler(),
    MangaDexSiteHandler(),
    MangaHubSiteHandler(),
    ComixSiteHandler(),
    FlameComicsSiteHandler(),
    TCBScansSiteHandler(),
    VoyceMeSiteHandler(),
    WebtoonXYZSiteHandler(),
    ToonilySiteHandler(),
    ZeroScansSiteHandler(),
    MangaPillSiteHandler(),
    ManhuaPlusSiteHandler(),
    ManhuaUSSiteHandler(),
    OmegaScansSiteHandler(),
    RizzFablesSiteHandler(),
    TecnoxmoonSiteHandler(),
    VioletScansSiteHandler(),
    BoratScansSiteHandler(),
)

# Create MangaThemesia handlers from configuration
_MANGATHEMESIA_HANDLERS = []
for site_conf in MANGATHEMESIA_SITES:
    # Skip disabled sites (commented out in config)
    
    handler = MangaThemesiaSiteHandler(
        name=site_conf["name"],
        display_name=site_conf["display_name"],
        base_url=site_conf["base_url"],
        domains=site_conf["domains"],
        url_normalizer=site_conf.get("url_normalizer"),
        chapter_filter=site_conf.get("chapter_filter"),
        use_playwright=site_conf.get("use_playwright", False),
        chapter_selector=site_conf.get("chapter_selector"),
        verify_ssl=site_conf.get("verify_ssl", True),
    )
    _MANGATHEMESIA_HANDLERS.append(handler)


_REGISTERED_HANDLERS: Iterable[BaseSiteHandler] = tuple(
    list(_BASE_HANDLERS)
    + _MANGATHEMESIA_HANDLERS
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
