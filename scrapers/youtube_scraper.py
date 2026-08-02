"""YouTube scraper via Data API v3 (free quota).

Uses uploads playlists (playlistItems.list = 1 unit/call) instead of search
(100 units/call) to stay far inside the 10k units/day limit.
"""

import logging
import time

log = logging.getLogger("youtube")


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
