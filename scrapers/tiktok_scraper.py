"""TikTok scraper — browses TikTok with the bot's browser, no API, no approval.

Two modes, driven by config (they are ADDITIVE — both run when enabled):
  * Feed mode     — scroll the general "For You" feed (no accounts needed).
  * Account mode  — visit curated @handles' profile grids (niche content).

Both return the same item schema; main.py downloads each video downstream via
yt-dlp. All selectors live in one module-level map so a TikTok DOM change is
fixed in one place.

Metadata correctness: every feed video's like count, description and URL are
read from the SAME card/container element, never from page-global ``.first``
selectors, so candidates in one feed can never exchange metadata. A card that
is missing any part yields an empty value for that part (it never borrows
another card's).

Duplicate videos: observations of the same video id (feed vs accounts, or a
repeated card) are merged via ``_merge_tiktok_candidate`` — the best valid like
count and the most complete caption win, regardless of discovery order, and
each video is emitted exactly once.

Resilience: an empty feed is a legitimate outcome; a page that exposes *none*
of the known card containers is diagnosed with a warning so selector drift is
visible instead of silent.
"""

import logging
import random
import re
import time

from scrapers._dom import first_matching_locator, iter_matching_nodes

log = logging.getLogger("tiktok")

# Ordered, conservative selectors. The first card selector is the current
# production one; later entries are safe alternatives. Everything is scoped:
# ``first_matching_locator`` returns the first selector that matches and
# remains bound to the supplied scope (a card), never broadcasting to the page.
TIKTOK_SELECTORS = {
    "card": [
        '[data-e2e="feed-item"]',
        '[data-e2e="video-card"]',
    ],
    "video_link": [
        'a[href*="/video/"]',
    ],
    "like_count": [
        '[data-e2e="like-count"]',
    ],
    "description": [
        '[data-e2e="video-card-desc"], [data-e2e="video-desc"]',
    ],
}

_VIDEO_LINK_RE = re.compile(r"/(?:@[^/]+/)?video/(\d+)")

# Feed mode loads roughly one video per viewport and virtualizes the list, so we
# scroll generously and stop once we've collected enough unique videos.
_FEED_SCROLL_MULTIPLIER = 3


def _empty_stats():
    return {"cards_seen": 0, "parsed": 0, "incomplete": 0}


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


def _better_caption(existing: str, incoming: str) -> str:
    """Deterministic caption choice: non-empty beats empty, longer beats shorter,
    ties keep the earlier value. Never invents or concatenates text."""
    if not existing:
        return incoming
    if not incoming:
        return existing
    if len(incoming) != len(existing):
        return incoming if len(incoming) > len(existing) else existing
    return existing


def _merge_tiktok_candidate(existing: dict | None, incoming: dict) -> dict:
    """Merge two observations of the SAME TikTok video into one candidate.

    Independent of discovery order:
      * likes   — max of valid values (engagement grows over time); a missing/
                  unparseable value (0) can never overwrite a valid one.
      * caption — ``_better_caption``: non-empty beats empty, longer beats
        shorter, ties keep the earlier value.
      * id/href — the first (canonical/stable) record is preserved so identity
        never churns between observations.
    """
    if existing is None:
        return incoming
    return {
        "id": existing["id"],
        "href": existing["href"],
        "likes": max(int(existing.get("likes", 0) or 0), int(incoming.get("likes", 0) or 0)),
        "caption": _better_caption(existing.get("caption", "") or "", incoming.get("caption", "") or ""),
    }


def _extract_feed_card(card, stats: dict | None = None) -> dict | None:
    """Extract one logical feed/source entry STRICTLY from its own card element.

    The video link, like count and description are all read from the SAME card
    locator, so two videos in one feed can never exchange metadata. A card
    missing any part yields an empty value for that part (it never borrows
    another card's). Returns None only for a card with no video link at all
    (i.e. not actually a video card).
    """
    success = stats is not None and stats.get("cards_seen") is not None
    if success:
        stats["cards_seen"] += 1
    try:
        link_loc = first_matching_locator(card, TIKTOK_SELECTORS["video_link"])
        href = link_loc.get_attribute("href") or "" if link_loc is not None else ""
        if not _VIDEO_LINK_RE.search(href):
            if success:
                stats["incomplete"] += 1
            return None

        likes = 0
        lik_loc = first_matching_locator(card, TIKTOK_SELECTORS["like_count"])
        if lik_loc is not None:
            try:
                likes = _parse_count(lik_loc.inner_text(timeout=1500))
            except Exception:
                likes = 0

        caption = ""
        desc_loc = first_matching_locator(card, TIKTOK_SELECTORS["description"])
        if desc_loc is not None:
            try:
                caption = (desc_loc.inner_text(timeout=1500) or "").strip()[:200]
            except Exception:
                caption = ""

        if success:
            stats["parsed"] += 1
        return _card(href, likes, caption)
    except Exception as e:
        if success:
            stats["incomplete"] += 1
        log.debug("tiktok feed card parse skipped: %s", e)
        return None


