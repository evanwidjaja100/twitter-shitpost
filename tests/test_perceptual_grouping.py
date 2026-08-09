"""Typed per-media perceptual history, migration, cooldown, and atomicity."""

import sqlite3

import pytest

from pipeline import perceptual
from storage.db import Database


NOW = 1_700_000_000.0


def _finalize(
    db,
    source_id="vid-1",
    content_hash="hash-1",
    fingerprints=None,
    media_kind="video",
    now=NOW,
):
    db.finalize_successful_post(
        caption="caption",
        media_path="media.mp4",
        source="youtube",
        source_id=source_id,
        source_url=f"https://youtu.be/{source_id}",
        content_hash=content_hash,
        fingerprints=fingerprints or [
            "1111111111111111",
            "4444444444444444",
            "7777777777777777",
        ],
        media_kind=media_kind,
        now_ts=now,
    )


def _table_count(db, table):
    return db._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_cross_video_matches_cannot_accumulate(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    a = "1111111111111111"
    b = "2222222222222222"
    c = "3333333333333333"
    db.record_fingerprints([a], "x", "old-a", media_kind="video", content_hash="a")
    db.record_fingerprints([b], "x", "old-b", media_kind="video", content_hash="b")

    groups = db.fingerprint_groups("video", 30)
    assert [group["fingerprints"] for group in groups] == [[a], [b]]
    assert not any(
        perceptual.is_near_duplicate([a, b, c], group["fingerprints"])
        for group in groups
    )


def test_majority_within_one_video_group_matches(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    historical = [
        "1111111111111111",
        "2222222222222222",
        "9999999999999999",
    ]
    candidate = [
        "1111111111111111",
        "2222222222222222",
        "3333333333333333",
    ]
    db.record_fingerprints(
        historical, "x", "old", media_kind="video", content_hash="old"
    )
    group = db.fingerprint_groups("video", 30)[0]
    assert perceptual.is_near_duplicate(candidate, group["fingerprints"])


def test_one_historical_frame_cannot_be_double_counted():
    a = "1111111111111111"
    candidate = [a, a, "ffffffffffffffff"]
    assert not perceptual.is_near_duplicate(candidate, [a])


def test_image_and_video_histories_are_isolated(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    shared = "0123456789abcdef"
    db.record_fingerprints(
        [shared], "x", "image", media_kind="image", content_hash="image-hash"
    )
    db.record_fingerprints(
        [shared, shared, shared], "x", "video", media_kind="video",
        content_hash="video-hash",
    )
    assert [g["media_kind"] for g in db.fingerprint_groups("image", 30)] == ["image"]
    assert [g["media_kind"] for g in db.fingerprint_groups("video", 30)] == ["video"]
    assert len(db.fingerprint_groups("image", 30)[0]["fingerprints"]) == 1
    assert len(db.fingerprint_groups("video", 30)[0]["fingerprints"]) == 3


def test_group_cooldown_and_successful_repost_refresh(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    _finalize(db, now=NOW)
    first = db.fingerprint_groups("video", 30, now_ts=NOW)[0]
    assert db.fingerprint_groups("video", 30, now_ts=NOW + 29 * 86400)
    assert not db.fingerprint_groups("video", 30, now_ts=NOW + 31 * 86400)

    _finalize(db, source_id="vid-2", now=NOW + 31 * 86400)
    refreshed = db.fingerprint_groups("video", 30, now_ts=NOW + 31 * 86400)[0]
    assert refreshed["group_id"] == first["group_id"]
    assert refreshed["last_seen"] == NOW + 31 * 86400
    row = db._conn.execute(
        "SELECT post_count FROM media_perceptual_groups WHERE id = ?",
        (first["group_id"],),
    ).fetchone()
    assert row["post_count"] == 2


def test_failed_post_row_does_not_refresh_perceptual_group(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    _finalize(db, now=NOW)
    before = db.fingerprint_groups("video", 30, now_ts=NOW)[0]
    db.add_post("c", "m", "youtube", "failed", "hash-1", "failed", "captcha")
    after = db.fingerprint_groups("video", 30, now_ts=NOW)[0]
    assert after["last_seen"] == before["last_seen"]
    row = db._conn.execute(
        "SELECT post_count FROM media_perceptual_groups WHERE id = ?",
        (before["group_id"],),
    ).fetchone()
    assert row["post_count"] == 1


def test_legacy_flat_rows_are_preserved_but_ignored(tmp_path):
    path = tmp_path / "legacy-flat.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE fingerprints (
            fingerprint TEXT PRIMARY KEY, source TEXT, source_url TEXT,
            first_seen REAL, last_seen REAL, post_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO fingerprints VALUES ('legacy-frame', 'x', 'u', ?, ?, 1)",
        (NOW, NOW),
    )
    conn.execute(
        """
        CREATE TABLE hashes (
            hash TEXT PRIMARY KEY, source TEXT, source_url TEXT,
            first_seen REAL, last_seen REAL, post_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO hashes VALUES ('exact', 'x', 'u', ?, ?, 1)", (NOW, NOW)
    )
    conn.commit()
    conn.close()

    db = Database(str(path))
    assert db.fingerprint_groups("image", 30, now_ts=NOW) == []
    assert db.fingerprint_groups("video", 30, now_ts=NOW) == []
    assert db.is_hash_seen("exact", 30, now_ts=NOW)
    legacy = db._conn.execute(
        "SELECT fingerprint FROM fingerprints"
    ).fetchall()
    assert [row["fingerprint"] for row in legacy] == ["legacy-frame"]


def test_pre_perceptual_and_fresh_migrations_are_idempotent(tmp_path):
    pre = tmp_path / "pre.db"
    conn = sqlite3.connect(pre)
    conn.execute(
        """
        CREATE TABLE hashes (
            hash TEXT PRIMARY KEY, source TEXT, source_url TEXT,
            first_seen REAL, last_seen REAL, post_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO hashes VALUES ('kept', 'x', 'u', ?, ?, 1)", (NOW, NOW)
    )
    conn.commit()
    conn.close()

    first = Database(str(pre))
    assert first.is_hash_seen("kept", 30, now_ts=NOW)
    _finalize(first)
    group_id = first.fingerprint_groups("video", 30, now_ts=NOW)[0]["group_id"]
    first._conn.close()

    reopened = Database(str(pre))
    assert reopened.fingerprint_groups("video", 30, now_ts=NOW)[0]["group_id"] == group_id
    assert _table_count(reopened, "media_perceptual_groups") == 1

    fresh = Database(str(tmp_path / "fresh.db"))
    assert fresh.fingerprint_groups("image", 30, now_ts=NOW) == []


class _SqlFailureConnection:
    def __init__(self, real, sql_fragment, occurrence=1):
        self.real = real
        self.sql_fragment = sql_fragment
        self.occurrence = occurrence
        self.seen = 0

    def execute(self, sql, *args, **kwargs):
        if self.sql_fragment in " ".join(sql.split()):
            self.seen += 1
            if self.seen == self.occurrence:
                raise RuntimeError("injected perceptual write failure")
        return self.real.execute(sql, *args, **kwargs)

    def commit(self):
        return self.real.commit()

    def rollback(self):
        return self.real.rollback()


@pytest.mark.parametrize(
    ("fragment", "occurrence"),
    (
        ("INSERT INTO media_perceptual_groups", 1),
        ("INSERT INTO media_perceptual_fingerprints", 1),
        ("INSERT INTO media_perceptual_fingerprints", 2),
    ),
)
def test_new_group_write_failures_roll_back_every_success_record(
    tmp_path, fragment, occurrence
):
    db = Database(str(tmp_path / "bot.db"))
    real = db._conn
    db._conn = _SqlFailureConnection(real, fragment, occurrence)
    with pytest.raises(RuntimeError, match="injected"):
        _finalize(db)
    db._conn = real

    assert _table_count(db, "posts") == 0
    assert _table_count(db, "source_seen") == 0
    assert _table_count(db, "hashes") == 0
    assert _table_count(db, "media_perceptual_groups") == 0
    assert _table_count(db, "media_perceptual_fingerprints") == 0
    db.record_follower(123)
    assert db.follower_history()[-1][1] == 123


def test_group_cooldown_update_failure_rolls_back_repost(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    _finalize(db, now=NOW)
    original = db.fingerprint_groups("video", 30, now_ts=NOW)[0]
    real = db._conn
    db._conn = _SqlFailureConnection(real, "UPDATE media_perceptual_groups")
    with pytest.raises(RuntimeError, match="injected"):
        _finalize(db, source_id="vid-2", now=NOW + 31 * 86400)
    db._conn = real

    assert _table_count(db, "posts") == 1
    assert _table_count(db, "source_seen") == 1
    assert _table_count(db, "hashes") == 1
    assert _table_count(db, "media_perceptual_groups") == 1
    assert _table_count(db, "media_perceptual_fingerprints") == 3
    current = db.fingerprint_groups("video", 60, now_ts=NOW + 31 * 86400)[0]
    assert current["group_id"] == original["group_id"]
    assert current["last_seen"] == NOW
    hash_row = db._conn.execute(
        "SELECT last_seen, post_count FROM hashes WHERE hash = 'hash-1'"
    ).fetchone()
    assert hash_row["last_seen"] == NOW
    assert hash_row["post_count"] == 1
