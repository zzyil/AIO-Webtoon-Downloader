from __future__ import annotations

from typing import Dict, Optional, Tuple

from .mangathemesia import MangaThemesiaSiteHandler


class RizzComicSiteHandler(MangaThemesiaSiteHandler):
    """Dedicated rizzcomic handler. Inherits MangaThemesia framework behavior
    (search, fetch_comic_context, get_chapters, get_chapter_images) but
    overrides the image-quality probe paths to NEVER fall back to cover art.

    Why: rizzcomic.com's CDN (rizzchoros.cloud) is currently 'cooked' —
    real chapter downloads frequently fail with throttle/timeout/host-poison
    even when the v3 multi-page aggregate probe falls back to scoring the
    cover. The cover happens to be a high-resolution 3023px JPEG that scores
    ~0.85 on the standard pixel-quality metrics, which gives rizzcomic a
    falsely-high rank in --search rankings (it'd ride the cover score up to
    rank #1 even though the user can't actually download a chapter from it).

    User feedback (2026-05-07): "Add an exception solely for rizzcomic to
    never use cover art for img_q, it's cooked."

    Override behavior:
    - `_probe_chapter_aggregate`: when the parent returns None (hard
      failure: fetch_comic_context / get_chapters / get_chapter_images
      raised an exception), we convert that to (0.0, samples=0/0) so the
      orchestrator caches a measured 0.0 instead of leaving the score
      un-measured (which would fall back to seed_quality=0.85 and again
      mis-rank the site).
    - `_probe_cover_image`: always returns None. Belt-and-suspenders —
      even if the orchestrator's cover-fallback path somehow fires, no
      cover bytes are produced for rizzcomic.

    Net effect: rizzcomic's img_quality_score is ALWAYS measured and ALWAYS
    grounded in real chapter-image fetches. Successful 5/5 probes preserve
    a real high score; everything else (5/5-failed, 0/5-attempted, hard
    failure) bottoms out at 0.0 and ranks last in the comparator. The
    comparator's _quality_for() check (`is not None`) treats the 0.0 as
    measured and uses it as the rank input — see search_system.md
    "Comparator" section.

    Cross-file: registered in sites/__init__.py:_BASE_HANDLERS, which
    causes the auto-registered MangaThemesia entry for rizzcomic in
    sites/mangathemesia_sites.py to be skipped via
    _MT_DEDICATED_NAMES dedup (name="rizzcomic" wins).
    """

    def __init__(self) -> None:
        super().__init__(
            name="rizzcomic",
            display_name="RizzComic",
            base_url="https://rizzcomic.com",
            domains=("rizzcomic.com", "www.rizzcomic.com"),
        )

    def _probe_chapter_aggregate(
        self, hit, scraper, make_request,
        max_samples: Optional[int] = None,
    ) -> Optional[Tuple[float, Dict]]:
        result = super()._probe_chapter_aggregate(
            hit, scraper, make_request, max_samples=max_samples,
        )
        if result is None:
            # Parent returned None — hard failure before even the fetch loop
            # could run (no chapter list / unreachable series page / parse
            # error). Without this override the orchestrator would fall back
            # to cover and the 3023px rizzcomic cover would score ~0.85,
            # camouflaging the broken-CDN reality. Force-record 0.0.
            return 0.0, {
                "width": 0,
                "height": 0,
                "format": "FAILED",
                "size_bytes": 0,
                "samples_attempted": 0,
                "samples_succeeded": 0,
                "cdn_reliability": 0.0,
            }

        # v5 throttle-probe-tail check (added 2026-05-17): when the breadth
        # phase happened to hit a healthy cache shard for every sampled
        # chapter, the breadth score can look fine (e.g. 0.7+) even though
        # the throttle-tail's 3 sequential follow-up fetches all failed —
        # i.e. the CDN serves first-page-per-chapter fine but throttles on
        # any subsequent request. That's the rizzchoros.cloud signature.
        # The base aggregate doesn't fold cdn_reliability into the score
        # (deliberately — a sleeping CDN that revives mid-probe shouldn't
        # crater a quality source). For rizzcomic specifically we cap the
        # composite at 0.1 when cdn_reliability==0 so the broken-CDN signal
        # dominates the orchestrator's per-chapter ranking even when the
        # breadth phase looked good.
        score, metadata = result
        cdn_reliability = metadata.get("cdn_reliability") if isinstance(metadata, dict) else None
        if cdn_reliability == 0.0 and score > 0.1:
            score = 0.1
            # Make the bottom-out visible in metadata for UI / debug.
            metadata = dict(metadata)
            metadata["outlier"] = "throttle_detected"
            result = (score, metadata)
        return result

    def _probe_cover_image(self, hit, scraper, make_request) -> Optional[bytes]:
        # Cover never used for rizzcomic — see class docstring. The
        # _probe_chapter_aggregate override above already prevents the
        # orchestrator from falling through to here for the high-seed
        # path, but this is the belt-and-suspenders guarantee for any
        # future code path that calls _probe_cover_image directly.
        return None


__all__ = ["RizzComicSiteHandler"]