def _collect_cards_with_stats(page, limit: int):
    """Return ``(cards, stats)`` for the visible video cards.

    Every card is scoped: its link, like count and description are read from
    the SAME container, so metadata cannot attach to the wrong video. When the
    same video id is observed more than once, observations are merged (better
    likes/caption win) instead of discarding the later record.
    """
    merged: dict[str, dict] = {}
    stats = _empty_stats()
    for el in iter_matching_nodes(page, TIKTOK_SELECTORS["card"]):
        card = _extract_feed_card(el, stats)
        if card is None:
            continue
        merged[card["id"]] = _merge_tiktok_candidate(merged.get(card["id"]), card)
        if len(merged) >= limit:
            break
    return list(merged.values()), stats


def _collect_cards(page, limit: int) -> list[dict]:
    """Backward-compatible wrapper: cards only (see _collect_cards_with_stats)."""
    cards, _ = _collect_cards_with_stats(page, limit)
    return cards


def _collect_feed_with_stats(page, max_posts: int, scrolls: int):
    """Scroll the For You feed; returns (cards, stats).

    Duplicate observations of the same video are merged (best likes/caption are
    kept) rather than dropping whichever record appeared second.
    """
    merged: dict[str, dict] = {}
    stats = _empty_stats()
    probes = max(scrolls * _FEED_SCROLL_MULTIPLIER, 8)
    for _ in range(probes):
        for el in iter_matching_nodes(page, TIKTOK_SELECTORS["card"]):
            card = _extract_feed_card(el, stats)
            if card is None:
                continue
            merged[card["id"]] = _merge_tiktok_candidate(merged.get(card["id"]), card)
            if len(merged) >= max_posts:
                return list(merged.values()), stats
        try:
            page.mouse.wheel(0, 3000)
        except Exception:
            pass
        page.wait_for_timeout(random.randint(1800, 2800))
    return list(merged.values()), stats


def _collect_feed(page, max_posts: int, scrolls: int) -> list[dict]:
    """Scroll the For You feed, collecting per-card metadata for each video."""
    cards, _ = _collect_feed_with_stats(page, max_posts, scrolls)
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

    Diagnostics: one concise summary line per scrape plus a warning when the
    page exposed none of the known card containers (possible selector drift).
    """
    accounts = [a for a in config.get("accounts", []) if a]
    foryou = bool(config.get("foryou", False))
    min_likes = int(config.get("min_likes", 0))
    max_posts = int(config.get("max_posts_per_account", 10))
    scrolls = int(config.get("scrolls", 3))

    stats = _empty_stats()
    items: list[dict] = []
    collected: dict[str, dict] = {}
    handle_for: dict[str, str] = {}
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
            cards, feed_stats = _collect_feed_with_stats(page, max_posts, scrolls)
            _merge_stats(stats, feed_stats)
            for card in cards:
                collected[card["id"]] = _merge_tiktok_candidate(
                    collected.get(card["id"]), card
                )

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

            cards, acct_stats = _collect_cards_with_stats(page, max_posts)
            _merge_stats(stats, acct_stats)
            for card in cards:
                prev = collected.get(card["id"])
                merged = _merge_tiktok_candidate(prev, card)
                collected[card["id"]] = merged
                if prev is None or (
                    card["caption"] and merged["caption"] == card["caption"]
                ):
                    handle_for[card["id"]] = handle
    finally:
        try:
            page.close()
        except Exception:
            pass

    for card in collected.values():
        item = _to_item(card, handle_for.get(card["id"], ""), min_likes)
        if item is not None:
            items.append(item)

    log.info(
        "TikTok: cards_seen=%d parsed=%d incomplete=%d candidates_after_min_likes=%d",
        stats["cards_seen"], stats["parsed"], stats["incomplete"], len(items),
    )
    if (foryou or not accounts) and stats["cards_seen"] == 0:
        log.warning(
            "TikTok scrape found no video cards via known selectors "
            "(%s) — possible structure/selector drift; no candidates emitted",
            ", ".join(TIKTOK_SELECTORS["card"]),
        )
    return items


def _merge_stats(total: dict, extra: dict):
    for key in ("cards_seen", "parsed", "incomplete"):
        total[key] += extra.get(key, 0)
    return total