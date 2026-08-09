"""X scraper — browses curated accounts with the bot's own logged-in browser.

Collects top recent posts (text + media) and downloads media via the shared
session (cookies included). No official API reads = $0.

Selector policy (scraper resilience): every selector lives in one module-level
map so a Twitter DOM change is fixed in one place. Per-item reads are ALWAYS
scoped to the tweet's own container element (never a page-global ``.first``),
and the like-count threshold is authoritative: a missing or unparseable count
is treated as 0 and can never slip through ``min_likes``.

When the page structure disappears entirely (no tweet containers matched by any
known selector) the scrape logs a diagnostics warning and returns no candidates
instead of fabricating items.
"""

import logging
import random
import time
from pathlib import Path
from urllib.parse import urlparse

from scrapers._dom import first_matching_locator, iter_matching_nodes

log = logging.getLogger("x_scraper")


def _raise_if_global_browser_error(exc: Exception) -> None:
    from publisher.x_publisher import BrowserSessionError, is_closed_context_error

    if isinstance(exc, BrowserSessionError) or is_closed_context_error(exc):
        raise exc


# Ordered, known-safe selectors. The first key is the current production
# selector; later entries are conservative fallbacks for DOM drift. Fallbacks
# MUST stay scoped to the same element kind — broad selectors that could match
# a different card are never used (correct failure beats wrong data).
X_SELECTORS = {
    # A tweet card: the scoping root (first_matching_locator uses the first
    # selector that has matches; both stay tweet-shaped).
    "article": [
        'article[data-testid="tweet"]',
        '[data-testid="tweet"]',
    ],
    "tweet_text": [
        'div[data-testid="tweetText"]',
    ],
    "like_count": [
        'button[data-testid="like"] span[data-testid="app-text-transition-container"]',
    ],
    "status_link": [
        'a[href*="/status/"]',
    ],
    "media_img": [
        'img[src*="pbs.twimg.com/media"]',
    ],
    "video": [
        "video",
    ],
}

_PBS_TWIMG = "pbs.twimg.com/media"


def _parse_count(text: str) -> int:
    """'12.4K' -> 12400, '1.2M' -> 1200000, '5' -> 5."""
    text = (text or "").strip()
    try:
        mult = 1.0
        if text.endswith("K"):
            mult, text = 1_000.0, text[:-1]
        elif text.endswith("M"):
            mult, text = 1_000_000.0, text[:-1]
        return int(float(text) * mult)
    except (ValueError, TypeError):
        return 0


def _first_text(scope, selector_key: str, timeout_ms: int = 2000) -> str:
    """Inner text of the first element under ``scope`` for a selector group.

    Returns ``""`` when nothing matches or the read fails — the missing text
    is never borrowed from another card or from page text.
    """
    loc = first_matching_locator(scope, X_SELECTORS[selector_key])
    if loc is None:
        return ""
    try:
        return (loc.inner_text(timeout=timeout_ms) or "")[:500]
    except Exception as exc:
        _raise_if_global_browser_error(exc)
        return ""


def _post_media(article) -> list[str]:
    """Collect media URLs strictly from the current tweet article."""
    urls = []
    for img in iter_matching_nodes(article, X_SELECTORS["media_img"]):
        src = img.get_attribute("src")
        if src and src not in urls:
            urls.append(src)
    for v in iter_matching_nodes(article, X_SELECTORS["video"]):
        src = v.get_attribute("src") or ""
        poster = v.get_attribute("poster") or ""
        if src.startswith("https://") and src not in urls:
            urls.append(src)
        if poster.startswith("https://") and poster not in urls:
            urls.append(poster)
    return urls


def _parse_article(article, min_likes: int) -> dict | None:
    """Extract ONE tweet strictly from its own card.

    Returns None when the like count is missing/unparseable/under the
    threshold (conservative — it can never bypass ``min_likes``) or when no
    status link is found. Missing media simply means a lower kind; a missing
    caption stays empty. The method never queries the page globally.
    """
    likes = 0
    like_loc = first_matching_locator(article, X_SELECTORS["like_count"])
    if like_loc is not None:
        try:
            likes = _parse_count(like_loc.inner_text(timeout=2000))
        except Exception as exc:
            _raise_if_global_browser_error(exc)
            likes = 0  # unparseable/missing = 0, never the threshold
    if likes < min_likes:
        return None

    link = ""
    link_loc = first_matching_locator(article, X_SELECTORS["status_link"])
    if link_loc is not None:
        try:
            link = link_loc.get_attribute("href") or ""
        except Exception as exc:
            _raise_if_global_browser_error(exc)
            link = ""
    if not link:
        return None

    media_urls = _post_media(article)
    kind = "image" if any(_PBS_TWIMG in u for u in media_urls) \
        else ("video" if media_urls else "text")

    return {
        "source": "x",
        "source_id": link.split("/status/")[-1].split("?")[0],
        "source_url": f"https://x.com{link}" if link.startswith("/") else link,
        "title": _first_text(article, "tweet_text"),
        "media_url": media_urls[0] if media_urls else None,
        "media_path": None,
        "score": float(likes),
        "created_utc": time.time(),
        "nsfw": False,
        "kind": kind,
    }


