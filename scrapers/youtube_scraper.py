"""YouTube scraper.

Two ways to pull candidates:
  * Shorts feed  — browser-scrapes the YouTube "Shorts" shelf on the home page
                   (like the TikTok For You feed). No API key, no channels.
  * Channels     — Data API v3 for a configured channel's uploads playlist
                   (playlistItems.list = 1 unit/call instead of search=100),
                   requires `secrets.youtube_api_key`.
"""

import logging
import random
import re
import time

log = logging.getLogger("youtube")

_SHORTS_RE = re.compile(r"/shorts/([\w-]{6,})")


def _parse_views(text) -> int:
    """'1.2M views' -> 1200000, '340K views' -> 340000, '5 views' -> 5."""
    m = re.search(r"([\d.]+)\s*([KMB]?)\s*views", text or "", re.IGNORECASE)
    if not m:
        return 0
    num = float(m.group(1))
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
        m.group(2).upper(), 1
    )
    return int(num * mult)


def scrape_shorts(session, config: dict) -> list[dict]:
    """Browser-scrape the Shorts shelf (general feed, no API key needed)."""
    max_items = int(config.get("max_items_per_channel", 10))
    min_views = int(config.get("min_views", 0))
    scrolls = int(config.get("scrolls", 4))

    items: list[dict] = []
    seen: set[str] = set()
    context = session._context
    page = context.new_page()
    try:
        page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        for _ in range(scrolls):
            try:
                page.mouse.wheel(0, 2600)
            except Exception:
                pass
            page.wait_for_timeout(random.randint(1500, 2500))

        for a in page.locator('a[href*="/shorts/"]').all():
            try:
                href = a.get_attribute("href") or ""
                m = _SHORTS_RE.search(href)
                if not m or href in seen:
                    continue
                seen.add(href)
                vid = m.group(1)
                views = 0
                title = f"youtube short {vid}"
                try:
                    card = a.locator("xpath=ancestor::ytd-rich-grid-media[1]")
                    if card.count():
                        txt = card.first.inner_text(timeout=1500)
                        views = _parse_views(txt)
                        t = card.first.locator("#video-title").first
                        if t.count():
                            title = t.inner_text(timeout=1500).strip()[:200]
                except Exception:
                    pass
                if views < min_views:
                    continue
                items.append(
                    {
                        "source": "youtube",
                        "source_id": vid,
                        "source_url": f"https://www.youtube.com/shorts/{vid}",
                        "title": title,
                        "media_url": None,
                        "media_path": None,
                        "score": float(views),
                        "created_utc": time.time(),
                        "nsfw": False,
                        "kind": "video",
                    }
                )
            except Exception as e:
                log.debug("shorts parse skipped: %s", e)
            if len(items) >= max_items:
                break
    finally:
        try:
            page.close()
        except Exception:
            pass
    return items



def _resolve_uploads_id(yt, channel_cfg: dict) -> str | None:
    playlist_id = (channel_cfg.get("playlist_id") or "").strip()
    if playlist_id:
        return playlist_id
    handle = (channel_cfg.get("handle") or "").strip().lstrip("@")
    if not handle:
        return None
    try:
        resp = (
            yt.channels()
            .list(part="contentDetails", forHandle=handle)
            .execute()
        )
        items = resp.get("items", [])
        if not items:
            log.warning("channel %s not found", handle)
            return None
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception as e:
        log.warning("resolve failed for %s: %s", handle, e)
        return None


def scrape(yt, config: dict) -> list[dict]:
    items: list[dict] = []
    min_views = config.get("min_views", 100000)
    max_age_days = config.get("max_age_days", 21)
    max_items = config.get("max_items_per_channel", 10)
    max_minutes = config.get("max_source_video_minutes", 8)
    cutoff = time.time() - max_age_days * 86400

    for channel_cfg in config.get("channels", []):
        uploads_id = _resolve_uploads_id(yt, channel_cfg)
        if not uploads_id:
            continue
        try:
            resp = (
                yt.playlistItems()
                .list(part="snippet,contentDetails", playlistId=uploads_id, maxResults=max_items)
                .execute()
            )
            video_ids = [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
            if not video_ids:
                continue
            details = (
                yt.videos()
                .list(part="snippet,contentDetails,statistics", id=",".join(video_ids))
                .execute()
            )
            for vid in details.get("items", []):
                views = int(vid["statistics"].get("viewCount", 0))
                if views < min_views:
                    continue
                published = vid["snippet"]["publishedAt"]
                try:
                    from datetime import datetime, timezone

                    pub = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if pub < cutoff:
                    continue
                duration = vid["contentDetails"]["duration"]
                if not _duration_within_minutes(duration, max_minutes):
                    continue
                items.append(
                    {
                        "source": "youtube",
                        "source_id": vid["id"],
                        "source_url": f"https://www.youtube.com/watch?v={vid['id']}",
                        "title": vid["snippet"]["title"],
                        "media_url": None,
                        "media_path": None,
                        "score": float(views),
                        "created_utc": pub,
                        "nsfw": False,
                        "kind": "video",
                    }
                )
        except Exception as e:
            log.warning("playlist fetch failed for %s: %s", channel_cfg, e)
        time.sleep(1.0)
    return items


def _duration_within_minutes(iso_duration: str, max_minutes: int) -> bool:
    import re

    m = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration or "PT0S"
    )
    if not m:
        return False
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    secs = int(m.group(3) or 0)
    return hours * 60 + mins + (1 if secs else 0) <= max_minutes
