"""Regression tests for Issue 3: deterministic, quota-limited posting windows.

Uses explicit `now` datetimes and seeded rng so tests never depend on the
real clock or real randomness. Persistence is exercised against real temp
SQLite files so daemon-restart behaviour is genuinely covered.
"""

from datetime import datetime
import random

import pytest

import scheduler
from storage.db import Database


def _db(tmp_path):
    return Database(str(tmp_path / "bot.db"))


def _slots(db, start_hour, end_hour, now, min_posts=3, max_posts=6,
           max_absolute=None, seed=1):
    return scheduler.remaining_slots(
        db, min_posts, max_posts, start_hour, end_hour,
        max_absolute=max_absolute, now=now, rng=random.Random(seed),
    )


def _set_target(db, wid, target):
    db.set_window_target(wid, target)


def _post_at(db, ts):
    if isinstance(ts, datetime):
        ts = ts.timestamp()
    db.finalize_successful_post(
        caption="c", media_path="m", source="youtube", source_id="vid-1",
        source_url="https://youtu.be/u", content_hash=f"h{int(ts)}",
        now_ts=ts,
    )


class TestWindowBounds:
    def test_same_day_window(self):
        now = datetime(2026, 1, 5, 12, 0)
        s = scheduler.window_start(9, 18, now)
        assert s == datetime(2026, 1, 5, 9, 0)
        assert scheduler.window_end(s, 9, 18) == datetime(2026, 1, 5, 18, 0)
        assert scheduler.window_active(s, scheduler.window_end(s, 9, 18), now)

    def test_overnight_window_before_midnight(self):
        now = datetime(2026, 1, 5, 20, 0)  # 16:00 -> 01:00, before midnight
        s = scheduler.window_start(16, 1, now)
        assert s == datetime(2026, 1, 5, 16, 0)
        assert scheduler.window_end(s, 16, 1) == datetime(2026, 1, 6, 1, 0)
        assert scheduler.window_id(s) == "2026-01-05"

    def test_overnight_window_after_midnight_still_same_window(self):
        now = datetime(2026, 1, 6, 0, 30)  # belongs to window started Jan 5 16:00
        s = scheduler.window_start(16, 1, now)
        assert s == datetime(2026, 1, 5, 16, 0)
        e = scheduler.window_end(s, 16, 1)
        assert e == datetime(2026, 1, 6, 1, 0)
        assert scheduler.window_active(s, e, now)

    def test_after_overnight_end_no_active_window(self):
        now = datetime(2026, 1, 6, 2, 0)  # 01:00 ended; inactive gap
        s = scheduler.window_start(16, 1, now)
        e = scheduler.window_end(s, 16, 1)
        assert not scheduler.window_active(s, e, now)
        assert scheduler.next_window_start(16, now) == datetime(2026, 1, 6, 16, 0)

    def test_after_same_day_end_next_start(self):
        now = datetime(2026, 1, 5, 20, 0)  # 09-18 done
        s = scheduler.window_start(9, 18, now)
        assert not scheduler.window_active(s, scheduler.window_end(s, 9, 18), now)
        assert scheduler.next_window_start(9, now) == datetime(2026, 1, 6, 9, 0)


class TestTargetSelection:
    def test_target_in_range_and_stable(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2026, 1, 5, 12, 0)
        slots1 = _slots(db, 9, 18, now)
        wid = scheduler.window_id(scheduler.window_start(9, 18, now))
        target = db.get_window_target(wid)
        assert 3 <= target <= 6
        assert len(slots1) == target
        for ts in slots1:
            dt = datetime.fromtimestamp(ts)
            assert now < dt < datetime(2026, 1, 5, 18, 0)

        # Repeated scheduling in the same window must NOT regenerate a quota.
        assert db.get_window_target(wid) == target
        slots2 = _slots(db, 9, 18, now, seed=999)
        assert len(slots2) == target  # unchanged, not minted anew

    def test_after_midnight_uses_existing_target(self, tmp_path):
        db = _db(tmp_path)
        _slots(db, 16, 1, datetime(2026, 1, 5, 20, 0), seed=7)
        target = db.get_window_target("2026-01-05")
        assert 3 <= target <= 6

        now_after = datetime(2026, 1, 6, 0, 30)
        slots = _slots(db, 16, 1, now_after, seed=7)
        assert db.get_window_target("2026-01-05") == target
        end = scheduler.window_end(datetime(2026, 1, 5, 16, 0), 16, 1)
        for ts in slots:
            assert datetime(2026, 1, 6, 0, 30) < datetime.fromtimestamp(ts) < end

    def test_window_ends_so_no_slots(self, tmp_path):
        db = _db(tmp_path)
        assert _slots(db, 16, 1, datetime(2026, 1, 6, 2, 0)) == []


