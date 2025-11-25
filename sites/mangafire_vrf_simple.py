"""Simple VRF token generator for MangaFire using Playwright.

This implementation captures VRF tokens by letting the page naturally generate them,
then intercepting the network requests to extract the VRF parameter.
"""
from __future__ import annotations

import atexit
import re
from typing import Optional, Dict
from urllib.parse import parse_qs, urlparse

try:
    from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class SimpleMangaFireVRFGenerator:
    """Generates VRF tokens by capturing them from actual page requests."""

    def __init__(self):
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "Playwright is required. Install with: pip install playwright && playwright install chromium"
            )

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._initialized = False

        # Cache VRF tokens based on URL patterns
        self._vrf_cache: Dict[str, str] = {}

        atexit.register(self.close)

    def _initialize(self, init_url: Optional[str] = None) -> None:
        """Initialize the browser context.

        Args:
            init_url: Optional URL to load for initialization. If not provided,
                     uses a fallback manga page.
        """
        if self._initialized:
            return

        print("[*] Initializing MangaFire VRF generator...")

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        )
        self._page = self._context.new_page()

        # Capture network requests with VRF parameters
        self._captured_vrfs = []

        def handle_request(request):
            url = request.url
            if 'vrf=' in url:
                parsed = urlparse(url)
                query = parse_qs(parsed.query)
                if 'vrf' in query:
                    vrf = query['vrf'][0]
                    path = parsed.path
                    self._vrf_cache[path] = vrf
                    self._captured_vrfs.append({
                        'path': path,
                        'vrf': vrf,
                        'full_url': url
                    })
                    print(f"[+] Captured VRF for {path}: {vrf[:30]}...")

        self._page.on('request', handle_request)

        # Load a MangaFire page to initialize the JS environment
        # Use the provided URL or fall back to a known working page
        if not init_url:
            init_url = "https://mangafire.to/read/hoegwija-sayongseolmyeongseo22.jjx8y/en/chapter-1"

        print(f"[*] Loading page to capture VRF tokens: {init_url}")

        try:
            self._page.goto(init_url, wait_until='networkidle', timeout=60000)
            self._page.wait_for_timeout(2000)  # Wait for async requests
        except Exception as e:
            print(f"[!] Warning: {e}")

        print(f"[+] Captured {len(self._captured_vrfs)} VRF tokens")
        self._initialized = True

    def _navigate_and_capture(self, page_url: str, expected_ajax_path: str) -> None:
        """Navigate to a page and wait to capture VRF for a specific AJAX endpoint.

        Args:
            page_url: Full URL to navigate to (e.g., chapter reader page)
            expected_ajax_path: The AJAX path we're trying to capture VRF for
        """
        if not self._initialized:
            self._initialize()

        try:
            print(f"[*] Navigating to {page_url} to capture VRF...")
            self._page.goto(page_url, wait_until='domcontentloaded', timeout=30000)
            self._page.wait_for_timeout(3000)  # Wait for AJAX calls

            if expected_ajax_path in self._vrf_cache:
                print(f"[+] Successfully captured VRF for {expected_ajax_path}")
            else:
                print(f"[!] Navigation completed but VRF for {expected_ajax_path} not captured")

        except Exception as e:
            print(f"[!] Navigation error: {e}")

    def generate_vrf(self, url_path: str, init_url: Optional[str] = None) -> str:
        """Generate VRF for a URL path.

        Since we can't easily call the Ph function directly, we use a cached approach:
        1. Check if we have a similar URL cached
        2. If not, try to trigger the page to generate it
        3. Fall back to requesting it via a new page load

        Args:
            url_path: Path like '/ajax/read/jjx8y/chapter/en'
            init_url: Optional URL to use for browser initialization

        Returns:
            VRF token string
        """
        if not self._initialized:
            self._initialize(init_url)

        # Check cache first
        if url_path in self._vrf_cache:
            print(f"[+] Using cached VRF for {url_path}")
            return self._vrf_cache[url_path]

        # For chapter list URLs, try to trigger it by navigating
        # Extract manga ID from path if possible
        match = re.match(r'/ajax/read/([^/]+)/chapter/([^/]+)', url_path)
        if match:
            manga_id = match.group(1)
            lang = match.group(2)

            # VRF tokens are URL-specific, so we need to navigate to the actual manga
            # to trigger the request and capture its VRF
            # Try multiple URL patterns to find the manga
            url_patterns = [
                f"https://mangafire.to/read/manga.{manga_id}/{lang}/chapter-1",
                f"https://mangafire.to/read/title.{manga_id}/{lang}/chapter-1",
                f"https://mangafire.to/read/unknown.{manga_id}/{lang}/chapter-1",
            ]

            for target_url in url_patterns:
                print(f"[*] Trying {target_url} to capture VRF...")

                try:
                    self._page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
                    self._page.wait_for_timeout(3000)  # Wait for AJAX calls

                    # Check if we captured the exact path we need
                    if url_path in self._vrf_cache:
                        print(f"[+] Successfully captured VRF for {url_path}")
                        return self._vrf_cache[url_path]

                except Exception as e:
                    print(f"[!] Failed with {target_url}: {e}")
                    continue

            # If we still don't have it, check if we got any similar VRF from the attempts
            if url_path in self._vrf_cache:
                return self._vrf_cache[url_path]

        # Check for chapter-specific URLs
        match = re.match(r'/ajax/read/chapter/(\d+)', url_path)
        if match:
            chapter_id = match.group(1)

            # For chapter images, we can't easily trigger the exact request
            # But VRF tokens for /ajax/read/chapter/{id} appear to follow a pattern
            # Try to generate by navigating to the chapter reader
            # However, we don't know the manga ID, so we can't construct the reader URL

            # Try to find a similar cached token (same endpoint type)
            # Note: This might not work due to VRF being chapter-specific
            for cached_path, vrf in self._vrf_cache.items():
                if '/ajax/read/chapter/' in cached_path:
                    cached_id = cached_path.split('/')[-1]
                    # Check if IDs are close (sequential chapters might have similar VRFs)
                    try:
                        if abs(int(cached_id) - int(chapter_id)) < 5:
                            print(f"[+] Using VRF from nearby chapter {cached_id} for chapter {chapter_id}")
                            return vrf
                    except ValueError:
                        pass

                    # Fallback: use any cached chapter VRF
                    print(f"[!] Warning: Using VRF from different chapter ({cached_path}), may fail")
                    return vrf

        raise RuntimeError(f"Could not generate VRF for {url_path}. No cached token available.")

    def close(self) -> None:
        """Clean up resources."""
        try:
            if self._page:
                self._page.close()
                self._page = None
        except Exception:
            pass

        try:
            if self._context:
                self._context.close()
                self._context = None
        except Exception:
            pass

        try:
            if self._browser:
                self._browser.close()
                self._browser = None
        except Exception:
            pass

        try:
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
        except Exception:
            pass

        self._initialized = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Singleton instance
_vrf_generator: Optional[SimpleMangaFireVRFGenerator] = None


def get_vrf_generator() -> SimpleMangaFireVRFGenerator:
    """Get or create the global VRF generator."""
    global _vrf_generator
    if _vrf_generator is None:
        _vrf_generator = SimpleMangaFireVRFGenerator()
    return _vrf_generator


def generate_vrf_token(url_path: str) -> str:
    """Generate a VRF token for the given URL path."""
    generator = get_vrf_generator()
    return generator.generate_vrf(url_path)
