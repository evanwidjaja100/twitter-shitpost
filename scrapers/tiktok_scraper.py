"""TikTok scraper — browses TikTok with the bot's browser, no API, no approval.

Two modes, driven by config (they are ADDITIVE — both run when enabled):
  * Feed mode     — scroll the general "For You" feed (no accounts needed).
  * Account mode  — visit curated @handles' profile grids (niche content).

Both return the same item schema; main.py downloads each video downstream via
yt-dlp. All selectors live here so they're a single point of maintenance.

Metadata correctness: every feed video's like count, description and URL are
read from the SAME card/container element, never from page-global ``.first``
selectors, so candidates in one feed cannot exchange metadata.
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

# A logical feed card: the container that owns one video link + its own
# like-count + description together. Kept as the single scoping selector so
# per-video metadata never leaks across cards.
_CARD = '[data-e2e="feed-item"], [data-e2e="video-card"]'


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


def _extract_feed_card(card) -> dict | None:
    """Extract one logical feed/source entry STRICTLY from its own card element.

    The video link, like count and description are all read from the SAME card
    locator, so two videos in one feed can never exchange metadata. A card
    missing any part yields an empty value for that part (it never borrows
    another card's).
    """
    try:
        a = card.locator('a[href*="/video/"]').first
        href = a.get_attribute("href") or "" if a.count() else ""
        if not _VIDEO_LINK_RE.search(href):
            return None

        likes = 0
        lik = card.locator('[data-e2e="like-count"]').first
        if lik.count():
            likes = _parse_count(lik.inner_text(timeout=1500))

        caption = ""
        desc = card.locator('[data-e2e="video-card-desc"], [data-e2e="video-desc"]').first
        if desc.count():
            caption = desc.inner_text(timeout=1500).strip()[:200]

        return _card(href, likes, caption)
    except Exception as e:
        log.debug("tiktok feed card parse skipped: %s", e)
        return None


def _collect_cards(page, limit: int) -> list[dict]:
    """Return [{'id','href','likes','caption'}...] for visible video cards.

    Every card is scoped: its link, like count and description are read from
    the SAME container, so metadata cannot attach to the wrong video.
    """
    cards: list[dict] = []
    seen: set[str] = set()
    for el in page.locator(_CARD).all():
        item = _extract_feed_card(el)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        cards.append(item)
        if len(cards) >= limit:
            break
    return cards


def _collect_feed(page, max_posts: int, scrolls: int) -> list[dict]:
    """Scroll the For You feed, collecting per-card metadata for each video."""
    cards: list[dict] = []
    seen: set[str] = set()
    probes = max(scrolls * _FEED_SCROLL_MULTIPLIER, 8)
    for _ in range(probes):
        for el in page.locator(_CARD).all():
            item = _extract_feed_card(el)
            if item is None or item["id"] in seen:
                continue
            seen.add(item["id"])
            cards.append(item)
            if len(cards) >= max_posts:
                return cards
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

    For You and configured accounts are treated as ADDITIVE — matching the
    documented behavior (README: accounts are "used when `foryou` is `false`,
    or in addition"): when ``foryou`` is enabled the general feed is collected
    AND every configured account is browsed; the final list is deduplicated by
    video id. Empty accounts + ``foryou=false`` falls back to the feed.
    """
    accounts = [a for a in config.get("accounts", []) if a]
    foryou = bool(config.get("foryou", False))
    min_likes = int(config.get("min_likes", 0))
    max_posts = int(config.get("max_posts_per_account", 10))
    scrolls = int(config.get("scrolls", 3))

    items: list[dict] = []
    seen_ids: set[str] = set()
    context = session._context
    page = context.new_page()
    try:
        if foryou or not accounts:
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

        if not accounts:
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