class TestQuotaEnforcement:
    def test_posts_never_exceed_target(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2026, 1, 5, 12, 0)
        start = scheduler.window_start(9, 18, now)
        end = scheduler.window_end(start, 9, 18)
        _set_target(db, scheduler.window_id(start), 6)

        for i in range(6):
            _post_at(db, start.timestamp() + 600 * (i + 1))
        assert db.window_post_count(start.timestamp(), end.timestamp()) == 6

        for seed in (1, 2, 3, 4, 5):
            assert _slots(db, 9, 18, now, seed=seed) == [], f"seed {seed}"
        assert db.window_post_count(start.timestamp(), end.timestamp()) == 6

    def test_remaining_slots_only(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2026, 1, 5, 12, 0)
        start = scheduler.window_start(9, 18, now)
        end = scheduler.window_end(start, 9, 18)
        _set_target(db, scheduler.window_id(start), 6)
        for i in range(4):
            _post_at(db, start.timestamp() + 600 * (i + 1))

        slots = _slots(db, 9, 18, now, seed=3)
        assert len(slots) == 2  # 6 - 4 already recorded

    def test_absolute_cap_is_backstop(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2026, 1, 5, 12, 0)
        start = scheduler.window_start(9, 18, now)
        end = scheduler.window_end(start, 9, 18)
        _set_target(db, scheduler.window_id(start), 6)
        max_abs = 4
        for i in range(3):
            _post_at(db, start.timestamp() + 600 * (i + 1))

        slots = _slots(db, 9, 18, now, max_absolute=max_abs, seed=3)
        assert len(slots) == 1  # min(6-3, 4-3)
        _post_at(db, start.timestamp() + 600 * 4)
        assert _slots(db, 9, 18, now, max_absolute=max_abs, seed=3) == []

    def test_restart_persists_target(self, tmp_path):
        db = _db(tmp_path)
        now = datetime(2026, 1, 5, 12, 0)
        _slots(db, 9, 18, now, seed=11)
        wid = scheduler.window_id(scheduler.window_start(9, 18, now))
        target = db.get_window_target(wid)
        assert target is not None

        reopened = Database(str(tmp_path / "bot.db"))  # simulate a daemon restart
        assert reopened.get_window_target(wid) == target

    def test_posts_in_previous_window_do_not_count(self, tmp_path):
        db = _db(tmp_path)
        start = datetime(2026, 1, 5, 16, 0)
        end = datetime(2026, 1, 6, 1, 0)
        _post_at(db, datetime(2026, 1, 5, 20, 0).timestamp())  # in the window

        assert db.window_post_count(start.timestamp(), end.timestamp()) == 1
        next_start = datetime(2026, 1, 6, 16, 0)
        assert db.window_post_count(next_start.timestamp(), next_start.timestamp() + 3600) == 0

    def test_post_after_midnight_counts_to_previous_window(self, tmp_path):
        db = _db(tmp_path)
        start = datetime(2026, 1, 5, 16, 0)
        end = datetime(2026, 1, 6, 1, 0)
        _post_at(db, datetime(2026, 1, 6, 0, 30))  # stays inside Jan 5 window

        assert db.window_post_count(start.timestamp(), end.timestamp()) == 1
        next_start = datetime(2026, 1, 6, 16, 0)
        assert db.window_post_count(next_start.timestamp(), next_start.timestamp() + 3600) == 0