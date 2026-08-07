"""Regression tests for Issue 1: record_successful_item/finalize_successful_post
are real transactions that roll back on failure.

The critical property proven here: after a mid-transaction exception the
*original* Database connection must not later flush the partial write when an
unrelated method commits.
"""

import pytest

from storage.db import Database


class _FailingConnection:
    """Proxy for a sqlite3 connection raising on the Nth execute call.

    Replaced at the Database level (Database._conn is a plain attribute), which
    avoids relying on assigning to the read-only sqlite3.Connection.execute.
    """

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


def _db(tmp_path):
    return Database(str(tmp_path / "bot.db"))


def test_successful_transaction_records_source_and_hash(tmp_path):
    db = _db(tmp_path)
    db.record_successful_item("youtube", "vid-1", "https://youtu.be/vid-1", "deadbeef1234")

    fresh = Database(str(tmp_path / "bot.db"))
    assert fresh.is_source_seen("youtube", "vid-1")
    assert fresh.is_hash_seen("deadbeef1234", 30)


def test_successful_finalize_records_post_history_and_dedup(tmp_path):
    import time

    db = _db(tmp_path)
    start = time.time()
    db.finalize_successful_post(
        caption="cap", media_path="m.mp4", source="youtube", source_id="vid-1",
        source_url="https://youtu.be/vid-1", content_hash="deadbeef1234",
        now_ts=start,
    )
    fresh = Database(str(tmp_path / "bot.db"))
    assert fresh.is_source_seen("youtube", "vid-1")
    assert fresh.is_hash_seen("deadbeef1234", 30)
    assert fresh.window_post_count(start, start + 3600) == 1
    row = fresh._conn.execute("SELECT status FROM posts").fetchone()
    assert row["status"] == "posted"


def test_mid_transaction_failure_rolls_everything_back(tmp_path):
    db = _db(tmp_path)
    db._conn = _FailingConnection(db._conn, fail_at=2)  # after source, before hash
    with pytest.raises(RuntimeError):
        db.record_successful_item("youtube", "vid-1", "url", "deadbeef1234")

    fresh = Database(str(tmp_path / "bot.db"))
    assert not fresh.is_source_seen("youtube", "vid-1")
    assert not fresh.is_hash_seen("deadbeef1234", 30)


def test_failed_transaction_not_committed_by_later_unrelated_write(tmp_path):
    """Regression for the exact missed failure mode.

    After the simulated crash, an unrelated op normally calls commit() on the
    original connection. Without explicit rollback the stale source_seen
    INSERT would be flushed by that later commit.
    """
    db = _db(tmp_path)
    db._conn = _FailingConnection(db._conn, fail_at=2)
    with pytest.raises(RuntimeError):
        db.record_successful_item("youtube", "vid-1", "url", "deadbeef1234")

    db.record_follower(123)  # unrelated write on the SAME original connection

    fresh = Database(str(tmp_path / "bot.db"))
    assert fresh.last_follower_check() is not None, "unrelated write should commit"
    assert not fresh.is_source_seen("youtube", "vid-1")
    assert not fresh.is_hash_seen("deadbeef1234", 30)


def test_connection_usable_after_rollback(tmp_path):
    db = _db(tmp_path)
    db._conn = _FailingConnection(db._conn, fail_at=2)
    with pytest.raises(RuntimeError):
        db.record_successful_item("youtube", "vid-bad", "url", "badhash")

    db.record_follower(5)
    db.record_successful_item("youtube", "vid-ok", "url", "goodhash")

    fresh = Database(str(tmp_path / "bot.db"))
    assert fresh.is_source_seen("youtube", "vid-ok")
    assert fresh.is_hash_seen("goodhash", 30)
    assert fresh.last_follower_check() is not None
    assert not fresh.is_source_seen("youtube", "vid-bad")
    assert not fresh.is_hash_seen("badhash", 30)


def test_finalize_partial_failure_also_rolls_back(tmp_path):
    db = _db(tmp_path)
    db._conn = _FailingConnection(db._conn, fail_at=3)  # posts ok, source ok, hash fails
    with pytest.raises(RuntimeError):
        db.finalize_successful_post(
            caption="cap", media_path="m", source="youtube", source_id="vid",
            source_url="https://youtu.be/v", content_hash="h",
            now_ts=1_700_000_000.0,
        )

    fresh = Database(str(tmp_path / "bot.db"))
    assert not fresh.is_source_seen("youtube", "vid")
    assert not fresh.is_hash_seen("h", 30)
    assert fresh.window_post_count(1_699_999_000.0, 1_700_000_001.0) == 0