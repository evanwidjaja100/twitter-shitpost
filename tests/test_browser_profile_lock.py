"""Regression tests for the browser-profile single-owner lock (Issue 2).

Every path that opens the configured persistent Brave profile — daemon, once,
sources, online stats and login (both via main.py AND via `python login.py`) —
must hold the same browser-profile lock for that profile, keyed to the *profile
path*, not the database. So same profile + different DBs still conflict, while
different profiles never conflict. Acquisition happens BEFORE any Playwright
browser startup.

Cross-process OS file-lock semantics are covered by REAL subprocesses; command
entry paths are driven with mocks. No real browser, no network, no posting.
"""

import queue
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

import login
import main
import publishing_lock as pkl
from publishing_lock import (
    BrowserProfileLock,
    BrowserProfileLockUnavailable,
    browser_lock_path_from_cfg,
    browser_profile_lock,
)

ROOT = str(Path(__file__).resolve().parent.parent)

CHILD_SCRIPT = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from publishing_lock import BrowserProfileLock

lock = BrowserProfileLock(sys.argv[2])
try:
    lock.acquire(command="child")
except Exception:
    print("DENIED", flush=True)
    sys.exit(0)
print("HELD", flush=True)
if sys.argv[3] == "hold":
    time.sleep(60)
else:
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


def _cfg(tmp_path, profile, db="bot.db"):
    return {"paths": {"db_file": str(tmp_path / db), "browser_profile": str(profile)}}


class TestBrowserProfileLock:
    # -- A: first process acquires --

    def test_first_process_acquires(self, tmp_path):
        profile = tmp_path / "profile-a"
        lock = BrowserProfileLock(browser_lock_path_from_cfg(_cfg(tmp_path, profile)))
        lock.acquire(command="test-a")
        try:
            assert lock.held
        finally:
            lock.release()
        assert not lock.held

    # -- B: second process with same profile is denied --

    def test_second_process_denied(self, tmp_path):
        path = str(browser_lock_path_from_cfg(
            _cfg(tmp_path, tmp_path / "profile-b")))
        a = _spawn(path, "hold")
        try:
            assert _readline(a) == "HELD"
            b = _spawn(path, "hold")
            assert _readline(b) == "DENIED"
            b.wait(timeout=30)
        finally:
            _kill(a)

    # -- C: different DBs, same profile collide --

    def test_same_profile_different_dbs_conflict(self, tmp_path):
        profile = tmp_path / "shared-profile"
        cfg1 = _cfg(tmp_path, profile, db="bot-a.db")
        cfg2 = _cfg(tmp_path, profile, db="bot-b.db")
        assert cfg1["paths"]["db_file"] != cfg2["paths"]["db_file"]
        lock1 = browser_lock_path_from_cfg(cfg1)
        lock2 = browser_lock_path_from_cfg(cfg2)
        assert lock1 == lock2                      # keyed to profile, not DB
        a = _spawn(str(lock1), "hold")
        try:
            assert _readline(a) == "HELD"
            b = _spawn(str(lock2), "hold")         # second bot, same profile
            assert _readline(b) == "DENIED"
            b.wait(timeout=30)
        finally:
            _kill(a)

    # -- D: different profiles do not collide --

    def test_different_profiles_no_conflict(self, tmp_path):
        p1 = tmp_path / "profile-one"
        p2 = tmp_path / "profile-two"
        lock1 = browser_lock_path_from_cfg(_cfg(tmp_path, p1))
        lock2 = browser_lock_path_from_cfg(_cfg(tmp_path, p2))
        assert lock1 != lock2
        a = _spawn(str(lock1), "hold")
        b = _spawn(str(lock2), "hold")
        try:
            assert _readline(a) == "HELD"
            assert _readline(b) == "HELD"          # unrelated profiles coexist
        finally:
            _kill(a)
            _kill(b)

    # -- E: crash recovery --

    def test_crash_recovery(self, tmp_path):
        path = str(browser_lock_path_from_cfg(
            _cfg(tmp_path, tmp_path / "profile-e")))
        a = _spawn(path, "hold")
        assert _readline(a) == "HELD"
        a.kill()
        a.wait(timeout=30)

        b = _spawn(path, "release")
        assert _readline(b) == "HELD"              # OS released the lock
        b.wait(timeout=30)
        assert b.returncode == 0

    # -- F: stale lock file harmless --

    def test_stale_lock_file_harmless(self, tmp_path):
        lock_path = browser_lock_path_from_cfg(_cfg(tmp_path, tmp_path / "profile-f"))
        lock_path.write_text("pid=999999 started=1970-01-01 command=stale\n")
        lock = BrowserProfileLock(lock_path)
        lock.acquire()
        assert lock.held
        lock.release()

    # -- G: missing parent directory --

    def test_missing_parent_directory_created(self, tmp_path):
        profile = tmp_path / "deep" / "missing" / "store"
        cfg = _cfg(tmp_path, profile)
        lock_path = browser_lock_path_from_cfg(cfg)
        assert not lock_path.parent.exists()
        lock = BrowserProfileLock(lock_path)
        lock.acquire(command="test-g")
        try:
            assert lock.held
            assert lock_path.parent.exists()
            assert lock_path.exists()
        finally:
            lock.release()

    # -- H: lock path independent of CWD --

    def test_lock_path_independent_of_cwd(self, tmp_path, monkeypatch):
        cfg = {"paths": {"browser_profile": "browser_profile"}}
        p1 = browser_lock_path_from_cfg(cfg)
        assert p1.is_absolute()
        assert p1.name.endswith(".browser.lock")
        monkeypatch.chdir(tmp_path)
        assert browser_lock_path_from_cfg(cfg) == p1


