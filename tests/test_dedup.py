"""Tests for moving deduplication writes to after a confirmed successful post.

Covers pick/success/failure/exception/media-prep-failure and the atomic,
idempotent single-transaction database recording.
"""

from unittest import mock

import pytest

import main
from storage.db import Database


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


def _pick_cfg():
    return {
        "tiktok": {"foryou": True, "accounts": []},
        "secrets": {"youtube_api_key": ""},
        "youtube": {"shorts_feed": False},
        "x_sources": {"accounts": []},
        "paths": {"assets_dir": "assets"},
        "filters": {"blocked_keywords": [], "cooldown_days": 30},
        "posting": {
            "caption_style": "title",
            "caption_pool": [],
            "random_caption_chance": 0.0,
            "max_caption_len": 200,
        },
    }


def _once_cfg(tmp_path):
    return {"paths": {"db_file": str(tmp_path / "bot.db")}}


class _FailingConnection:
    """Proxy for a sqlite3 connection that raises on the Nth execute call."""

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


class TestPickItemSelection:
    def test_selection_records_nothing(self, db, tmp_path):
        media = tmp_path / "m.mp4"
        media.write_bytes(b"x")
        with mock.patch("scrapers.tiktok_scraper.scrape", return_value=[_item()]), \
                mock.patch("main.prepare_item", return_value=str(media)), \
                mock.patch("pipeline.media.hash_file", return_value="deadbeef1234"):
            picked = main.pick_item(_pick_cfg(), db, mock.MagicMock())
        assert picked is not None
        assert picked["_hash"] == "deadbeef1234"
        assert not db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)


class TestCmdOnce:
    def _run(self, tmp_path, item, session_result=None, session_exc=None, pick=None):
        db = Database(str(tmp_path / "bot.db"))
        session = mock.MagicMock()
        if session_exc is not None:
            session.post.side_effect = session_exc
        else:
            session.post.return_value = session_result
        if pick is None:
            pick = _item() if item is not None else None
        with mock.patch("main._make_db", return_value=db), \
                mock.patch("publisher.x_publisher.XSession", return_value=session), \
                mock.patch("main.pick_item", return_value=pick), \
                mock.patch("main.alert") as alert:
            main.cmd_once(_once_cfg(tmp_path))
        return db, session, alert

    def test_success_records_dedup(self, tmp_path):
        db, session, alert = self._run(
            tmp_path, item=_item(), session_result={"ok": True, "reason": "posted"}
        )
        assert session.post.call_count == 1
        assert db.is_source_seen("youtube", "vid-1")
        assert db.is_hash_seen("deadbeef1234", 30)
        alert.assert_not_called()

    def test_failed_post_records_nothing(self, tmp_path):
        db, session, alert = self._run(
            tmp_path, item=_item(), session_result={"ok": False, "reason": "login"}
        )
        assert session.post.call_count == 1
        assert not db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)
        alert.assert_called_once()

    def test_readiness_timeout_does_not_write_any_success_dedup(self, tmp_path):
        item = _item(kind="video", _fingerprints=["frame-a", "frame-b"])
        db, session, alert = self._run(
            tmp_path,
            item=item,
            pick=item,
            session_result={
                "ok": False,
                "reason": "post_button_disabled_timeout",
            },
        )

        assert session.post.call_count == 1
        assert not db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)
        assert db.fingerprint_groups("video", 30) == []
        alert.assert_called_once()

    def test_exception_records_nothing(self, tmp_path):
        db = Database(str(tmp_path / "bot.db"))
        session = mock.MagicMock()
        session.post.side_effect = RuntimeError("boom")
        with mock.patch("main._make_db", return_value=db), \
                mock.patch("publisher.x_publisher.XSession", return_value=session), \
                mock.patch("main.pick_item", return_value=_item()), \
                mock.patch("main.alert"):
            with pytest.raises(RuntimeError):
                main.cmd_once(_once_cfg(tmp_path))
        assert session.post.call_count == 1
        assert not db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)

    def test_no_candidate_records_nothing(self, tmp_path):
        db, session, alert = self._run(tmp_path, item=None, pick=None)
        session.post.assert_not_called()
        assert not db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)
        alert.assert_called_once()

    @pytest.mark.parametrize(
        "reason", [
            "unverified",
            "timeout",
            "error",
            "captcha",
            "login",
            "post_button_disabled_timeout",
        ]
    )
    def test_ambiguous_or_failed_publisher_never_records(self, tmp_path, reason):
        """Every non-positive publisher result must skip permanent dedup."""
        db, session, alert = self._run(
            tmp_path, item=_item(), session_result={"ok": False, "reason": reason}
        )
        assert session.post.call_count == 1
        assert not db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)
        alert.assert_called_once()


class TestMarkItemPublished:
    def test_calls_atomic_recorder_with_item_fields(self):
        db = mock.MagicMock()
        main.mark_item_published(db, _item())
        db.finalize_successful_post.assert_called_once_with(
            caption="caption",
            media_path="media.mp4",
            source="youtube",
            source_id="vid-1",
            source_url="https://youtu.be/vid-1",
            content_hash="deadbeef1234",
            fingerprints=None,
        )

    def test_calls_atomic_recorder_when_hash_missing(self):
        db = mock.MagicMock()
        item = _item()
        item.pop("_hash")
        main.mark_item_published(db, item)
        db.finalize_successful_post.assert_called_once_with(
            caption="caption",
            media_path="media.mp4",
            source="youtube",
            source_id="vid-1",
            source_url="https://youtu.be/vid-1",
            content_hash=None,
            fingerprints=None,
        )


class TestRecordSuccessfulItem:
    def test_commits_source_and_hash_together(self, tmp_path):
        db = Database(str(tmp_path / "bot.db"))
        db.record_successful_item("youtube", "vid-1", "https://youtu.be/vid-1", "deadbeef1234")
        fresh = Database(str(tmp_path / "bot.db"))
        assert fresh.is_source_seen("youtube", "vid-1")
        assert fresh.is_hash_seen("deadbeef1234", 30)

    def test_partial_write_leaves_database_consistent(self, tmp_path):
        db = Database(str(tmp_path / "bot.db"))
        db._conn = _FailingConnection(db._conn, fail_at=2)
        with pytest.raises(RuntimeError):
            db.record_successful_item("youtube", "vid-1", "https://youtu.be/vid-1", "deadbeef1234")

        fresh = Database(str(tmp_path / "bot.db"))
        assert not fresh.is_source_seen("youtube", "vid-1")
        assert not fresh.is_hash_seen("deadbeef1234", 30)

    def test_idempotent_without_errors(self, tmp_path):
        db = Database(str(tmp_path / "bot.db"))
        db.record_successful_item("youtube", "vid-1", "u1", "deadbeef1234")
        db.record_successful_item("youtube", "vid-1", "u1", "deadbeef1234")
        assert db.is_source_seen("youtube", "vid-1")
        assert db.is_hash_seen("deadbeef1234", 30)
        row = db._conn.execute(
            "SELECT post_count AS n FROM hashes WHERE hash = 'deadbeef1234'"
        ).fetchone()
        assert row["n"] == 2

    def test_without_hash_records_source_only(self, tmp_path):
        db = Database(str(tmp_path / "bot.db"))
        db.record_successful_item("youtube", "vid-1", "u1", None)
        assert db.is_source_seen("youtube", "vid-1")
        assert not db.is_hash_seen("deadbeef1234", 30)
