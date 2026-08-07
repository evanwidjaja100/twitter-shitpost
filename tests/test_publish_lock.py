"""Regression tests for single-instance publishing protection (HIGH).

The bot must never run two publishing processes for the same bot/database at
the same time: `daemon + daemon`, `daemon + once`, and `once + once` must be
impossible. The lock is an OS-exclusive file lock (msvcrt on Windows, flock on
POSIX) so it is automatically released when the owner exits or crashes, and a
stale lock file alone is harmless.

Tests A-E use REAL separate child processes to prove the OS lock excludes a
second process (lock semantics for multiple fds inside one process are not
portable). Tests F-J drive the command entry paths with mocks/fakes against a
real temp lock file. No browser, no network, no real posting.
"""

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

import main
from publishing_lock import (
    PublishLock,
    PublishLockUnavailable,
    lock_path_from_cfg,
    publishing_lock,
)
from storage.db import Database

ROOT = str(Path(__file__).resolve().parent.parent)

CHILD_SCRIPT = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from publishing_lock import PublishLock, PublishLockUnavailable

path = sys.argv[2]
mode = sys.argv[3]
lock = PublishLock(path)
try:
    lock.acquire(command="child-test")
except PublishLockUnavailable:
    print("DENIED", flush=True)
    sys.exit(0)
print("HELD", flush=True)
if mode == "hold":
    time.sleep(60)          # parent will terminate us
elif mode == "release":
    lock.release()
    print("RELEASED", flush=True)
elif mode == "acquire":
    lock.release()
    print("RELEASED", flush=True)
