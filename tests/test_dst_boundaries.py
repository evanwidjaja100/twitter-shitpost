"""Deterministic DST-safe day-boundary tests (Issue 2).

Covers `storage.db.local_day_bounds` and `Database.posts_today` against the
real America/New_York timezone. All cases use explicit `zoneinfo.ZoneInfo` and
fixed epochs so the 23-hour spring-forward day and 25-hour fall-back day are
verified without depending on the host clock or host timezone.
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from storage import db as db_module
from storage.db import Database, local_day_bounds

NY = ZoneInfo("America/New_York")
DAY = 86_400.0


def ts_in_ny(year, month, day, hour, minute=0, second=0) -> float:
    """Epoch timestamp of a naive wall-clock time in America/New_York."""
    return datetime(year, month, day, hour, minute, second, tzinfo=NY).timestamp()


def _fresh_db(tmp_path) -> Database:
    return Database(str(tmp_path / "test.db"))


class TestLocalDayBounds:
    def test_normal_day_is_24h(self):
        now = ts_in_ny(2026, 6, 15, 12, 0, 0)  # EDT, no transition
        start, end = local_day_bounds(now, tz=NY)
        assert start == ts_in_ny(2026, 6, 15, 0, 0, 0)
        assert end == ts_in_ny(2026, 6, 16, 0, 0, 0)
        assert end - start == DAY

    def test_spring_forward_day_is_23h(self):
        # US spring forward 2026: Sunday 2026-03-08, 02:00 -> 03:00 EST->EDT.
        now = ts_in_ny(2026, 3, 8, 12, 0, 0)
        start, end = local_day_bounds(now, tz=NY)
        assert start == ts_in_ny(2026, 3, 8, 0, 0, 0)
        assert end == ts_in_ny(2026, 3, 9, 0, 0, 0)
        assert end - start == DAY - 3600.0  # exactly 23 hours

    def test_fall_back_day_is_25h(self):
        # US fall back 2026: Sunday 2026-11-01, 02:00 -> 01:00 EDT->EST.
        now = ts_in_ny(2026, 11, 1, 12, 0, 0)
        start, end = local_day_bounds(now, tz=NY)
        assert start == ts_in_ny(2026, 11, 1, 0, 0, 0)
        assert end == ts_in_ny(2026, 11, 2, 0, 0, 0)
        assert end - start == DAY + 3600.0  # exactly 25 hours

    def test_start_is_inclusive_end_is_exclusive(self):
        start, end = local_day_bounds(ts_in_ny(2026, 6, 15, 23, 59, 59), tz=NY)
        assert start == ts_in_ny(2026, 6, 15, 0, 0, 0)
        assert end == ts_in_ny(2026, 6, 16, 0, 0, 0)

    def test_23h_start_and_end_are_exact(self):
        start, end = local_day_bounds(ts_in_ny(2026, 3, 8, 1, 30, 0), tz=NY)
        assert end - start == DAY - 3600.0

    def test_25h_start_and_end_are_exact(self):
        start, end = local_day_bounds(ts_in_ny(2026, 11, 1, 1, 30, 0), tz=NY)
        assert end - start == DAY + 3600.0

    def test_fixed_86400_is_wrong(self):
        # The bug being fixed: adding 86400 to the local start of the spring
        # forward day lands INSIDE the next day (2026-03-09 01:00), one hour
        # past the real local midnight.
        start, end = local_day_bounds(ts_in_ny(2026, 3, 8, 12, 0, 0), tz=NY)
        assert end == ts_in_ny(2026, 3, 9, 0, 0, 0)
        assert start + DAY != end

    def test_host_local_backward_compatible(self):
        now = ts_in_ny(2026, 6, 15, 12, 0, 0)
        # tz=None must still work (naive host local) and return sane bounds.
        start, end = local_day_bounds(now, tz=None)
        assert start < now < end
        assert end - start == DAY


class TestPostsTodayDST:
    def _seed(self, db, *pairs):
        # pairs: (status, epoch)
        with db._lock:
            for status, ts in pairs:
                db._conn.execute(
                    "INSERT INTO posts (posted_at, caption, media_path, source, source_url, hash, status, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, "", "", "", "", None, status, None),
                )
            db._conn.commit()

    def test_posts_across_spring_forward_23h_day(self, tmp_path):
        db = _fresh_db(tmp_path)
        day_start = ts_in_ny(2026, 3, 8, 0, 0, 0)
        # 02:30 EST on 03-08 does not exist (skipped by the transition); posts
        # at 03:00 (EDT) and late 22:00 both belong to the 23-hour local day.
        self._seed(
            db,
            ("posted", day_start),                    # exactly at local midnight
            ("posted", ts_in_ny(2026, 3, 8, 3, 0)),   # just after the jump
            ("posted", ts_in_ny(2026, 3, 8, 22, 0)),  # before the 23h day's end
        )
        # 03:00 EDT = 07:00 UTC; end of 03-08 local = 2026-03-09 04:00 UTC.
        assert db.posts_today(day_start, tz=NY) == 3
        assert db.posts_today(ts_in_ny(2026, 3, 8, 12, 0), tz=NY) == 3
        # A post in the first minute of the NEXT local day is not counted today.
        next_day_early = ts_in_ny(2026, 3, 9, 0, 0, 5)
        self._seed(db, ("posted", next_day_early))
        assert db.posts_today(ts_in_ny(2026, 3, 8, 23, 59, 59), tz=NY) == 3

    def test_posts_across_fall_back_25h_day(self, tmp_path):
        db = _fresh_db(tmp_path)
        # 01:30 EDT and 01:30 EST both occur on 2026-11-01; both are within the
        # 25-hour local day and must both be counted.
        self._seed(
            db,
            ("posted", ts_in_ny(2026, 11, 1, 0, 30, 0)),  # before first 01:xx
            ("posted", ts_in_ny(2026, 11, 1, 1, 30, 0)),  # EDT occurrence
            ("posted", ts_in_ny(2026, 11, 1, 1, 30, 0) + 3600.0),  # EST re-run
            ("posted", ts_in_ny(2026, 11, 1, 23, 0, 0)),  # end of the long day
        )
        assert db.posts_today(ts_in_ny(2026, 11, 1, 12, 0, 0), tz=NY) == 4
        # End of the long day in epoch: the 25h day covers 2026-11-01 00:00 EDT
        # (04:00 UTC) to 2026-11-02 00:00 EST (05:00 UTC).
        end = ts_in_ny(2026, 11, 2, 0, 0, 0)
        assert end - ts_in_ny(2026, 11, 1, 0, 0, 0) == DAY + 3600.0

    def test_failed_posts_not_counted(self, tmp_path):
        db = _fresh_db(tmp_path)
        self._seed(
            db,
            ("posted", ts_in_ny(2026, 3, 8, 10, 0, 0)),
            ("error", ts_in_ny(2026, 3, 8, 11, 0, 0)),
            ("posted", ts_in_ny(2026, 3, 8, 12, 0, 0)),
        )
        assert db.posts_today(ts_in_ny(2026, 3, 8, 15, 0, 0), tz=NY) == 2

    def test_boundary_exclusion_of_next_day(self, tmp_path):
        db = _fresh_db(tmp_path)
        # 23:59:59 on 06-15 vs 00:00:01 on 06-16.
        self._seed(
            db,
            ("posted", ts_in_ny(2026, 6, 15, 23, 59, 59)),
            ("posted", ts_in_ny(2026, 6, 16, 0, 0, 1)),
        )
        assert db.posts_today(ts_in_ny(2026, 6, 15, 12, 0, 0), tz=NY) == 1
        assert db.posts_today(ts_in_ny(2026, 6, 16, 12, 0, 0), tz=NY) == 1

    def test_old_86400_formula_is_wrong(self, tmp_path):
        # The original bug: posts_today used end = start + 86400. On the spring
        # forward day the real local midnight of 03-09 is 04:00 UTC (EDT), but
        # start+86400 lands at 05:00 UTC = 01:00 EDT on 03-09, so a post made at
        # 00:30 EDT on 03-09 (04:30 UTC) is inside the old "day" but is actually
        # the NEXT local day.
        db = _fresh_db(tmp_path)
        day_start = ts_in_ny(2026, 3, 8, 0, 0, 0)
        real_end = ts_in_ny(2026, 3, 9, 0, 0, 0)        # 04:00 UTC
        next_day_post = ts_in_ny(2026, 3, 9, 0, 30, 0)  # 04:30 UTC
        assert next_day_post > real_end                  # really the next day
        assert day_start + DAY > real_end                # old end is wrong: 1h late
        assert next_day_post < day_start + DAY           # old formula WOULD count it
        self._seed(db, ("posted", next_day_post))
        assert db.posts_today(day_start, tz=NY) == 0  # correctly NOT today