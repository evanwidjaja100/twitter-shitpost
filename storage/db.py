import sqlite3
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path


def local_day_bounds(now_ts, tz=None) -> tuple[float, float]:
    """Epoch bounds ``[start, end)`` of the LOCAL calendar day containing `now_ts`.

    Both bounds are constructed as real local midnights: the midnight of
    ``now_ts``'s date and the midnight of the next local date. Converting each
    midnight to an epoch timestamp (rather than adding 86_400 seconds to the
    start) is what makes local days that are 23 or 25 hours long — DST spring
    forward / fall back — produce correct 23 h / 25 h windows instead of a
    fixed 24 hours.

    ``tz`` is ``None`` (host local, naive — backward compatible with the rest of
    the codebase) or an explicit ``zoneinfo.ZoneInfo`` for deterministic tests.
    """
    if tz is None:
        base = datetime.fromtimestamp(float(now_ts))
        start_dt = datetime(base.year, base.month, base.day)
        next_date = base.date() + timedelta(days=1)
        end_dt = datetime(next_date.year, next_date.month, next_date.day)
    else:
        base = datetime.fromtimestamp(float(now_ts), tz)
        start_dt = datetime(base.year, base.month, base.day, tzinfo=tz)
        next_date = base.date() + timedelta(days=1)
        end_dt = datetime(next_date.year, next_date.month, next_date.day, tzinfo=tz)
    return start_dt.timestamp(), end_dt.timestamp()


