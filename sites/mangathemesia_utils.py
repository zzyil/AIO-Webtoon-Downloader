"""Helper utilities for MangaThemesia sites."""

import json
import re
from typing import Dict, List, Optional


_TS_READER_RE = re.compile(r"ts_reader\.run\((\{.*?\})\)", re.DOTALL)


def _balanced_ts_reader_payload(html: str) -> Optional[str]:
    marker = "ts_reader.run("
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(html)):
        ch = html[idx]
        if ch == '"' and not escape:
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return html[start : idx + 1]
        escape = ch == "\\" and not escape and in_string
    return None


def extract_ts_reader_payload(html: str) -> Optional[Dict]:
    """Return the parsed JSON payload from a ts_reader.run(...) call."""
    raw_json = None
    match = _TS_READER_RE.search(html)
    if match:
        raw_json = match.group(1)
        try:
            return json.loads(_normalize_ts_json(raw_json))
        except json.JSONDecodeError:
            raw_json = None

    if raw_json is None:
        raw_json = _balanced_ts_reader_payload(html)
        if not raw_json:
            return None
        try:
            return json.loads(_normalize_ts_json(raw_json))
        except json.JSONDecodeError:
            return None


def _normalize_ts_json(raw: str) -> str:
    """Convert common JS literals (e.g. !0) into valid JSON."""
    return raw.replace("!0", "true").replace("!1", "false")


def extract_ts_reader_images(html: str, payload: Optional[Dict] = None) -> List[str]:
    """
    Extract image URLs from ts_reader.run() JavaScript call.
    
    Args:
        html: HTML content containing ts_reader.run() call
        payload: Optional pre-parsed ts_reader payload to avoid re-parsing.
        
    Returns:
        List of image URLs, or empty list if not found
    """
    data = payload or extract_ts_reader_payload(html)
    if not data:
        return []

    try:
        sources = data.get("sources") or []
        if not isinstance(sources, list) or not sources:
            return []
        first = sources[0]
        images = first.get("images")
        if isinstance(images, list):
            return images
    except (KeyError, IndexError, AttributeError):
        return []

    return []
