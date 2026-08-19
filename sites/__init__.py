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
from .dynasty import DynastySiteHandler
from .likemanga import LikeMangaSiteHandler
from .madara import MadaraSiteHandler
from .manhwaread import ManhwaReadHandler
from .madara_extra_sites import MADARA_EXTRA_SITES
from .mangadex import MangaDexSiteHandler
from .mangabin import MangaBinSiteHandler
from .mangahub import MangaHubSiteHandler
from .mangafox import MangaFoxSiteHandler
from .manganato import ManganatoSiteHandler
from .mangakatana import MangaKatanaSiteHandler
from .mangataro import MangataroSiteHandler
from .summanga import SumMangaSiteHandler
from .weebcentral import WeebCentralSiteHandler
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
from .rizzcomic import RizzComicSiteHandler
from .rizzfables import RizzFablesSiteHandler
from .tecnoxmoon import TecnoxmoonSiteHandler
from .violetscans import VioletScansSiteHandler
from .boratscans import BoratScansSiteHandler
from .linewebtoon import LineWebtoonSiteHandler
from .tapas import TapasSiteHandler
from .kappabeast import KappabeastSiteHandler

# MangaThemesia sites - unified handler
from .mangathemesia import MangaThemesiaSiteHandler
from .mangathemesia_sites import MANGATHEMESIA_SITES

from .manhuaplus import ManhuaPlusSiteHandler
from .manhuaus import ManhuaUSSiteHandler

# ---------------------------------------------------------------------------
# Optional handlers — modules that import a NATIVE extension at module scope.
#
# All three pull in `cryptography` (mangareader additionally imports Pillow).
# On desktop those always resolve, so this block is a no-op and the registry
# stays at the documented REGISTERED: 303 / BASE: 42.
#
# WHY GUARD AT ALL: every import in this file runs before `_BASE_HANDLERS` is
# built, so ONE missing wheel takes down the ENTIRE registry — all 303 domains
# — rather than the 3 sites that actually need it. That is a real platform, not
# a hypothetical: Android via Chaquopy is the live case.
#
# Failures are RECORDED, not swallowed — a site vanishing silently is how you
# spend an afternoon wondering why a URL says "no handler". Cross-file:
# aio_android.py logs _OPTIONAL_HANDLER_ERRORS at startup.
# ---------------------------------------------------------------------------
_OPTIONAL_HANDLER_ERRORS: dict = {}

try:
    from .kagane import KaganeSiteHandler
except Exception as _exc:  # noqa: BLE001 — any import-time failure, not just ImportError
    KaganeSiteHandler = None  # type: ignore[assignment]
    _OPTIONAL_HANDLER_ERRORS["kagane"] = f"{type(_exc).__name__}: {_exc}"

try:
    from .mangago import MangaGoSiteHandler
except Exception as _exc:  # noqa: BLE001
    MangaGoSiteHandler = None  # type: ignore[assignment]
    _OPTIONAL_HANDLER_ERRORS["mangago"] = f"{type(_exc).__name__}: {_exc}"

try:
    from .mangareader import MangaReaderSiteHandler
except Exception as _exc:  # noqa: BLE001
    MangaReaderSiteHandler = None  # type: ignore[assignment]
    _OPTIONAL_HANDLER_ERRORS["mangareader"] = f"{type(_exc).__name__}: {_exc}"


def _optional(factory):
    """Instantiate an optional handler, or None when its module didn't import.

    Kept as a filtered-out None rather than an omitted entry so the literal
    below preserves its ORIGINAL ORDER. Order is load-bearing:
    get_handler_for_url returns the FIRST handler whose domain matches, so
    reshuffling could silently reassign a URL to a different handler.
    """
    if factory is None:
        return None
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 — a ctor that needs a missing dep
        _OPTIONAL_HANDLER_ERRORS.setdefault(
            getattr(factory, "__name__", str(factory)), f"{type(exc).__name__}: {exc}"
        )
        return None


_BASE_HANDLERS: Iterable[BaseSiteHandler] = tuple(
    _h
    for _h in (
        MangataroSiteHandler(),
        MangaFireSiteHandler(),
        _optional(MangaGoSiteHandler),
        AsuraSiteHandler(),
        ArtlapsaSiteHandler(),
        AsmotoonSiteHandler(),
        AssortedScansSiteHandler(),
        ArcRelightSiteHandler(),
        ArcaneScansSiteHandler(),
        MangaBinSiteHandler(),
        _optional(MangaReaderSiteHandler),
        SumMangaSiteHandler(),
        WeebCentralSiteHandler(),
        AtsumaruSiteHandler(),
        MangaKatanaSiteHandler(),
        ManganatoSiteHandler(),
        LikeMangaSiteHandler(),
        MangaFoxSiteHandler(),
        _optional(KaganeSiteHandler),
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
        RizzComicSiteHandler(),
        RizzFablesSiteHandler(),
        TecnoxmoonSiteHandler(),
        VioletScansSiteHandler(),
        BoratScansSiteHandler(),
        LineWebtoonSiteHandler(),
        TapasSiteHandler(),
        ManhwaReadHandler(),
        KappabeastSiteHandler(),
    )
    if _h is not None
)


