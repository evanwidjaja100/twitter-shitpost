"""Follower tracking — reads @average_pocka's follower count via the logged-in
browser (no API = $0) and keeps a history for the account's sales sheet.
"""

import csv
import logging
import re
import time

log = logging.getLogger("tracker")

_FOLLOWERS_RE = re.compile(r"([\d.,]+[KM]?)\s*Followers", re.I)


def _parse_number(text: str) -> int | None:
    """'1,234' -> 1234, '12.4K' -> 12400, '1.2M' -> 1200000."""
    m = re.search(r"([\d.,]+)([KM]?)", (text or "").replace(",", ""))
    if not m:
        return None
    try:
        num = float(m.group(1))
        mult = {"K": 1_000, "M": 1_000_000}.get(m.group(2).upper(), 1)
        return int(num * mult)
    except ValueError:
        return None


def get_follower_count(session, handle: str, timeout_s: int = 45) -> int | None:
    """Return current follower count, or None if it can't be determined."""
    page = session.new_page()
    try:
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        # Primary: the profile stats link contains the number + "Followers".
        link = page.locator(f'a[href*="{handle}/verified_followers"]').first
        if link.count():
            text = link.inner_text(timeout=5000)
            if n := _parse_number(text):
                return n

        # Fallback: scan the visible page text.
        body = page.locator("body").inner_text(timeout=5000)
        m = _FOLLOWERS_RE.search(body)
        if m:
            return _parse_number(m.group(1))
        return None
    except Exception as e:
        log.warning("follower read failed: %s", e)
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


def write_csv(path: str, history: list[tuple[float, int]]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["checked_at_utc", "followers"])
        for ts, count in history:
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)), count])


def maybe_check_followers(db, cfg, session) -> bool:
    """Check follower count if enough time has passed since the last check.
    Returns True if a check was performed (or no browser session available)."""
    tracking = cfg.get("tracking", {})
    interval_h = tracking.get("follow_check_hours", 168)
    handle = tracking.get("own_handle", "average_pocka")
    last = db.last_follower_check()
    if last is not None and (time.time() - last) < interval_h * 3600:
        return False
    if session is None:
        log.warning("follower check skipped: no browser session")
        return False
    count = get_follower_count(session, handle)
    if count is None:
        log.warning("follower check failed (not logged in? page changed?)")
        return False
    db.record_follower(count)
    log.info("followers: %d", count)
    return True
