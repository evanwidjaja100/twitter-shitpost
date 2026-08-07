"""Regression tests for the hash repost-cooldown fix (HIGH).

The cooldown must be measured from `last_seen` — the most recent successful
publication — not from `first_seen`. After the first cooldown expiry, a
successful repost updates `last_seen` so the cooldown restarts; a failed
publication or a rolled-back finalization must leave `last_seen`/`post_count`
untouched.

All timestamps are fixed day offsets from a constant epoch so tests never
sleep and never depend on the system clock:
    DAY0 = 1_700_000_000.0, day(N) = DAY0 + N * 86400
A 30-day cooldown is used throughout: with `timestamp >= cutoff` (inclusive),
a post at DAY0 is still blocked at exactly DAY0 + 30 days and eligible one
second later.
"""

from unittest import mock

import pytest

import main
from storage.db import Database

DAILY = 86400
DAY0 = 1_700_000_000.0


def day(n: int, offset_seconds: int = 0) -> float:
    return DAY0 + n * DAILY + offset_seconds


def _db(tmp_path):
    return Database(str(tmp_path / "bot.db"))


def _post_hash(db, h, at: float):
    """Authoritative production success path at a fixed timestamp."""
    db.finalize_successful_post(
        caption="c", media_path="m.mp4", source="youtube", source_id=f"vid-{int(at)}",
        source_url="https://youtu.be/u", content_hash=h, now_ts=at,
    )


def _hash_row(db, h):
    return dict(db._conn.execute(
        "SELECT hash, first_seen, last_seen, post_count FROM hashes WHERE hash = ?",
        (h,),
    ).fetchone())


class TestCooldownLifecycle:
    """A single hash walked through its full cooldown lifecycle."""

    def test_first_post_starts_cooldown(self, tmp_path):
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))
        assert db.is_hash_seen("ABC", 30, now_ts=day(0)) is True

    def test_still_blocked_during_cooldown(self, tmp_path):
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))
        assert db.is_hash_seen("ABC", 30, now_ts=day(29)) is True

    def test_eligible_after_cooldown_expires(self, tmp_path):
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))
        assert db.is_hash_seen("ABC", 30, now_ts=day(31)) is False

    def test_boundary_is_inclusive(self, tmp_path):
        """Existing convention is `timestamp >= cutoff`: exactly 30 days later
        the post is still 'seen'; one second after that it is eligible."""
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))
        assert db.is_hash_seen("ABC", 30, now_ts=day(30)) is True
        assert db.is_hash_seen("ABC", 30, now_ts=day(30) + 1) is False

    def test_successful_repost_restarts_cooldown(self, tmp_path):
        """The mandatory regression sequence:
        Day 0 post -> Day 31 eligible -> Day 31 repost -> immediately blocked.
        Fails against the old first_seen-based implementation."""
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))
        assert db.is_hash_seen("ABC", 30, now_ts=day(31)) is False  # eligible

        _post_hash(db, "ABC", day(31))  # confirmed successful repost
        assert db.is_hash_seen("ABC", 30, now_ts=day(31)) is True  # blocked again

    def test_cooldown_measured_from_second_post(self, tmp_path):
        """After a Day 31 repost the cooldown runs from Day 31, not Day 0."""
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))
        _post_hash(db, "ABC", day(31))

        assert db.is_hash_seen("ABC", 30, now_ts=day(60)) is True  # 29 days since repost
        assert db.is_hash_seen("ABC", 30, now_ts=day(62)) is False  # 31 days since repost

    def test_first_seen_remains_historical(self, tmp_path):
        """Second publication must not overwrite first_seen."""
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))
        _post_hash(db, "ABC", day(31))

        row = _hash_row(db, "ABC")
        assert row["first_seen"] == day(0)
        assert row["last_seen"] == day(31)
        assert row["post_count"] == 2

    def test_finalize_updates_last_seen_only(self, tmp_path):
        """Production success path: second finalize updates last_seen + count,
        leaves first_seen untouched."""
        db = _db(tmp_path)
        _post_hash(db, "XYZ", day(0))
        _post_hash(db, "XYZ", day(31))

        row = _hash_row(db, "XYZ")
        assert row["first_seen"] == day(0)
        assert row["last_seen"] == day(31)
        assert row["post_count"] == 2
        assert db.is_hash_seen("XYZ", 30, now_ts=day(31)) is True


class TestFailedAndRolledBackPublications:
    """Failures must never restart or shift the cooldown."""

    def _item(self, **overrides):
        item = {
            "source": "youtube",
            "source_id": "vid-fail",
            "source_url": "https://youtu.be/vid-fail",
            "title": "clip",
            "score": 10.0,
            "_caption": "caption",
            "_media_path": "media.mp4",
            "_hash": "ABC",
        }
        item.update(overrides)
        return item

    def test_failed_publication_does_not_restart_cooldown(self, tmp_path):
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))  # successful post at Day 0

        session = mock.MagicMock()
        session.post.return_value = {"ok": False, "reason": "captcha"}
        with mock.patch("main._make_db", return_value=db), \
                mock.patch(
                    "publisher.x_publisher.XSession", return_value=session
                ), mock.patch(
                    "main.pick_item", return_value=self._item()
                ), mock.patch("main.alert"):
            main.cmd_once({"paths": {"db_file": str(tmp_path / "bot.db")}})

        row = _hash_row(db, "ABC")
        assert row["last_seen"] == day(0)   # cooldown NOT restarted
        assert row["post_count"] == 1       # failed attempt not counted
        assert row["first_seen"] == day(0)
        assert not db.is_source_seen("youtube", "vid-fail")  # no permanent dedup

    def test_finalization_failure_rolls_back_and_preserves_cooldown(self, tmp_path):
        db = _db(tmp_path)
        _post_hash(db, "ABC", day(0))

        real = db._conn
        db._conn = _FailingConnection(real, fail_at=3)  # posts ok, source ok, hash fails
        with pytest.raises(RuntimeError):
            _post_hash(db, "ABC", day(31))
        db._conn = real

        fresh = Database(str(tmp_path / "bot.db"))
        row = _hash_row(fresh, "ABC")
        assert row["first_seen"] == day(0)
        assert row["last_seen"] == day(0)   # repost timestamp rolled back
        assert row["post_count"] == 1


class TestLegacyNullLastSeen:
    """Old databases could hold last_seen = NULL; first_seen is the fallback."""

    def test_null_last_seen_falls_back_to_first_seen(self, tmp_path):
        db = _db(tmp_path)
        db._conn.execute(
            """
            INSERT INTO hashes (hash, source, source_url, first_seen, last_seen, post_count)
            VALUES ('LEGACY', 'youtube', 'u', ?, NULL, 1)
            """,
            (day(0),),
        )
        db._conn.commit()

        assert db.is_hash_seen("LEGACY", 30, now_ts=day(0)) is True
        assert db.is_hash_seen("LEGACY", 30, now_ts=day(29)) is True
        assert db.is_hash_seen("LEGACY", 30, now_ts=day(31)) is False


class _FailingConnection:
    """Proxy raising on the Nth execute call (mirrors test_db_atomicity)."""

    def __init__(self, real, fail_at):
        self._real = real
        self._fail_at = fail_at
        self._n = 0

    def execute(self, *args, **kwargs):
        self._n += 1
        if self._n == self._fail_at:
            raise RuntimeError("simulated crash mid-write")
        return self._real.execute(*args, **kwargs)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()
