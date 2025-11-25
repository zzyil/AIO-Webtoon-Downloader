import urllib3
from .mangathemesia import MangaThemesiaSiteHandler

class VioletScansSiteHandler(MangaThemesiaSiteHandler):
    name = "violetscans"
    display_name = "VioletScans"
    base_url = "https://violetscans.org"
    domains = ("violetscans.org", "www.violetscans.org")
    
    def __init__(self, *args, **kwargs):
        # Initialize with specific settings for VioletScans
        super().__init__(
            name=self.name,
            display_name=self.display_name,
            base_url=self.base_url,
            domains=self.domains,
            use_playwright=True,
            verify_ssl=False,
            *args, **kwargs
        )

    def configure_session(self, scraper, args) -> None:
        # Call parent to set up headers and basic SSL verification setting
        super().configure_session(scraper, args)
        
        # Explicitly suppress warnings for this session if verification is disabled
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
