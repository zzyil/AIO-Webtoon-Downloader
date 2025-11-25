import time
try:
    from playwright.sync_api import sync_playwright
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