# Create MangaThemesia handlers from configuration. Skip entries whose name
# OR domain is already covered by a dedicated handler in _BASE_HANDLERS
# (arcanescans, tercoscans/tecnoxmoon — both have richer dedicated handlers
# AND appear in MANGATHEMESIA_SITES, producing duplicate candidates without
# this filter).
_MT_DEDICATED_NAMES = {h.name.lower() for h in _BASE_HANDLERS}
_MT_DEDICATED_DOMAINS: set = set()
for _h in _BASE_HANDLERS:
    for _d in getattr(_h, "domains", ()) or ():
        _MT_DEDICATED_DOMAINS.add(_d.lower())

_MANGATHEMESIA_HANDLERS = []
for site_conf in MANGATHEMESIA_SITES:
    if site_conf["name"].lower() in _MT_DEDICATED_NAMES:
        continue
    site_domains = site_conf.get("domains") or ()
    if any(d.lower() in _MT_DEDICATED_DOMAINS for d in site_domains):
        continue
    handler = MangaThemesiaSiteHandler(
        name=site_conf["name"],
        display_name=site_conf["display_name"],
        base_url=site_conf["base_url"],
        domains=site_conf["domains"],
        url_normalizer=site_conf.get("url_normalizer"),
        chapter_filter=site_conf.get("chapter_filter"),
        use_playwright=site_conf.get("use_playwright", False),
        use_zendriver=site_conf.get("use_zendriver", False),
        chapter_selector=site_conf.get("chapter_selector"),
        verify_ssl=site_conf.get("verify_ssl", True),
    )
    _MANGATHEMESIA_HANDLERS.append(handler)


def _build_madara_extras_skipping_dups() -> list:
    """Build MadaraSiteHandler instances from MADARA_EXTRA_SITES, skipping
    entries whose name OR domain already has a dedicated handler.

    Some sites in MADARA_EXTRA_SITES have richer dedicated handlers
    (Toonily, ManhuaPlus, ManhuaUS, ArcaneScans, MangaBin, SumManga,
    TercoScans). Without this filter both the dedicated and the generic
    Madara handler register, producing duplicate candidates in the search
    output (e.g. 'Toonily' AND 'toonily'). The dedicated handler always
    wins — this skips the redundant generic Madara registration.

    Comparison is case-insensitive on name and exact-match on domain
    netloc so subtle case differences ('toonily' vs 'Toonily') don't slip
    through as duplicates.
    """
    dedicated_names: set = set()
    dedicated_domains: set = set()
    for h in _BASE_HANDLERS:
        dedicated_names.add(h.name.lower())
        for d in getattr(h, "domains", ()) or ():
            dedicated_domains.add(d.lower())
    extras = []
    for name, url in MADARA_EXTRA_SITES:
        if name.lower() in dedicated_names:
            continue
        # Domain check too — site might be registered under a different
        # 'name' but same domain (e.g. dedicated 'manhuaplus' handler vs
        # MADARA entry 'manhuaplusonline' for the same site).
        host_low = url.replace("https://", "").replace("http://", "").rstrip("/").lower()
        if host_low in dedicated_domains:
            continue
        extras.append(MadaraSiteHandler(name, url))
    return extras


_REGISTERED_HANDLERS: Iterable[BaseSiteHandler] = tuple(
    list(_BASE_HANDLERS)
    + _MANGATHEMESIA_HANDLERS
    + _build_madara_extras_skipping_dups()
)


def get_handler_by_name(name: str) -> Optional[BaseSiteHandler]:
    # Case-insensitive lookup on BOTH sides. Some Madara child handlers
    # (Toonily, WebtoonXYZ) pass a mixed-case display name to the parent's
    # __init__, which overwrites the lowercase class attribute and leaves
    # handler.name capitalized at runtime. Before this fix the comparison
    # silently failed for those two sites — get_handler_by_name("toonily")
    # returned None even though the handler was registered. Regression
    # guards: tests/test_komikku_metadata.py::test_get_handler_by_name_case_insensitive_*
    lowered = name.lower()
    for handler in _REGISTERED_HANDLERS:
        if handler.name.lower() == lowered:
            return handler
    return None


def get_handler_for_url(url: str) -> Optional[BaseSiteHandler]:
    for handler in _REGISTERED_HANDLERS:
        if handler.matches(url):
            return handler
    return None


def iter_search_capable_handlers() -> Iterable[BaseSiteHandler]:
    """Yield handlers whose `search` method is overridden vs the base no-op.

    Used by sites.search_orchestrator.search_all to skip handlers that haven't
    implemented search() yet. We compare unbound function objects on the class
    so subclasses that explicitly override the method are picked up; subclasses
    that just inherit the base are filtered out. This matches the "no
    reliability tracking — handlers that can't search just don't show up" rule
    from the plan (snappy-forging-waffle.md core principle 1).
    """
    base_search = BaseSiteHandler.search
    for handler in _REGISTERED_HANDLERS:
        # type(handler).search resolves to the overriding function on the
        # class hierarchy. If it's the same object as BaseSiteHandler.search,
        # the handler hasn't implemented search.
        if type(handler).search is not base_search:
            yield handler


__all__ = [
    "get_handler_by_name",
    "get_handler_for_url",
    "iter_search_capable_handlers",
    "BaseSiteHandler",
]
