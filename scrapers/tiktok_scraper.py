"""TikTok scraper — browses TikTok with the bot's browser, no API, no approval.

Two modes, driven by config:
  * Account mode  — visit curated @handles' profile grids (niche content).
  * Feed mode     — scroll the general "For You" feed (no accounts needed).

Both return the same item schema; main.py downloads each video downstream via
yt-dlp. All selectors live here so they're a single point of maintenance.
"""

import logging
import random
import re
import time

log = logging.getLogger("tiktok")

_VIDEO_LINK_RE = re.compile(r"/(?:@[^/]+/)?video/(\d+)")

# Feed mode loads roughly one video per viewport and virtualizes the list, so we
# scroll generously and stop once we've collected enough unique videos.
_FEED_SCROLL_MULTIPLIER = 3


def _parse_count(text) -> int:
    """'12.4K' -> 12400, '1.2M' -> 1200000, '5' -> 5."""
    text = (text or "").strip()
    try:
        mult = 1.0
        if text.endswith("K"):
            mult, text = 1_000.0, text[:-1]
        elif text.endswith("M"):
            mult, text = 1_000_000.0, text[:-1]
        elif text.endswith("B"):
            mult, text = 1_000_000_000.0, text[:-1]
        return int(float(text) * mult)
    except (ValueError, TypeError):
        return 0


def _card(href: str, likes: int, caption: str) -> dict:
    m = _VIDEO_LINK_RE.search(href)
    full = href if href.startswith("http") else f"https://www.tiktok.com{href}"
    return {
        "id": m.group(1) if m else full,
        "href": full,
        "likes": likes,
        "caption": caption,
    }


def _collect_cards(page, limit: int) -> list[dict]:
    """Return [{'id','href','likes','caption'}...] for visible video cards.

    Profile-grid mode: each card renders one <a href=\".../video/ID\"> and one
    like badge (data-e2e=\"like-count\") in the same DOM order, so we zip them.
    Unreadable counts fall back to 0 (still postable when min_likes is 0).
    """
    links: list[dict] = []
    seen: set[str] = set()
    for a in page.locator('a[href*="/video/"]').all():
        try:
            href = a.get_attribute("href") or ""
            if not _VIDEO_LINK_RE.search(href) or href in seen:
                continue
            seen.add(href)
            links.append(href)
        except Exception as e:
            log.debug("tiktok link parse skipped: %s", e)

    like_counts: list[int] = []
    for el in page.locator('[data-e2e="like-count"]').all():
        try:
            like_counts.append(_parse_count(el.inner_text(timeout=1000)))
        except Exception:
            like_counts.append(0)

    captions: list[str] = []
    for el in page.locator('[data-e2e="video-card-desc"]').all():
        try:
            captions.append(el.inner_text(timeout=1000).strip()[:200])
        except Exception:
            captions.append("")

    return [
        _card(
            href,
            like_counts[i] if i < len(like_counts) else 0,
            captions[i] if i < len(captions) else "",
        )
        for i, href in enumerate(links[: limit * 4])
    ]


def _collect_feed(page, max_posts: int, scrolls: int) -> list[dict]:
    """Scroll the For You feed, collecting one link + like count per viewport."""
    cards: list[dict] = []
    seen: set[str] = set()
    probes = max(scrolls * _FEED_SCROLL_MULTIPLIER, 8)
    for _ in range(probes):
        for a in page.locator('a[href*="/video/"]').all():
            try:
                href = a.get_attribute("href") or ""
                if not _VIDEO_LINK_RE.search(href) or href in seen:
                    continue
                seen.add(href)
                likes = 0
                caption = ""
                try:
                    lik = page.locator('[data-e2e="like-count"]').first
                    if lik.count():
                        likes = _parse_count(lik.inner_text(timeout=1500))
                except Exception:
                    pass
                try:
                    desc = page.locator('[data-e2e="video-desc"]').first
                    if desc.count():
                        caption = desc.inner_text(timeout=1500).strip()[:200]
                except Exception:
                    pass
                cards.append(_card(href, likes, caption))
            except Exception as e:
                log.debug("tiktok feed parse skipped: %s", e)
        if len(cards) >= max_posts:
            break
        try:
            page.mouse.wheel(0, 3000)
        except Exception:
            pass
        page.wait_for_timeout(random.randint(1800, 2800))
    return cards


def _to_item(card: dict, handle: str, min_likes: int) -> dict | None:
    if card["likes"] < min_likes:
        return None
    return {
        "source": "tiktok",
        "source_id": card["id"],
        "source_url": card["href"],
        "title": card["caption"] or (f"tiktok @{handle}" if handle else "tiktok"),
        "media_url": None,
        "media_path": None,
        "score": float(card["likes"]),
        "created_utc": time.time(),
        "nsfw": False,
        "kind": "video",
    }


def scrape(session, config: dict, assets_dir: str) -> list[dict]:
    """Collect top video candidates from TikTok.

    Scrapes the general For You feed when `foryou` is enabled OR no accounts are
    configured; otherwise browses each configured @handle's profile grid.
    """
    accounts = config.get("accounts", [])
    foryou = bool(config.get("foryou", False))
    use_feed = foryou or not accounts
    min_likes = int(config.get("min_likes", 0))
    max_posts = int(config.get("max_posts_per_account", 10))
    scrolls = int(config.get("scrolls", 3))

    items: list[dict] = []
    seen_ids: set[str] = set()
    context = session._context
    page = context.new_page()
    try:
        if use_feed:
            try:
                page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
            except Exception as e:
                log.warning("tiktok feed load failed: %s", e)
                return items
            for card in _collect_feed(page, max_posts, scrolls):
                if card["id"] in seen_ids:
                    continue
                item = _to_item(card, "", min_likes)
                if item is None:
                    continue
                seen_ids.add(card["id"])
                items.append(item)
            return items

        for handle in accounts:
            handle = (handle or "").strip().lstrip("@")
            if not handle:
                continue
            try:
                page.goto(
                    f"https://www.tiktok.com/@{handle}",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                page.wait_for_timeout(4000)
                for _ in range(scrolls):
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(random.randint(2000, 3200))
            except Exception as e:
                log.warning("profile %s failed: %s", handle, e)
                continue

            collected = 0
            for card in _collect_cards(page, max_posts):
                if card["id"] in seen_ids:
                    continue
                item = _to_item(card, handle, min_likes)
                if item is None:
                    continue
                seen_ids.add(card["id"])
                items.append(item)
                collected += 1
                if collected >= max_posts:
                    break
    finally:
        try:
            page.close()
        except Exception:
            pass
    return items