def _browser_cfg(tmp_path, profile):
    return {
        "paths": {
            "db_file": str(tmp_path / "bot.db"),
            "browser_profile": str(profile),
            "brave": "C:/does-not-exist/brave.exe",
        },
    }


class TestBrowserEntryPoints:
    # -- I: daemon (browser lock held) excludes sources before browser startup --

    def test_sources_refused_before_browser_when_profile_held(self, tmp_path):
        cfg = _browser_cfg(tmp_path, tmp_path / "profile-i")
        holder = BrowserProfileLock(browser_lock_path_from_cfg(cfg))
        holder.acquire(command="daemon")
        try:
            with mock.patch("publisher.x_publisher.XSession") as xs, \
                    mock.patch("main.pick_item"):
                with pytest.raises(SystemExit):
                    main.cmd_sources(cfg)
            xs.assert_not_called()          # browser never constructed/started
        finally:
            holder.release()

    def test_sources_works_when_profile_free(self, tmp_path):
        cfg = _browser_cfg(tmp_path, tmp_path / "profile-i2")
        db = main._make_db(cfg)
        session = mock.MagicMock()
        picked = {
            "source": "youtube", "source_id": "v", "source_url": "https://youtu.be/v",
            "title": "t", "score": 1.0, "_caption": "c", "_media_path": "m.mp4",
        }
        with mock.patch("publisher.x_publisher.XSession", return_value=session) as xs, \
                mock.patch("main._make_db", return_value=db), \
                mock.patch("main.pick_item", return_value=picked):
            main.cmd_sources(cfg)
        xs.assert_called_once()             # browser acquired + released
        lock = BrowserProfileLock(browser_lock_path_from_cfg(cfg))
        lock.acquire()                      # released by cmd_sources
        lock.release()

    # -- J: daemon excludes login before persistent context launches --

    def test_login_refused_while_profile_held_before_playwright(self, tmp_path):
        paths = {
            "_base": str(tmp_path),
            "browser_profile": "login-bp",
            "brave": "C:/nope/brave.exe",
            "logs_dir": str(tmp_path / "logs"),
        }
        cfg_for_holder = {"paths": paths}
        holder = BrowserProfileLock(browser_lock_path_from_cfg(cfg_for_holder))
        holder.acquire(command="daemon")
        try:
            with mock.patch("login.load_config_paths", return_value=paths), \
                    mock.patch("login.sync_playwright") as sp:
                with pytest.raises(SystemExit):
                    login.main()
            sp.assert_not_called()          # never reached playwright at all
        finally:
            holder.release()

    def test_login_direct_entry_uses_same_lock_key(self, tmp_path):
        """login.py's authoritative check uses the same key as XSession."""
        paths = {
            "_base": str(tmp_path),
            "browser_profile": "login-direct",
            "brave": "C:/nope/brave.exe",
            "logs_dir": str(tmp_path / "logs"),
        }
        # The lock path computed from the login-provided paths == the one
        # computed from a full bot config with the same resolved profile.
        via_login = browser_lock_path_from_cfg({"paths": paths})
        via_cfg = browser_lock_path_from_cfg(
            {"paths": {"browser_profile": str(tmp_path / "login-direct")}})
        assert via_login == via_cfg

    # -- K: offline / non-browser commands unaffected --

    def test_non_browser_commands_work_while_profile_held(self, tmp_path):
        profile = tmp_path / "profile-k"
        cfg = {
            "paths": {
                "db_file": str(tmp_path / "bot.db"),
                "browser_profile": str(profile),
                "ffmpeg": "tools/does-not-exist/ffmpeg.exe",
                "ffprobe": "tools/does-not-exist/ffprobe.exe",
                "brave": "C:/does-not-exist/brave.exe",
                "assets_dir": "assets",
                "logs_dir": str(tmp_path / "logs"),
            },
            "tracking": {},
            "secrets": {},
        }
        holder = BrowserProfileLock(browser_lock_path_from_cfg(cfg))
        holder.acquire(command="daemon")
        try:
            rc = main.cmd_selftest(cfg)     # never opens the profile
            assert rc in (0, 1)
            # offline stats is database-only: no browser lock needed
            main.cmd_stats(cfg, offline=True)
        finally:
            holder.release()

    # -- L: lock is released after a command exception --

    def test_lock_released_after_exception(self, tmp_path):
        cfg = _browser_cfg(tmp_path, tmp_path / "profile-l")
        with pytest.raises(RuntimeError):
            with browser_profile_lock(cfg, "sources"):
                raise RuntimeError("boom")
        lock = BrowserProfileLock(browser_lock_path_from_cfg(cfg))
        lock.acquire()                      # reusable after unwind
        lock.release()

    # -- lock ORDER: publishing before browser profile --

    def test_once_acquires_publish_before_browser(self, tmp_path):
        order = []
        session = mock.MagicMock()
        session.post.return_value = {"ok": True, "reason": "posted"}
        with mock.patch("publishing_lock.publishing_lock") as pub, \
                mock.patch("publishing_lock.browser_profile_lock") as br, \
                mock.patch("main._make_db",
                           return_value=main._make_db(
                               _browser_cfg(tmp_path, tmp_path / "profile-o"))), \
                mock.patch("publisher.x_publisher.XSession", return_value=session), \
                mock.patch("main.pick_item", return_value=None), \
                mock.patch("main.alert"):
            pub.return_value.__enter__.side_effect = lambda: order.append("publish")
            br.return_value.__enter__.side_effect = lambda: order.append("browser")
            main.cmd_once(_browser_cfg(tmp_path, tmp_path / "profile-o"))
        assert order == ["publish", "browser"]

    def test_daemon_acquires_publish_before_browser(self, tmp_path):
        order = []
        with mock.patch("publishing_lock.publishing_lock") as pub, \
                mock.patch("publishing_lock.browser_profile_lock") as br, \
                mock.patch("main.supervise_daemon"):
            pub.return_value.__enter__.side_effect = lambda: order.append("publish")
            br.return_value.__enter__.side_effect = lambda: order.append("browser")
            main.cmd_daemon(_browser_cfg(tmp_path, tmp_path / "profile-o2"))
        assert order == ["publish", "browser"]