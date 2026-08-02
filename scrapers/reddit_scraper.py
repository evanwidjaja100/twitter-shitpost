"""Reddit scraper via PRAW (free personal tier, 100 req/min)."""

import logging
import time

log = logging.getLogger("reddit")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _gallery_first_image(post) -> str | None:
    try:
        meta = post.media_metadata or {}
        if not meta:
            return None
        first_id = next(iter(meta))
        url = meta[first_id]["s"]["u"]
        return url.replace("&amp;", "&")
    except Exception:
        return None


def scrape(reddit, config: dict) -> list[dict]:
    """Return list of candidate items from configured subreddits."""
    items: list[dict] = []
    block_nsfw = config.get("block_nsfw", True)
    min_score = config.get("min_score", 3000)
    limit = config.get("limit_per_subreddit", 25)
    blocked = config.get("blocked_keywords", [])

    for name in config.get("subreddits", []):
        try:
            sub = reddit.subreddit(name)
            for post in sub.hot(limit=limit):
                if post.stickied:
                    continue
                if block_nsfw and post.over_18:
                    continue
                if post.score < min_score:
                    continue
                if any(kw.lower() in post.title.lower() for kw in blocked if kw):
                    continue
                if post.crosspost_parent_list:
                    continue

                item = {
                    "source": "reddit",
                    "source_id": post.id,
                    "source_url": f"https://www.reddit.com{post.permalink}",
                    "title": post.title,
                    "media_url": None,
                    "media_path": None,
                    "score": float(post.score),
                    "created_utc": float(post.created_utc),
                    "nsfw": bool(post.over_18),
                    "kind": None,
                }

                url = (post.url or "").lower()
                if post.is_video and post.media:
                    item["kind"] = "video"
                    item["media_url"] = f"https://www.reddit.com{post.permalink}"
                    item["title"] = post.title
                elif post.is_gallery:
                    first = _gallery_first_image(post)
                    if not first:
                        continue
                    item["kind"] = "image"
                    item["media_url"] = first
                elif url.endswith(IMAGE_EXTS):
                    item["kind"] = "image"
                    item["media_url"] = post.url
                else:
                    continue  # external links, text posts

                items.append(item)
        except Exception as e:
            log.warning("subreddit %s failed: %s", name, e)
        time.sleep(1.0)
    return items
