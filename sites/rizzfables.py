from __future__ import annotations

import requests

from .mangathemesia import MangaThemesiaSiteHandler


class RizzFablesSiteHandler(MangaThemesiaSiteHandler):
    """Custom MangaThemesia handler that avoids Cloudscraper TLS failures."""

    def __init__(self) -> None:
        super().__init__(
            name="rizzfables",
            display_name="RizzFables",
            base_url="https://rizzfables.com",
            domains=("rizzfables.com", "www.rizzfables.com"),
            use_playwright=False,
        )
        self._plain_session = requests.Session()

    def configure_session(self, scraper, args) -> None:
        """
        Cloudsraper cannot establish TLS with rizzfables.com. We keep a private
        requests.Session, copy any cookies, and monkey-patch the scraper object
        so that every downstream call uses the plain requests session instead.
        """
        session = self._plain_session
        session.headers.update({"Referer": f"{self.base_url}/"})
        session.cookies.update(scraper.cookies)
        session.verify = True

        scraper.headers = session.headers
        scraper.cookies = session.cookies
        scraper.get = session.get
        scraper.post = session.post
        scraper.request = session.request
        scraper.verify = session.verify


__all__ = ["RizzFablesSiteHandler"]
