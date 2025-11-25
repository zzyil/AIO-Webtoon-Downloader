"""
MangaThemesia site configurations.

Each site can have custom settings:
- url_normalizer: Function to normalize URLs (e.g., redirect domains)
- chapter_filter: CSS selector modifier to filter chapters (e.g., skip locked)
- custom_headers: Additional HTTP headers
"""

MANGATHEMESIA_SITES = [
    {
        "name": "flonescans",
        "display_name": "FloneScans",
        "base_url": "https://sweetmanhwa.online",
        "domains": ("sweetmanhwa.online", "www.sweetmanhwa.online"),
    },
    {
        "name": "erosscans",
        "display_name": "ErosScans",
        "base_url": "https://erosvoid.xyz",
        "domains": ("erosvoid.xyz", "www.erosvoid.xyz", "erosscans.xyz", "www.erosscans.xyz", "erosxsun.xyz", "www.erosxsun.xyz"),
        "url_normalizer": lambda url: url.replace("erosscans.xyz", "erosvoid.xyz").replace("erosxsun.xyz", "erosvoid.xyz"),
    },
    {
        "name": "galaxymanga",
        "display_name": "GalaxyManga",
        "base_url": "https://galaxymanga.io",
        "domains": ("galaxymanga.io", "www.galaxymanga.io"),
    },
    {
        "name": "kingofshojo",
        "display_name": "KingOfShojo",
        "base_url": "https://kingofshojo.com",
        "domains": ("kingofshojo.com", "www.kingofshojo.com"),
    },
    {
        "name": "kirascans",
        "display_name": "KiraScans",
        "base_url": "https://kirascans.com",
        "domains": ("kirascans.com", "www.kirascans.com"),
        "chapter_filter": "li:not(:has(svg))",  # Skip chapters with SVG (locked/premium)
    },
    {
        "name": "lagoonscans",
        "display_name": "LagoonScans",
        "base_url": "https://lagoonscans.com",
        "domains": ("lagoonscans.com", "www.lagoonscans.com"),
    },
    {
        "name": "armageddon",
        "display_name": "Armageddon",
        "base_url": "https://www.silentquill.net",
        "domains": ("silentquill.net", "www.silentquill.net"),
    },
    {
        "name": "elftoon",
        "display_name": "ElfToon",
        "base_url": "https://elftoon.com",
        "domains": ("elftoon.com", "www.elftoon.com"),
        "chapter_filter": "li:not(:has(.gem-price-icon))",  # Skip paid chapters
    },
    {
        "name": "evascans",
        "display_name": "EvaScans",
        "base_url": "https://evascans.org",
        "domains": ("evascans.org", "www.evascans.org"),
    },
    {
        "name": "kappabeast",
        "display_name": "KappaBeast",
        "base_url": "https://kappabeast.com",
        "domains": ("kappabeast.com", "www.kappabeast.com"),
    },
    {
        "name": "culturedworks",
        "display_name": "CulturedWorks",
        "base_url": "https://culturedworks.com",
        "domains": ("culturedworks.com", "www.culturedworks.com"),
    },
    # Failed sites (SSL or selector issues) - Disabled for now
    # {
    #     "name": "lavatoons",
    #     "display_name": "LavaToons",
    #     "base_url": "https://lavatoons.com",
    #     "domains": ("lavatoons.com", "www.lavatoons.com"),
    # },
    {
        "name": "leemiau",
        "display_name": "LeeMiau",
        "base_url": "https://leemiau.com",
        "domains": ("leemiau.com", "www.leemiau.com"),
    },
    {
        "name": "mihentai",
        "display_name": "MiHentai",
        "base_url": "https://mihentai.net",
        "domains": ("mihentai.net", "www.mihentai.net"),
    },
    {
        "name": "arcanescans",
        "display_name": "ArcaneScans",
        "base_url": "https://arcanescans.org",
        "domains": ("arcanescans.org", "www.arcanescans.org", "arcanescans.com", "www.arcanescans.com"),
    },
    {
        "name": "hentai20",
        "display_name": "Hentai20",
        "base_url": "https://hentai20.io",
        "domains": ("hentai20.io", "www.hentai20.io"),
    },
    {
        "name": "mangagojo",
        "display_name": "MangaGojo",
        "base_url": "https://mangagojo.com",
        "domains": ("mangagojo.com", "www.mangagojo.com"),
    },
    {
        "name": "manhuascanus",
        "display_name": "ManhuaScanUS",
        "base_url": "https://manhuascan.us",
        "domains": ("manhuascan.us", "www.manhuascan.us"),
    },
    {
        "name": "manhwax",
        "display_name": "ManhwaX",
        "base_url": "https://manhwax.top",
        "domains": ("manhwax.top", "www.manhwax.top", "manhwax.org", "www.manhwax.org"),
    },
    {
        "name": "nikatoons",
        "display_name": "NikaToons",
        "base_url": "https://nikatoons.com",
        "domains": ("nikatoons.com", "www.nikatoons.com"),
    },
    {
        "name": "noxenscans",
        "display_name": "NoxenScans",
        "base_url": "https://noxenscan.com",
        "domains": ("noxenscan.com", "www.noxenscan.com"),
    },
    {
        "name": "rackusreads",
        "display_name": "RackusReads",
        "base_url": "https://rackusreads.com",
        "domains": ("rackusreads.com", "www.rackusreads.com"),
    },
    {
        "name": "ragescans",
        "display_name": "RageScans",
        "base_url": "https://ragescans.com",
        "domains": ("ragescans.com", "www.ragescans.com"),
    },
    {
        "name": "ravenscans",
        "display_name": "RavenScans",
        "base_url": "https://ravenscans.com",
        "domains": ("ravenscans.com", "www.ravenscans.com"),
    },
    {
        "name": "razure",
        "display_name": "Razure",
        "base_url": "https://razure.org",
        "domains": ("razure.org", "www.razure.org"),
    },
    {
        "name": "restscans",
        "display_name": "RestScans",
        "base_url": "https://restscans.com",
        "domains": ("restscans.com", "www.restscans.com"),
    },
    {
        "name": "rizzcomic",
        "display_name": "RizzComic",
        "base_url": "https://rizzcomic.com",
        "domains": ("rizzcomic.com", "www.rizzcomic.com"),
    },
    {
        "name": "rokaricomics",
        "display_name": "RokariComics",
        "base_url": "https://rokaricomics.com",
        "domains": ("rokaricomics.com", "www.rokaricomics.com"),
    },
    {
        "name": "skymanga",
        "display_name": "SkyManga",
        "base_url": "https://skymanga.work",
        "domains": ("skymanga.work", "www.skymanga.work"),
    },
    {
        "name": "tercoscans",
        "display_name": "TercoScans",
        "base_url": "https://tecnoxmoon.xyz",
        "domains": ("tecnoxmoon.xyz", "www.tecnoxmoon.xyz", "tecnocomic1.xyz", "www.tecnocomic1.xyz"),
    },

    {
        "name": "witchscans",
        "display_name": "WitchScans",
        "base_url": "https://witchscans.com",
        "domains": ("witchscans.com", "www.witchscans.com"),
    },
]
