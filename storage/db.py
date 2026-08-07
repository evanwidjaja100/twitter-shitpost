import sqlite3
import time
import threading
from pathlib import Path


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
        with self._lock:
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
            self._conn.commit()

    def is_hash_seen(self, content_hash: str, cooldown_days: int) -> bool:
        cutoff = time.time() - cooldown_days * 86400
        with self._lock:
            row = self._conn.execute(
                "SELECT first_seen, post_count FROM hashes WHERE hash = ?", (content_hash,)
            ).fetchone()
            if row is None:
                return False
            return row["first_seen"] >= cutoff

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

    def record_successful_item(
        self,
        source: str,
        source_id: str,
        source_url: str,
        content_hash: str | None,
    ):
        """Record a successfully published item in one transaction.

        Both the source dedup and the media-hash dedup are written together so
        a partial write can never leave the database inconsistent.
        """
        now = time.time()
        with self._lock:
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

    def add_post(self, caption, media_path, source, source_url, content_hash, status, error=None):
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO posts (posted_at, caption, media_path, source, source_url, hash, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), caption, media_path, source, source_url, content_hash, status, error),
            )
            self._conn.commit()

    def posts_today(self) -> int:
        start_of_day = time.time() - (time.time() % 86400)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM posts WHERE posted_at >= ? AND status = 'posted'",
                (start_of_day,),
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
