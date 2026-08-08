"""Tests for the daemon's pre-post quota recheck (Issue 3, second part).

Scheduled slots are only an estimate: between slot computation and wake time
the state can change (another process posts, a manual post happens, a previous
scheduled post succeeded, the day/window rolled over, the persisted target was
reached). These tests drive `main.attempt_slot()` — the function the daemon
calls immediately after `sleep_until()` and before `pick_item()`/`session.post()`
— with a real temp SQLite Database and mocked pick/session. No real sleeps, no
live X, no browser.
"""

import random
from datetime import datetime
from unittest import mock

import main
import scheduler
from storage.db import Database


def _db(tmp_path):
    return Database(str(tmp_path / "bot.db"))


def _post_at(db, dt):
    ts = dt.timestamp()
    db.finalize_successful_post(
        caption="c", media_path="m", source="youtube", source_id=f"vid-{int(ts)}",
        source_url="https://youtu.be/u", content_hash=f"h{int(ts)}", now_ts=ts,
    )


def _cfg(tmp_path):
    return {
        "paths": {"db_file": str(tmp_path / "bot.db")},
        "posting": {
            "min_posts_per_day": 3,
            "max_posts_per_day": 6,
            "active_hours_start": 16,
            "active_hours_end": 1,
        },
        "safety": {
            "max_daily_posts_absolute": 10,
            "stop_on_login_failure": True,
            "retry_backoff_minutes": 1,
        },
    }


def _item(**overrides):
    item = {
        "source": "youtube",
        "source_id": "vid-1",
        "source_url": "https://youtu.be/vid-1",
        "title": "some clip",
        "score": 10.0,
        "_caption": "caption",
        "_media_path": "media.mp4",
        "_hash": "deadbeef1234",
    }
    item.update(overrides)
    return item


class TestPrePostQuotaRecheck:
    """Each test: a valid slot is computed, then the world changes while the
    daemon 'sleeps', then the daemon wakes and calls attempt_slot()."""

    def _attempt(self, db, cfg, now, session=None, pick_return=None):
        if session is None:
            session = mock.MagicMock()
            session.post.return_value = {"ok": True, "reason": "posted"}
        pick = mock.MagicMock(return_value=pick_return)
        with mock.patch("main.pick_item", pick), mock.patch("main.alert"):
            result = main.attempt_slot(cfg, db, session, now=now, rng=random.Random(1))
        return result, session, pick

    def test_daily_absolute_cap_reached_while_sleeping(self, tmp_path):
        """G: slot computed with room, daily cap reached during 'sleep'."""
        db = _db(tmp_path)
        cfg = _cfg(tmp_path)
        for minute in range(0, 8, 2):  # 4 posts today already
            _post_at(db, datetime(2026, 1, 6, 0, minute))

        now = datetime(2026, 1, 6, 17, 0)
        slots = scheduler.remaining_slots(
            db, 3, 6, 16, 1, max_absolute=10, now=now, rng=random.Random(1)
        )
        assert slots  # a valid future slot existed before the world changed

        for minute in range(8, 14):  # while 'sleeping': 6 more posts today -> cap 10
            _post_at(db, datetime(2026, 1, 6, 0, minute))

        result, session, pick = self._attempt(db, cfg, now)
        assert result["outcome"] == "vetoed"
        assert result["reason"] == "daily_absolute_cap"
        pick.assert_not_called()
        session.post.assert_not_called()

    def test_logical_target_reached_while_sleeping(self, tmp_path):
        """H: another post reached the persisted window target during 'sleep'."""
        db = _db(tmp_path)
        cfg = _cfg(tmp_path)
        now = datetime(2026, 1, 6, 17, 0)
        slots = scheduler.remaining_slots(
            db, 3, 6, 16, 1, max_absolute=10, now=now, rng=random.Random(1)
        )
        assert slots
        target = db.get_window_target("2026-01-06")
        assert target is not None

        for i in range(target):  # while 'sleeping': window fills up completely
            _post_at(db, datetime(2026, 1, 6, 17, 10 + i))

        result, session, pick = self._attempt(db, cfg, now)
        assert result["outcome"] == "vetoed"
        assert result["reason"] == "target_reached"
        pick.assert_not_called()
        session.post.assert_not_called()

    def test_lowered_max_reached_while_sleeping(self, tmp_path):
        """Old persisted target 6 + config max lowered to 4 + 4 window successes
        -> the fresh recheck clamps to 4 and vetoes. Stale slot must not post."""
        db = _db(tmp_path)
        cfg = _cfg(tmp_path)
        now = datetime(2026, 1, 6, 17, 0)
        slots = scheduler.remaining_slots(
            db, 3, 6, 16, 1, max_absolute=10, now=now, rng=random.Random(1)
        )
        assert slots  # a valid future slot existed under the old config
        assert db.get_window_target("2026-01-06") is not None
        db.set_window_target("2026-01-06", 6)  # guarantee target above new max

        cfg["posting"]["max_posts_per_day"] = 4  # config lowered mid-window
        for i in range(4):  # while 'sleeping': 4 successful window posts
            _post_at(db, datetime(2026, 1, 6, 17, 10 + i))

        result, session, pick = self._attempt(db, cfg, now)
        assert result["outcome"] == "vetoed"
        assert result["reason"] == "target_reached"
        assert result["state"]["effective_target"] == 4
        pick.assert_not_called()
        session.post.assert_not_called()

    def test_window_expired_while_sleeping(self, tmp_path):
        """I: wake time is outside the active logical window."""
        db = _db(tmp_path)
        cfg = _cfg(tmp_path)
        slots = scheduler.remaining_slots(
            db, 3, 6, 16, 1, max_absolute=10,
            now=datetime(2026, 1, 6, 23, 30), rng=random.Random(1),
        )
        assert slots  # window Jan 6 16:00 -> Jan 7 01:00 had room

        result, session, pick = self._attempt(
            db, cfg, now=datetime(2026, 1, 7, 2, 0)  # daemon wakes after 01:00
        )
        assert result["outcome"] == "vetoed"
        assert result["reason"] == "window_inactive"
        pick.assert_not_called()
        session.post.assert_not_called()

    def test_normal_post_still_works(self, tmp_path):
        """J: inside active window, below target, below daily cap -> posts."""
        db = _db(tmp_path)
        cfg = _cfg(tmp_path)
        session = mock.MagicMock()
        session.post.return_value = {"ok": True, "reason": "posted"}

        result, session, pick = self._attempt(
            db, cfg, now=datetime(2026, 1, 6, 17, 0),
            session=session, pick_return=_item(),
        )
        assert result["outcome"] == "posted"
        pick.assert_called_once()
        session.post.assert_called_once()
        assert db.is_source_seen("youtube", "vid-1")
        assert db.is_hash_seen("deadbeef1234", 30)

    def test_failed_post_still_records_failure_only(self, tmp_path):
        """Failed publisher result after an allowed slot: history row, no dedup."""
        db = _db(tmp_path)
        cfg = _cfg(tmp_path)
        session = mock.MagicMock()
        session.post.return_value = {"ok": False, "reason": "captcha"}

        result, session, pick = self._attempt(
            db, cfg, now=datetime(2026, 1, 6, 17, 0),
            session=session, pick_return=_item(),
        )
        assert result["outcome"] == "failed"
        assert result["reason"] == "captcha"
        session.post.assert_called_once()
        assert not db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)
        row = db._conn.execute("SELECT status FROM posts").fetchone()
        assert row["status"] == "failed"