class Database:
    """SQLite-backed dedup and post history. Thread-safe via a lock."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS hashes (
                    hash TEXT PRIMARY KEY,
                    source TEXT,
                    source_url TEXT,
                    first_seen REAL,
                    last_seen REAL,
                    post_count INTEGER DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS source_seen (
                    source TEXT,
                    source_id TEXT,
                    first_seen REAL,
                    PRIMARY KEY (source, source_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posted_at REAL,
                    caption TEXT,
                    media_path TEXT,
                    source TEXT,
                    source_url TEXT,
                    hash TEXT,
                    status TEXT,
                    error TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS followers (
                    checked_at REAL PRIMARY KEY,
                    count INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS posting_windows (
                    window_id TEXT PRIMARY KEY,
                    target INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS fingerprints (
                    fingerprint TEXT PRIMARY KEY,
                    source TEXT,
                    source_url TEXT,
                    first_seen REAL,
                    last_seen REAL,
                    post_count INTEGER DEFAULT 0
                )
                """
            )
            # Legacy ``fingerprints`` rows above are intentionally retained but
            # never used for grouped decisions: they contain no reliable media
            # identity or kind, so fabricating video groups would be unsafe.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS media_perceptual_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_kind TEXT NOT NULL CHECK (media_kind IN ('image', 'video')),
                    content_hash TEXT,
                    source TEXT,
                    source_url TEXT,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    post_count INTEGER NOT NULL DEFAULT 1,
                    last_post_id INTEGER
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_perceptual_group_hash_kind
                ON media_perceptual_groups (media_kind, content_hash)
                WHERE content_hash IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_perceptual_group_kind_seen
                ON media_perceptual_groups (media_kind, last_seen)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS media_perceptual_fingerprints (
                    group_id INTEGER NOT NULL,
                    sample_index INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    PRIMARY KEY (group_id, sample_index),
                    FOREIGN KEY (group_id) REFERENCES media_perceptual_groups(id)
                )
                """
            )

    def is_hash_seen(self, content_hash: str, cooldown_days: int, now_ts: float | None = None) -> bool:
        """Whether `content_hash` is within its repost cooldown.

        The cooldown is measured from `last_seen` (the most recent successful
        publication of this media), NOT from `first_seen` — otherwise a repost
        after the first expiry would never restart the cooldown. `first_seen`
        is used only as a compatibility fallback for rows written by older
        schema versions where `last_seen` could be NULL. `now_ts` defaults to
        the real clock for backward compatibility; tests inject a fixed epoch.
        """
        now = time.time() if now_ts is None else float(now_ts)
        cutoff = now - cooldown_days * 86400
        with self._lock:
            row = self._conn.execute(
                "SELECT first_seen, last_seen FROM hashes WHERE hash = ?", (content_hash,)
            ).fetchone()
            if row is None:
                return False
            seen_at = row["last_seen"]
            if seen_at is None:
                seen_at = row["first_seen"]
            return seen_at >= cutoff

    def record_hash(self, content_hash: str, source: str, source_url: str):
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO hashes (hash, source, source_url, first_seen, last_seen, post_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(hash) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    post_count = post_count + 1
                """,
                (content_hash, source, source_url, now, now),
            )
            self._conn.commit()

    def is_source_seen(self, source: str, source_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM source_seen WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchone()
            return row is not None

    def record_source(self, source: str, source_id: str):
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO source_seen (source, source_id, first_seen) VALUES (?, ?, ?)",
                (source, source_id, now),
            )
            self._conn.commit()

    @staticmethod
    def _media_kind(media_kind: str) -> str:
        if media_kind not in ("image", "video"):
            raise ValueError("media_kind must be 'image' or 'video'")
        return media_kind

    def _write_perceptual_group(
        self,
        fingerprints,
        media_kind: str,
        source: str,
        source_url: str,
        content_hash: str | None,
        post_id: int | None,
        now: float,
    ) -> int | None:
        """Insert/update one complete group; caller owns lock and transaction."""
        if not fingerprints:
            return None
        kind = self._media_kind(media_kind)
        group_id = None
        if content_hash:
            row = self._conn.execute(
                """
                SELECT id FROM media_perceptual_groups
                WHERE media_kind = ? AND content_hash = ?
                """,
                (kind, content_hash),
            ).fetchone()
            if row is not None:
                group_id = row["id"]
                self._conn.execute(
                    """
                    UPDATE media_perceptual_groups
                    SET source = ?, source_url = ?, last_seen = ?,
                        post_count = post_count + 1,
                        last_post_id = COALESCE(?, last_post_id)
                    WHERE id = ?
                    """,
                    (source, source_url, now, post_id, group_id),
                )
                self._conn.execute(
                    "DELETE FROM media_perceptual_fingerprints WHERE group_id = ?",
                    (group_id,),
                )
        if group_id is None:
            cur = self._conn.execute(
                """
                INSERT INTO media_perceptual_groups
                    (media_kind, content_hash, source, source_url, first_seen,
                     last_seen, post_count, last_post_id)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (kind, content_hash, source, source_url, now, now, post_id),
            )
            group_id = cur.lastrowid
        for sample_index, fingerprint in enumerate(fingerprints):
            self._conn.execute(
                """
                INSERT INTO media_perceptual_fingerprints
                    (group_id, sample_index, fingerprint)
                VALUES (?, ?, ?)
                """,
                (group_id, sample_index, fingerprint),
            )
        return group_id

    def record_fingerprints(
        self,
        fingerprints,
        source: str,
        source_url: str,
        now_ts=None,
        media_kind: str = "image",
        content_hash: str | None = None,
    ):
        """Record one typed perceptual group atomically.

        This compatibility helper is useful for imports/tests. Production
        success uses :meth:`finalize_successful_post` so post/source/hash/group
        state commits together.
        """
        if not fingerprints:
            return None
        now = time.time() if now_ts is None else float(now_ts)
        with self._lock:
            try:
                group_id = self._write_perceptual_group(
                    fingerprints, media_kind, source, source_url,
                    content_hash, None, now,
                )
                self._conn.commit()
                return group_id
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def fingerprint_groups(
        self,
        media_kind: str,
        cooldown_days: int,
        now_ts: float | None = None,
    ) -> list[dict]:
        """Recent same-kind history, preserving one media group per result."""
        kind = self._media_kind(media_kind)
        now = time.time() if now_ts is None else float(now_ts)
        cutoff = now - cooldown_days * 86400
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT g.id AS group_id, g.media_kind, g.content_hash,
                       g.first_seen, g.last_seen, f.sample_index, f.fingerprint
                FROM media_perceptual_groups AS g
                JOIN media_perceptual_fingerprints AS f ON f.group_id = g.id
                WHERE g.media_kind = ? AND g.last_seen >= ?
                ORDER BY g.id, f.sample_index
                """,
                (kind, cutoff),
            ).fetchall()
        groups = []
        by_id = {}
        for row in rows:
            group = by_id.get(row["group_id"])
            if group is None:
                group = {
                    "group_id": row["group_id"],
                    "media_kind": row["media_kind"],
                    "content_hash": row["content_hash"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "fingerprints": [],
                }
                by_id[row["group_id"]] = group
                groups.append(group)
            group["fingerprints"].append(row["fingerprint"])
        return groups

    def record_successful_item(
        self,
        source: str,
        source_id: str,
        source_url: str,
        content_hash: str | None,
    ):
        """Record a successfully published item atomically.

        Source dedup and media-hash dedup are written together and committed
        once. On any exception the whole transaction is rolled back so no
        partial write can ever be flushed by a later, unrelated commit() on
        this shared connection. The connection is left in a clean (non-
        transaction) state and remains usable afterwards.
        """
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO source_seen (source, source_id, first_seen) VALUES (?, ?, ?)",
                    (source, source_id, now),
                )
                if content_hash:
                    self._conn.execute(
                        """
                        INSERT INTO hashes (hash, source, source_url, first_seen, last_seen, post_count)
                        VALUES (?, ?, ?, ?, ?, 1)
                        ON CONFLICT(hash) DO UPDATE SET
                            last_seen = excluded.last_seen,
                            post_count = post_count + 1
                        """,
                        (content_hash, source, source_url, now, now),
                    )
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def finalize_successful_post(
        self,
        caption,
        media_path,
        source: str,
        source_id: str,
        source_url: str,
        content_hash: str | None,
        fingerprints=None,
        media_kind: str | None = None,
        error: str | None = "posted",
        now_ts=None,
    ):
        """Record a confirmed successful post in one transaction.

        Writes the posts history row (status='posted'), the source dedup and
        the media-hash dedup (plus the perceptual fingerprint rows, when
        ``fingerprints`` is provided) together so post history, dedup state and
        the scheduler's per-window success count (derived from the posts table)
        can never disagree. Any exception rolls the whole transaction back.
        """
        now = now_ts if now_ts is not None else time.time()
        with self._lock:
            try:
                post_cur = self._conn.execute(
                    """
                    INSERT INTO posts (posted_at, caption, media_path, source, source_url, hash, status, error)
                    VALUES (?, ?, ?, ?, ?, ?, 'posted', ?)
                    """,
                    (now, caption, media_path, source, source_url, content_hash, error),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO source_seen (source, source_id, first_seen) VALUES (?, ?, ?)",
                    (source, source_id, now),
                )
                if content_hash:
                    self._conn.execute(
                        """
                        INSERT INTO hashes (hash, source, source_url, first_seen, last_seen, post_count)
                        VALUES (?, ?, ?, ?, ?, 1)
                        ON CONFLICT(hash) DO UPDATE SET
                            last_seen = excluded.last_seen,
                            post_count = post_count + 1
                        """,
                        (content_hash, source, source_url, now, now),
                    )
                if fingerprints:
                    kind = media_kind or ("image" if len(fingerprints) == 1 else "video")
                    self._write_perceptual_group(
                        fingerprints, kind, source, source_url,
                        content_hash, post_cur.lastrowid, now,
                    )
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def add_post(self, caption, media_path, source, source_url, content_hash, status, error=None):
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO posts (posted_at, caption, media_path, source, source_url, hash, status, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (time.time(), caption, media_path, source, source_url, content_hash, status, error),
                )
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def posts_today(self, now_ts: float | None = None, tz=None) -> int:
        """Successful posts in the local calendar day of `now_ts`.

        Day boundaries are the actual local midnights of the containing date and
        the next local date — never ``start + 86_400`` — so 23-hour (spring
        forward) and 25-hour (fall back) local days are counted correctly.
        ``tz`` defaults to the host local timezone (naive); pass an explicit
        ``zoneinfo.ZoneInfo`` for deterministic tests. `now_ts` defaults to the
        real clock for backward compatibility.
        """
        start, end = local_day_bounds(
            time.time() if now_ts is None else float(now_ts), tz=tz,
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM posts WHERE posted_at >= ? AND posted_at < ? AND status = 'posted'",
                (start, end),
            ).fetchone()
            return row["n"]

    def get_window_target(self, window_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT target FROM posting_windows WHERE window_id = ?", (window_id,)
            ).fetchone()
            return row["target"] if row else None

    def set_window_target(self, window_id: str, target: int):
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO posting_windows (window_id, target, created_at) VALUES (?, ?, ?)",
                    (window_id, int(target), time.time()),
                )
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def window_post_count(self, start_ts: float, end_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM posts WHERE status = 'posted' AND posted_at >= ? AND posted_at < ?",
                (float(start_ts), float(end_ts)),
            ).fetchone()
            return row["n"]

    def record_follower(self, count: int):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO followers (checked_at, count) VALUES (?, ?)",
                (time.time(), int(count)),
            )
            self._conn.commit()

    def follower_history(self, limit: int = 60) -> list[tuple[float, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT checked_at, count FROM followers ORDER BY checked_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r["checked_at"], r["count"]) for r in reversed(rows)]

    def last_follower_check(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(checked_at) AS t FROM followers"
            ).fetchone()
            return row["t"] if row and row["t"] is not None else None

    def stats(self) -> dict:
        with self._lock:
            hashes = self._conn.execute("SELECT COUNT(*) AS n FROM hashes").fetchone()["n"]
            seen = self._conn.execute("SELECT COUNT(*) AS n FROM source_seen").fetchone()["n"]
            posts = self._conn.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
            posted_ok = self._conn.execute(
                "SELECT COUNT(*) AS n FROM posts WHERE status = 'posted'"
            ).fetchone()["n"]
            return {
                "hashes": hashes,
                "source_seen": seen,
                "posts_total": posts,
                "posts_ok": posted_ok,
            }
