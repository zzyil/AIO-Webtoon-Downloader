import time
# Drop-in import of Patchright (Playwright fork with CDP-leak patches). Same
# API; the swap is transparent to call sites in this file. The public flag
# stays named PLAYWRIGHT_AVAILABLE (not PATCHRIGHT_*) because callers
# (aio_search_cli.py, sites/comix.py) grep that name — renaming would cascade
# through them without functional benefit.
try:
    from patchright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

def fetch_html_playwright(url: str, wait_selector: str = None, wait_time: int = 5) -> str:
    """
    Fetch HTML content using Playwright.
    
    Args:
        url: URL to fetch
        wait_selector: Optional CSS selector to wait for
        wait_time: Time to wait in seconds (default 5)
        
    Returns:
        HTML content string
    """
    # Embedder-supplied browser (Android's WebView) wins when one is installed.
    # Desktop never installs one, so the Patchright path below is unchanged —
    # including its ignore_https_errors and pinned UA, which some MangaThemesia
    # sites depend on. Cross-file: sites/browser_backend.py:custom_backend.
    from . import browser_backend as _bb

    _backend = _bb.custom_backend("fetch")
    if _backend is not None:
        _backend.goto(url, wait_until="domcontentloaded", timeout_ms=60000)
        if wait_selector:
            # Same "don't raise on timeout" contract as the wait below: the
            # selector may simply never render, and the caller still wants
            # whatever HTML did arrive.
            _backend.wait_for_selector(wait_selector, timeout_ms=10000)
        time.sleep(wait_time)  # let JS settle, mirroring wait_for_timeout below
        return _backend.content()

    if not PLAYWRIGHT_AVAILABLE:
        raise ImportError("Playwright is not available. Please install it.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            ignore_https_errors=True
        )
        page = context.new_page()
        
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10000)
                except:
                    pass # Continue if selector not found (maybe it's not there yet or never will be)
            
            # Always wait a bit for JS to settle
            page.wait_for_timeout(wait_time * 1000)
            
            content = page.content()
            return content
            
        finally:
            browser.close()
