from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
import tempfile
import shutil
import os
import threading
import time
import uuid
from sites import get_handler_by_name, get_handler_for_url
import sites
from sites.base import SiteComicContext
import requests

app = FastAPI()

# Allow CORS for all origins (customize as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = os.path.join(tempfile.gettempdir(), "aio_webtoon_api")
os.makedirs(TEMP_DIR, exist_ok=True)

# Cleanup thread for temp files
def cleanup_temp_files():
    while True:
        now = time.time()
        for fname in os.listdir(TEMP_DIR):
            fpath = os.path.join(TEMP_DIR, fname)
            if os.path.isfile(fpath):
                try:
                    # Remove files older than 1 hour
                    if now - os.path.getmtime(fpath) > 3600:
                        os.remove(fpath)
                except Exception:
                    pass
        time.sleep(600)  # Check every 10 minutes

threading.Thread(target=cleanup_temp_files, daemon=True).start()

def get_scraper():
    # Use cloudscraper if available, else requests.Session
    try:
        import cloudscraper
        return cloudscraper.create_scraper()
    except Exception:
        return requests.Session()

@app.get("/api/handlers")
def list_handlers():
    handlers = []
    for handler in getattr(sites, "_REGISTERED_HANDLERS", []):
        # Try to get a base_url or domains for each handler
        base_url = getattr(handler, "base_url", None)
        domains = getattr(handler, "domains", None)
        handlers.append({
            "name": getattr(handler, "name", None),
            "display_name": getattr(handler, "display_name", None),
            "base_url": base_url,
            "domains": list(domains) if domains else None
        })
    return handlers

@app.get("/api/info")
def get_comic_info(url: str, site: Optional[str] = None):
    handler = get_handler_by_name(site) if site else get_handler_for_url(url)
    if not handler:
        raise HTTPException(404, "No handler found for this site")
    scraper = get_scraper()
    context = handler.fetch_comic_context(url, scraper, lambda u, s=scraper: s.get(u))
    # Return all available info
    return context.comic

@app.get("/api/chapters")
def get_chapters(url: str, site: Optional[str] = None, language: str = "en", type: str = "chapter"):
    handler = get_handler_by_name(site) if site else get_handler_for_url(url)
    if not handler:
        raise HTTPException(404, "No handler found for this site")
    scraper = get_scraper()
    context = handler.fetch_comic_context(url, scraper, lambda u, s=scraper: s.get(u))
    if type == "volume":
        items = handler.get_volumes(context, scraper, language, lambda u, s=scraper: s.get(u))
    else:
        items = handler.get_chapters(context, scraper, language, lambda u, s=scraper: s.get(u))
    return items

@app.get("/api/chapter_images")
def get_chapter_images(url: str, chapter_id: str, site: Optional[str] = None):
    handler = get_handler_by_name(site) if site else get_handler_for_url(url)
    if not handler:
        raise HTTPException(404, "No handler found for this site")
    scraper = get_scraper()
    context = handler.fetch_comic_context(url, scraper, lambda u, s=scraper: s.get(u))
    chapters = handler.get_chapters(context, scraper, "en", lambda u, s=scraper: s.get(u))
    chapter = next((c for c in chapters if str(c.get("id")) == chapter_id), None)
    if not chapter:
        raise HTTPException(404, "Chapter not found")
    images = handler.get_chapter_images(chapter, scraper, lambda u, s=scraper: s.get(u))
    return {"images": images}

@app.get("/api/download_image")
def download_image(url: str):
    # Download image to temp dir and return local URL
    ext = os.path.splitext(url)[1] or ".jpg"
    fname = f"img_{uuid.uuid4().hex}{ext}"
    fpath = os.path.join(TEMP_DIR, fname)
    scraper = get_scraper()
    try:
        r = scraper.get(url, stream=True, timeout=30)
        r.raise_for_status()
        with open(fpath, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
    except Exception as e:
        raise HTTPException(500, f"Failed to download image: {e}")
    # Return a local URL for the image
    return {"url": f"/api/temp/{fname}"}

@app.get("/api/temp/{filename}")
def serve_temp_file(filename: str):
    fpath = os.path.join(TEMP_DIR, filename)
    if not os.path.isfile(fpath):
        raise HTTPException(404, "File not found")
    return FileResponse(fpath)

# Example search route (if you want to implement search by title, etc.)
@app.get("/api/search")
def search_comics(query: str):
    # This is a placeholder. Actual implementation depends on available handlers.
    return JSONResponse({"error": "Search not implemented. Use /api/info with a direct URL."})
