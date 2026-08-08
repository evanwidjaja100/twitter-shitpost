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

A persisted target is an *authorization ceiling* as well as a randomness seed:
the stored target is kept unchanged for restart consistency, but the *effective*
target used for remaining-slot math is ``min(stored_target, current max)``. If
the user lowers ``max_posts_per_day`` mid-window, an old higher stored target
can therefore never authorize posts above the newly configured maximum.

Two independent counters are enforced:

* logical posting-window quota: the persisted target minus successful posts
  recorded inside the current window (the 3-6/day normal mechanism), and
* local calendar-day absolute cap: `max_daily_posts_absolute` minus successful
  posts on the machine-local calendar day.

The calendar-day cap is an independent backstop — overnight posts belong to
the *previous* logical window but to the *current* calendar day, so a window
may still have room while the day has none. `check_posting_limits()` returns
both counters separately; `remaining_slots()` and the daemon's pre-post gate
only ever allow `min(window_room, absolute_room)` posts.

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


def check_posting_limits(
    db,
    min_posts: int,
    max_posts: int,
    start_hour: int,
    end_hour: int,
    max_absolute=None,
    now: datetime | None = None,
    rng=None,
) -> dict:
    """Evaluate the two independent quota counters at `now`.

    Returns an info dict separating the logical posting-window count from the
    local calendar-day absolute count (they are NEVER merged into one counter):

        effective_target = min(persisted_target, current max)  # clamp
        window_room     = effective_target - window_posts       # 3-6/day logical window
        absolute_room   = max_absolute - today_posts            # per-calendar-day backstop
        remaining       = max(0, min(window_room, absolute_room))

    `window_posts` counts successful posts inside the current logical window.
    `today_posts` counts successful posts on the machine-local calendar day of
    `now`, so posts from a previous overnight window still shrink the absolute
    room correctly. The per-window target is minted once (and persisted) only
    when a window is currently active.

    `now` defaults to the real clock; `rng` defaults to `random`. The `reason`
    field is None when posting is allowed, otherwise one of:
    "window_inactive", "target_reached", "daily_absolute_cap".
    """
    now = now or datetime.now()
    rng = rng or random
    start = window_start(start_hour, end_hour, now)
    end = window_end(start, start_hour, end_hour)
    active = window_active(start, end, now)
    wid = window_id(start)

    target = db.get_window_target(wid) if active else None
    if active and target is None:
        # Never let a malformed config (min > max) blow up rng.randint() at
        # runtime; a minted target can never exceed the current maximum.
        lo = int(min_posts)
        hi = int(max_posts)
        if lo > hi:
            lo = hi
        target = int(rng.randint(lo, hi))
        db.set_window_target(wid, target)

    # A persisted target must never authorize posting above the current
    # configured maximum. Clamp for authorization only; the stored original is
    # left untouched so restart consistency and a later re-raise of the max
    # (which may legitimately re-enable the original target) are preserved.
    effective_target = min(target, int(max_posts)) if target is not None else None

    window_posts = (db.window_post_count(start.timestamp(), end.timestamp())
                    if active else 0)
    window_room = max(effective_target - window_posts, 0) if active and effective_target is not None else 0

    today_posts = db.posts_today(now.timestamp())
    absolute_room = (max(int(max_absolute) - today_posts, 0)
                     if max_absolute is not None else None)

    remaining = window_room
    if max_absolute is not None:
        remaining = min(remaining, absolute_room)
    remaining = max(remaining, 0)

    if not active:
        reason = "window_inactive"
    elif window_room == 0:
        reason = "target_reached"
    elif remaining == 0:
        reason = "daily_absolute_cap"
    else:
        reason = None

    return {
        "allowed": remaining > 0,
        "remaining": remaining,
        "reason": reason,
        "active": active,
        "window_id": wid,
        "window_start": start,
        "window_end": end,
        "window_posts": window_posts,
        "target": target,
        "effective_target": effective_target,
        "window_room": window_room,
        "today_posts": today_posts,
        "absolute_room": absolute_room,
    }


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

    Delegates every quota decision to `check_posting_limits()`:
    * identifies the single logical window containing `now`
    * no window is active (e.g. overnight gap after end_hour) -> []
    * the per-window target is chosen once and persisted via `db`
    * returns at most `min(remaining window slots, remaining absolute cap)`
      and NEVER mints another full quota on repeated calls.
    """
    now = now or datetime.now()
    rng = rng or random
    state = check_posting_limits(
        db, min_posts, max_posts, start_hour, end_hour,
        max_absolute=max_absolute, now=now, rng=rng,
    )
    if state["remaining"] <= 0:
        return []
    return sample_slots(
        state["window_start"], state["window_end"], now, state["remaining"], rng
    )


def sleep_until(target_ts: float):
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        time.sleep(min(5.0, remaining))