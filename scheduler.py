"""Randomized daily posting schedule: 3-6 posts/day within active hours.

Supports windows that wrap past midnight (e.g. 16:00 -> 01:00). Slots are
sampled from today's remaining future window plus tomorrow's full window, so
the daemon never starves late at night.
"""

import random
import time


def compute_post_times(
    min_posts: int,
    max_posts: int,
    start_hour: int,
    end_hour: int,
    now: time.struct_time | None = None,
) -> list[float]:
    """Random distinct posting times (epoch seconds), all in the future."""
    now = now or time.localtime()
    n = random.randint(min_posts, max_posts)
    start_min = start_hour * 60
    end_min = end_hour * 60
    if end_min > start_min:
        window = list(range(start_min, end_min))
    else:
        window = list(range(start_min, 1440)) + list(range(0, end_min))
    if not window:
        return []

    day_start = time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, -1)
    )
    now_ts = time.time()

    epochs = []
    for day_offset in (0, 1):  # today + tomorrow
        base = day_start + day_offset * 86400
        for slot in window:
            ts = base + slot * 60
            if slot < start_min:
                ts += 86400  # wrapped slot belongs to the next day
            if ts > now_ts:
                epochs.append(ts)

    if not epochs:
        return []
    return sorted(random.sample(epochs, min(n, len(epochs))))


def sleep_until(target_ts: float):
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        time.sleep(min(5.0, remaining))