def _scrape_site(session, config: dict, assets_dir: str):
    """Scrape all configured accounts; returns ``(items, stats)``.

    ``stats`` records the observed counters used for diagnostics:
      accounts, containers, parsed, parsed_incomplete, candidates,
      selector_failures.
    A zero-container page (with accounts configured) is diagnosed as likely
    selector drift; a low parsed count after filtering is normal ``min_likes``.
    """
    stats = {
        "accounts": 0,
        "containers": 0,
        "parsed": 0,
        "incomplete": 0,
        "candidates": 0,
        "selector_failures": 0,
    }
    accounts = config.get("accounts", [])
    if not accounts:
        return [], stats
    stats["accounts"] = len(accounts)
    min_likes = config.get("min_likes", 5000)
    max_posts = config.get("max_posts_per_account", 10)
    scrolls = config.get("scrolls", 3)

    items: list[dict] = []
    page = session.new_page()
    try:
        for handle in accounts:
            handle = handle.lstrip("@")
            try:
                page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                for _ in range(scrolls):
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(random.randint(2000, 3500))
            except Exception as e:
                _raise_if_global_browser_error(e)
                log.warning("profile %s failed: %s", handle, e)
                continue

            articles = iter_matching_nodes(page, X_SELECTORS["article"])
            stats["containers"] += len(articles)
            if not articles:
                log.warning(
                    "X account @%s: page loaded but no tweet article containers "
                    "matched known selectors (%s)",
                    handle, ", ".join(X_SELECTORS["article"]),
                )
                stats["selector_failures"] += 1
                continue

            collected = 0
            for article in articles:
                try:
                    parsed = _parse_article(article, min_likes)
                    if parsed is None:
                        stats["incomplete"] += 1
                        continue
                    stats["parsed"] += 1
                    if parsed["kind"] == "text" or not parsed["media_url"]:
                        continue
                    items.append(parsed)
                    stats["candidates"] += 1
                    collected += 1
                    if collected >= max_posts:
                        break
                except Exception as e:
                    _raise_if_global_browser_error(e)
                    log.debug("tweet parse skipped: %s", e)
                    continue
    finally:
        try:
            page.close()
        except Exception:
            pass
    return items, stats


def scrape(session, config: dict, assets_dir: str) -> list[dict]:
    """Visit each account profile and collect candidate posts.

    Diagnostics: one concise summary line per scrape plus a warning when a
    loaded page exposes none of the expected tweet containers. An empty result
    is a legitimate outcome when ``min_likes`` filters everything or no accounts
    are configured; it is never treated as an exception.
    """
    items, stats = _scrape_site(session, config, assets_dir)
    log.info(
        "X: accounts=%d containers=%d parsed=%d candidates_after_min_likes=%d",
        stats["accounts"], stats["containers"], stats["parsed"], stats["candidates"],
    )
    if stats["accounts"] and stats["containers"] == 0:
        log.warning(
            "X scrape found no tweet containers across %d accounts — possible "
            "selector drift or bot-check page; no candidates emitted",
            stats["accounts"],
        )
    return items


def _host_matches(host: str, domain: str) -> bool:
    """True when ``domain`` is the host itself or a parent domain (safe to send)."""
    if not host or not domain:
        return False
    host = host.lower()
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def _copy_cookies(session, url: str) -> dict:
    """Copy only the cookies relevant to the media host from the browser context.

    Cookies are read in-process and never logged, printed or persisted. Only
    cookies whose domain matches (or is a parent of) the media host are copied.
    X media hosts (pbs.twimg.com) are typically public, so this is usually an
    empty map. If the context has no usable cookie API, an empty map is used.
    """
    if not hasattr(session, "cookies"):
        return {}
    try:
        raw = session.cookies()
    except Exception as exc:
        _raise_if_global_browser_error(exc)
        return {}
    host = urlparse(url).hostname if url else ""
    return {
        c["name"]: c["value"]
        for c in raw or []
        if c.get("name") is not None
        and c.get("value") is not None
        and _host_matches(host or "", c.get("domain", ""))
    }


def download_media(session, item: dict, dest_dir: str, max_bytes: int | None = None) -> str | None:
    """Download item media via the bounded streaming downloader.

    The transfer is NEVER materialized in memory before the limit is enforced:
    this is a true HTTP stream with a Content-Length early check PLUS a running
    byte counter that hard-aborts the moment ``max_bytes`` is exceeded, exactly
    like ``pipeline.media.download``. A partial/oversized file is removed. The
    browser context's cookies (restricted to the media host) are transferred so
    authenticated media still works without buffering the body.
    Returns local path or None.
    """
    from pipeline import media as m

    url = item.get("media_url")
    if not url:
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    ext = ".jpg"
    if ".png" in url:
        ext = ".png"
    elif ".gif" in url:
        ext = ".gif"
    out = dest / f"{item['source_id']}{ext}"
    try:
        m.download(
            url,
            str(out),
            referer="https://x.com/",
            max_bytes=max_bytes,
            cookies=_copy_cookies(session, url) or None,
        )
        return str(out)
    except m.MediaError as e:
        log.warning("media download rejected: %s", e)
        return None
