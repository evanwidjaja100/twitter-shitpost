"""Deterministic posting-window scheduling with a persisted per-window quota.

A logical posting window is anchored at local `start_hour` and may wrap past
midnight (e.g. 16:00 -> 01:00). It is treated as one window starting at
`start_hour` on day N and ending at `end_hour` on day N+1; posts between
midnight and `end_hour` belong to the window that began the previous afternoon.

Exactly one target post count is chosen per window (between min and max,
inclusive) and persisted in SQLite so the target stays stable across daemon
loop iterations and survives daemon restarts. Only the *remaining* slots
(target minus successful posts already inside the window) are ever returned, so
the configured maximum is a real maximum and repeating the scheduling loop can
never mint another full quota.

All functions take an explicit `now` and rng so tests are deterministic.
"""

import random
import time
from datetime import datetime, timedelta


def _day_boundary(dt: datetime, hour: int) -> datetime:
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def window_start(start_hour: int, end_hour: int, now: datetime) -> datetime:
    """Start of the logical posting window that the instant `now` belongs to.

    This is the most recent moment (<= `now`) at which a window began, so a
    00:30 instant with a 16:00->01:00 window correctly resolves to yesterday
    16:00 rather than today 16:00.
    """
    candidate = _day_boundary(now, start_hour)
    if candidate <= now:
        return candidate
    return candidate - timedelta(days=1)


def window_end(start: datetime, start_hour: int, end_hour: int) -> datetime:
    """End of the window that began at `start`."""
    if end_hour > start_hour:
        return _day_boundary(start, end_hour)
    return _day_boundary(start + timedelta(days=1), end_hour)


def window_id(start: datetime) -> str:
    """Stable identifier for a window, based on the calendar date of its start."""
    return start.strftime("%Y-%m-%d")


def window_active(start: datetime, end: datetime, now: datetime) -> bool:
    return start <= now < end


def next_window_start(start_hour: int, now: datetime) -> datetime:
    """Start of the upcoming window (strictly after `now`)."""
    cand = _day_boundary(now, start_hour)
    if cand > now:
        return cand
    return cand + timedelta(days=1)


def sample_slots(start, end, now, count, rng=None) -> list[float]:
    """Return up to `count` future epoch-second slots within [start, end).

    Slots are on minute boundaries, always strictly later than `now`, and
    sampled without replacement. Deterministic when `rng` is supplied.
    """
    count = max(0, int(count))
    if count == 0:
        return []
    rng = rng or random
    s = _floor_minute(start)
    e = _floor_minute(end)
    n = _floor_minute(now)
    total_minutes = int((e - s).total_seconds() // 60)
    candidates = []
    for i in range(max(total_minutes, 0)):
        t = s + timedelta(minutes=i)
        if t > n:
            candidates.append(t)
    if not candidates:
        return []
    chosen = rng.sample(candidates, min(count, len(candidates)))
    return sorted(int(t.timestamp()) for t in chosen)


def remaining_slots(
    db,
    min_posts: int,
    max_posts: int,
    start_hour: int,
    end_hour: int,
    max_absolute=None,
    now: datetime | None = None,
    rng=None,
) -> list[float]:
    """Future epoch slots still due inside the current posting window.

    * identifies the single logical window containing `now`
    * no window is active (e.g. overnight gap after end_hour) -> []
    * the per-window target is chosen once and persisted via `db`
    * the number of slots returned is target minus successful posts already
      recorded in that window, additionally clamped by `max_absolute`
    """
    now = now or datetime.now()
    rng = rng or random
    start = window_start(start_hour, end_hour, now)
    end = window_end(start, start_hour, end_hour)
    if not window_active(start, end, now):
        return []

    wid = window_id(start)
    target = db.get_window_target(wid)
    if target is None:
        target = rng.randint(int(min_posts), int(max_posts))
        db.set_window_target(wid, target)

    posted = db.window_post_count(start.timestamp(), end.timestamp())
    remaining = target - posted
    if max_absolute is not None:
        remaining = min(remaining, int(max_absolute) - posted)
    if remaining <= 0:
        return []
    return sample_slots(start, end, now, remaining, rng)


def sleep_until(target_ts: float):
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        time.sleep(min(5.0, remaining))