"""


def _spawn(path, mode):
    return subprocess.Popen(
        [sys.executable, "-c", CHILD_SCRIPT, ROOT, str(path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _readline(proc, timeout=30.0):
    q = queue.Queue()
    threading.Thread(target=lambda: q.put(proc.stdout.readline()), daemon=True).start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        proc.kill()
        raise AssertionError("child produced no output within timeout")
    if line == "":
        raise AssertionError("child exited without printing a status line")
    return line.strip()


def _kill(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)


# ---------------------------------------------------------------- lock module

class TestSingleInstanceLock:
    def test_first_process_acquires_and_releases(self, tmp_path):
        path = tmp_path / "publisher.lock"
        lock = PublishLock(path)
        lock.acquire(command="test-a")
        assert lock.held
        lock.release()
        assert not lock.held

    def test_second_process_denied(self, tmp_path):
        """Process A owns the lock; a second distinct process is denied."""
        path = str(tmp_path / "publisher.lock")
        a = _spawn(path, "hold")
        try:
            assert _readline(a) == "HELD"
            b = _spawn(path, "hold")
            assert _readline(b) == "DENIED"
            b.wait(timeout=30)
        finally:
            _kill(a)

    def test_lock_available_after_normal_release(self, tmp_path):
        """A releases; B can immediately acquire."""
        path = str(tmp_path / "publisher.lock")
        a = _spawn(path, "release")
        assert _readline(a) == "HELD"
        assert _readline(a) == "RELEASED"
        a.wait(timeout=30)
        assert a.returncode == 0

        b = _spawn(path, "acquire")
        assert _readline(b) == "HELD"
        b.wait(timeout=30)
        assert b.returncode == 0

    def test_lock_available_after_process_termination(self, tmp_path):
        """Crash/kill path: OS releases the lock when the owner dies."""
        path = str(tmp_path / "publisher.lock")
        a = _spawn(path, "hold")
        assert _readline(a) == "HELD"
        a.kill()
        a.wait(timeout=30)
        assert a.returncode != 0

        b = _spawn(path, "acquire")
        assert _readline(b) == "HELD"
        b.wait(timeout=30)
        assert b.returncode == 0

    def test_stale_lock_file_is_harmless(self, tmp_path):
        """A lock file left on disk with nobody holding the OS lock is fine."""
        path = tmp_path / "publisher.lock"
        path.write_text("pid=999999 started=1970-01-01 command=stale\n")
        lock = PublishLock(path)
        lock.acquire()
        assert lock.held
        lock.release()

    @pytest.mark.skipif(
        os.name != "nt",
        reason="in-process fd lock semantics differ on POSIX; subprocess tests cover it",
    )
    def test_second_handle_in_same_process_denied(self, tmp_path):
        path = str(tmp_path / "publisher.lock")
        a = PublishLock(path)
        a.acquire()
        try:
            with pytest.raises(PublishLockUnavailable):
                PublishLock(path).acquire()
        finally:
            a.release()

    def test_lock_path_independent_of_cwd(self, tmp_path, monkeypatch):
        cfg = {"paths": {"db_file": "data/bot.db"}}
        p1 = lock_path_from_cfg(cfg)
        assert p1.is_absolute()
        assert p1.name == "publisher.lock"

        monkeypatch.chdir(tmp_path)  # different CWD -> same absolute path
        p2 = lock_path_from_cfg(cfg)
        assert p2 == p1

    def test_released_on_exception(self, tmp_path):
        cfg = {"paths": {"db_file": str(tmp_path / "bot.db")}}
        with pytest.raises(RuntimeError):
            with publishing_lock(cfg, "test"):
                raise RuntimeError("boom")
        lock = PublishLock(lock_path_from_cfg(cfg))
        lock.acquire()  # reusable after the exception walked out
        lock.release()


# ---------------------------------------------------------------- entry points

def _once_cfg(tmp_path):
    return {"paths": {"db_file": str(tmp_path / "bot.db")}}


def _daemon_cfg(tmp_path):
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
        "title": "clip",
        "score": 10.0,
        "_caption": "caption",
        "_media_path": "media.mp4",
        "_hash": "deadbeef1234",
    }
    item.update(overrides)
    return item


class TestPublisherEntryPoints:
    def test_daemon_refused_when_lock_held(self, tmp_path):
        """Second daemon refuses BEFORE starting the browser or any loop."""
        cfg = _daemon_cfg(tmp_path)
        holder = PublishLock(lock_path_from_cfg(cfg))
        holder.acquire(command="daemon")
        try:
            with mock.patch("publisher.x_publisher.XSession") as xs, \
                    mock.patch("main.pick_item") as pick, \
                    mock.patch("main.alert"):
                with pytest.raises(SystemExit):
                    main.cmd_daemon(cfg)
            xs.assert_not_called()   # browser/session never constructed
            pick.assert_not_called()  # no scheduler loop, no pick, no post
        finally:
            holder.release()

    def test_once_refused_when_daemon_lock_held(self, tmp_path):
        """`once` is refused before browser/pick/post while daemon holds lock."""
        cfg = _once_cfg(tmp_path)
        holder = PublishLock(lock_path_from_cfg(cfg))
        holder.acquire(command="daemon")
        try:
            session = mock.MagicMock()
            with mock.patch("publisher.x_publisher.XSession", return_value=session) as xs, \
                    mock.patch("main.pick_item") as pick, \
                    mock.patch("main.mark_item_published") as mip, \
                    mock.patch("main.alert"):
                with pytest.raises(SystemExit):
                    main.cmd_once(cfg)
            xs.assert_not_called()           # browser never started
            pick.assert_not_called()          # no candidate selection
            session.post.assert_not_called()  # no X post
            mip.assert_not_called()           # no dedup/finalization
        finally:
            holder.release()

    def test_once_works_when_lock_available(self, tmp_path):
        """Normal `once` flow still publishes; the lock is released afterwards."""
        cfg = _once_cfg(tmp_path)
        db = Database(str(tmp_path / "bot.db"))
        session = mock.MagicMock()
        session.post.return_value = {"ok": True, "reason": "posted"}
        with mock.patch("main._make_db", return_value=db), \
                mock.patch("publisher.x_publisher.XSession", return_value=session), \
                mock.patch("main.pick_item", return_value=_item()), \
                mock.patch("main.alert") as alert:
            main.cmd_once(cfg)
        session.post.assert_called_once()
        alert.assert_not_called()
        assert db.is_source_seen("youtube", "vid-1")

        lock = PublishLock(lock_path_from_cfg(cfg))
        lock.acquire()  # lock was released by cmd_once
        lock.release()

    def test_non_publishing_command_not_blocked(self, tmp_path):
        """A read-only command still operates while the publish lock is held."""
        cfg = {
            "paths": {
                "db_file": str(tmp_path / "bot.db"),
                "ffmpeg": "tools/does-not-exist/ffmpeg.exe",
                "ffprobe": "tools/does-not-exist/ffprobe.exe",
                "brave": "C:/does-not-exist/brave.exe",
                "logs_dir": str(tmp_path / "logs"),
            },
            "secrets": {},
        }
        holder = PublishLock(lock_path_from_cfg(cfg))
        holder.acquire(command="daemon")
        try:
            rc = main.cmd_selftest(cfg)  # never touches the publish lock
        finally:
            holder.release()
        assert rc in (0, 1)  # ran to completion, not blocked