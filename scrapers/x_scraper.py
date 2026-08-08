"""X scraper — browses curated accounts with the bot's own logged-in browser.

Collects top recent posts (text + media) and downloads media via the shared
session (cookies included). No official API reads = $0.
"""

import logging
import random
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("x_scraper")


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


def _post_media(page) -> list[str]:
    """Collect media URLs from the current tweet article."""
    urls = []
    for img in page.locator('img[src*="pbs.twimg.com/media"]').all():
        src = img.get_attribute("src")
        if src and src not in urls:
            urls.append(src)
    for v in page.locator("video").all():
        src = v.get_attribute("src") or ""
        poster = v.get_attribute("poster") or ""
        if src.startswith("https://") and src not in urls:
            urls.append(src)
        if poster.startswith("https://") and poster not in urls:
            urls.append(poster)
    return urls


def scrape(session, config: dict, assets_dir: str) -> list[dict]:
    """Visit each account profile and collect candidate posts."""
    accounts = config.get("accounts", [])
    if not accounts:
        return []
    min_likes = config.get("min_likes", 5000)
    max_posts = config.get("max_posts_per_account", 10)
    scrolls = config.get("scrolls", 3)

    items: list[dict] = []
    context = session._context
    page = context.new_page()
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
                log.warning("profile %s failed: %s", handle, e)
                continue

            articles = page.locator('article[data-testid="tweet"]')
            count = min(articles.count(), max_posts * 4)
            collected = 0
            for i in range(count):
                try:
                    article = articles.nth(i)
                    text = ""
                    t = article.locator('div[data-testid="tweetText"]')
                    if t.count():
                        text = t.first.inner_text(timeout=2000)[:500]

                    like_el = article.locator(
                        'button[data-testid="like"] span[data-testid="app-text-transition-container"]'
                    ).first
                    likes = _parse_count(like_el.inner_text(timeout=2000)) if like_el.count() else 0

                    link = ""
                    a = article.locator('a[href*="/status/"]').first
                    if a.count():
                        link = a.get_attribute("href") or ""

                    media_urls = _post_media(article)
                    if likes < min_likes:
                        continue
                    if not link:
                        continue

                    kind = "image" if any(
                        ".twimg.com/media" in u for u in media_urls
                    ) else ("video" if media_urls else "text")

                    item = {
                        "source": "x",
                        "source_id": link.split("/status/")[-1].split("?")[0],
                        "source_url": f"https://x.com{link}" if link.startswith("/") else link,
                        "title": text,
                        "media_url": media_urls[0] if media_urls else None,
                        "media_path": None,
                        "score": float(likes),
                        "created_utc": time.time(),
                        "nsfw": False,
                        "kind": kind,
                    }
                    if item["kind"] != "text" and item["media_url"]:
                        items.append(item)
                        collected += 1
                        if collected >= max_posts:
                            break
                except Exception as e:
                    log.debug("tweet parse skipped: %s", e)
                    continue
    finally:
        try:
            page.close()
        except Exception:
            pass
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
    ctx = getattr(session, "_context", None)
    if ctx is None or not hasattr(ctx, "cookies"):
        return {}
    try:
        raw = ctx.cookies()
    except Exception:
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
