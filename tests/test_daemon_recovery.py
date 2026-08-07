"""Regression tests for daemon crash-recovery supervision (Issue 1).

The daemon must not die permanently on an ordinary unexpected exception: the
supervisor restarts the browser session with bounded, growing backoff and
escalates to a non-zero exit after repeated consecutive failures. However an
*intentional* login/captcha shutdown (``stop_on_login_failure``), a
``KeyboardInterrupt`` and a ``SystemExit`` must never be auto-restarted. Both
global ownership locks (publishing + browser profile) stay held across a
recovery backoff window.

No real browser, no real sleeps (all time.sleep calls are mocked), no network,
no posting.
"""

import queue
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

import main
from storage.db import Database

ROOT = str(Path(__file__).resolve().parent.parent)

HOLDER_SCRIPT = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from publishing_lock import PublishLock, BrowserProfileLock
pub = PublishLock(sys.argv[2]); br = BrowserProfileLock(sys.argv[3])
pub.acquire(command="child-daemon"); br.acquire(command="child-daemon")
print("HELD", flush=True)
time.sleep(60)
pub.release(); br.release()
print("DONE", flush=True)
"""

PROBE_SCRIPT = r"""
import sys
sys.path.insert(0, sys.argv[1])
from publishing_lock import PublishLock, BrowserProfileLock
cls = PublishLock if sys.argv[2] == "PublishLock" else BrowserProfileLock
lock = cls(sys.argv[3])
try:
    lock.acquire(command="probe")
except Exception:
    print("DENIED", flush=True)
    sys.exit(0)
print("HELD", flush=True)
lock.release()
sys.exit(1)
"""


class _LoopEnd(BaseException):
    """Test-only control signal: not an Exception, so it is never swallowed by
    the supervisor's recoverable-error or intentional-stop handlers."""


def _spawn_helper(script, *args):
    return subprocess.Popen(
        [sys.executable, "-c", script, ROOT, *map(str, args)],
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


def _daemon_cfg(tmp_path, **safety_overrides):
    cfg = {
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
            "max_daemon_restarts": 2,
        },
    }
    cfg["safety"].update(safety_overrides)
    return cfg


class SessionRecorder:
    """Fake XSession factory that records start/stop event ordering."""

    def __init__(self):
        self.sessions = []
        self.events = []

    def factory(self):
        def _factory(paths):
            tag = f"session{len(self.sessions) + 1}"
            s = mock.MagicMock(name=tag)
            s.start.side_effect = lambda: self.events.append(f"{tag}.start")
            s.stop.side_effect = lambda: self.events.append(f"{tag}.stop")
            s.post.return_value = {"ok": True, "reason": "posted"}
            self.sessions.append(s)
            return s

        return _factory


class TestDaemonRecovery:
    # -- A/B: unexpected exception -> cleanup then retry -------------------

    def test_unexpected_exception_triggers_retry(self, tmp_path):
        """A: exception during work -> session stopped -> backoff -> a fresh
        session is created and started, all inside one daemon process."""
        cfg = _daemon_cfg(tmp_path, max_daemon_restarts=2)
        db = Database(str(cfg["paths"]["db_file"]))
        rec = SessionRecorder()
        count = {"n": 0}
        sleep = mock.MagicMock()

        def iteration_work(c, d, ses):
            count["n"] += 1
            rec.events.append("daemon.work")
            if count["n"] == 1:
                raise RuntimeError("boom during fanout")
            raise _LoopEnd()

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=db), \
                mock.patch("main._daemon_iteration", side_effect=iteration_work), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert"):
            with pytest.raises(_LoopEnd):
                main.supervise_daemon(cfg)

        assert len(rec.sessions) == 2                 # session 2 constructed
        assert rec.sessions[0].start.call_count == 1
        assert rec.sessions[0].stop.call_count == 1   # cleanup before retry
        assert rec.sessions[1].start.call_count == 1  # fresh session started
        assert sleep.call_count == 1                  # one bounded backoff, no hot loop

    def test_session_cleanup_ordering(self, tmp_path):
        """B: exact event ordering start -> work -> exception -> stop -> sleep
        -> second start."""
        cfg = _daemon_cfg(tmp_path, max_daemon_restarts=3)
        db = Database(str(cfg["paths"]["db_file"]))
        rec = SessionRecorder()
        sleep = mock.MagicMock()
        count = {"n": 0}

        def iteration(c, d, ses):
            rec.events.append("daemon.work")
            count["n"] += 1
            if count["n"] == 1:
                raise RuntimeError("boom")
            raise _LoopEnd()

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=db), \
                mock.patch("main._daemon_iteration", side_effect=iteration), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert"):
            with pytest.raises(_LoopEnd):
                main.supervise_daemon(cfg)

        assert rec.events == [
            "session1.start", "daemon.work", "session1.stop",
            "session2.start", "daemon.work", "session2.stop",
        ]
        assert len(sleep.call_args_list) == 1   # backoff between the two sessions

    # -- C: intentional login/captcha stop is NOT retried ------------------

    def test_login_captcha_stop_not_retried(self, tmp_path):
        """C: production control path: captcha + stop_on_login_failure ->
        intentional stop. No second XSession, no recovery backoff, clean exit."""
        cfg = _daemon_cfg(tmp_path)
        rec = SessionRecorder()
        db = Database(str(cfg["paths"]["db_file"]))
        sleep = mock.MagicMock()
        alert = mock.MagicMock()

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=db), \
                mock.patch("tracker.maybe_check_followers"), \
                mock.patch("scheduler.remaining_slots", return_value=[9999999999.0]), \
                mock.patch("scheduler.sleep_until"), \
                mock.patch("main.attempt_slot",
                           return_value={"outcome": "failed", "reason": "captcha"}), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert", alert):
            main.supervise_daemon(cfg)   # returns silently — intentional stop

        assert len(rec.sessions) == 1                       # no second session
        assert rec.sessions[0].start.call_count == 1
        assert rec.sessions[0].stop.call_count == 1         # clean shutdown
        sleep.assert_not_called()                           # no recovery backoff
        alert_calls = [str(c.args[1]) for c in alert.call_args_list]
        assert any("login/captcha" in m for m in alert_calls)

    # -- D/E: KeyboardInterrupt and SystemExit are not swallowed ----------

    def test_keyboard_interrupt_not_swallowed(self, tmp_path):
        cfg = _daemon_cfg(tmp_path)
        rec = SessionRecorder()
        sleep = mock.MagicMock()

        def itr(c, d, s):
            raise KeyboardInterrupt()

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("main._daemon_iteration", side_effect=itr), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert"):
            with pytest.raises(KeyboardInterrupt):
                main.supervise_daemon(cfg)

        assert len(rec.sessions) == 1
        assert rec.sessions[0].stop.call_count == 1  # cleanup happened
        sleep.assert_not_called()                    # no restart

    def test_system_exit_not_swallowed(self, tmp_path):
        cfg = _daemon_cfg(tmp_path)
        rec = SessionRecorder()

        def itr(c, d, s):
            raise SystemExit(3)

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("main._daemon_iteration", side_effect=itr), \
                mock.patch("main.time.sleep"), \
                mock.patch("main.alert"):
            with pytest.raises(SystemExit) as exc:
                main.supervise_daemon(cfg)

        assert exc.value.code == 3
        assert len(rec.sessions) == 1
        assert rec.sessions[0].stop.call_count == 1   # cleanup still ran

    # -- F/G: repeated failures escalate; backoff is bounded --------------

    def test_repeated_failures_escalate(self, tmp_path):
        """F: >max consecutive failures -> supervisor re-raises (non-zero exit)."""
        cfg = _daemon_cfg(tmp_path, max_daemon_restarts=2)
        rec = SessionRecorder()
        sleep = mock.MagicMock()
        alert = mock.MagicMock()

        def always_crash(c, d, s):
            raise RuntimeError("always crashing")

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("main._daemon_iteration", side_effect=always_crash), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert", alert):
            with pytest.raises(RuntimeError) as exc_info:
                main.supervise_daemon(cfg)

        assert "always crashing" in str(exc_info.value)
        assert sleep.call_count == 1                     # one backoff, then escalate
        assert any("gave up" in str(c.args[1]) for c in alert.call_args_list)
        assert len(rec.sessions) == 2

    def test_backoff_is_bounded(self, tmp_path):
        """G: delays grow with each retry but never exceed the cap."""
        cfg = _daemon_cfg(tmp_path, retry_backoff_minutes=1, max_daemon_restarts=6)
        rec = SessionRecorder()
        sleep = mock.Mock()

        def always_crash(c, d, s):
            raise RuntimeError("boom")

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("main._daemon_iteration", side_effect=always_crash), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert"):
            with pytest.raises(RuntimeError):
                main.supervise_daemon(cfg)

        delays = [c.args[0] for c in sleep.call_args_list]  # 5 backoffs before give-up
        assert delays == sorted(delays)
        assert delays[-1] == delays[-1] < 600.0
        assert max(delays) <= 600.0                         # cap never exceeded
        assert all(d >= 0 for d in delays)

    # -- H: successful recovery + failure-counter reset -------------------

    def test_successful_recovery(self, tmp_path):
        """H: crash then one successful normal-work iteration -> daemon keeps
        running on the fresh session (ends only on the test stop signal)."""
        cfg = _daemon_cfg(tmp_path)
        rec = SessionRecorder()
        count = {"n": 0}

        def itr(c, d, s):
            count["n"] += 1
            if count["n"] == 1:
                raise RuntimeError("boom")
            if count["n"] == 2:
                rec.events.append("normal-work")
            raise _LoopEnd()

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("main._daemon_iteration", side_effect=itr), \
                mock.patch("main.time.sleep"), \
                mock.patch("main.alert"):
            with pytest.raises(_LoopEnd):
                main.supervise_daemon(cfg)

        assert len(rec.sessions) == 2
        assert "normal-work" in rec.events
        assert rec.sessions[1].start.call_count == 1

    def test_consecutive_failure_counter_resets_on_success(self, tmp_path):
        """H2: a completed loop iteration clears the counter, so escalation
        requires *consecutive* failures (a success in between re-arms it)."""
        cfg = _daemon_cfg(tmp_path, max_daemon_restarts=2)
        rec = SessionRecorder()
        steps = {"n": 0}
        sleep = mock.Mock()

        def itr(c, d, s):
            steps["n"] += 1
            n = steps["n"]
            if n in (1, 3):
                raise RuntimeError("boom")   # crash, success, crash, end
            if n == 4:
                raise _LoopEnd()
            return

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("main._daemon_iteration", side_effect=itr), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert"):
            # Without the reset, failure #1 and #3 would be consecutive (1, 2)
            # and escalate with an exception. It must run to the stop signal.
            with pytest.raises(_LoopEnd):
                main.supervise_daemon(cfg)

        assert len(rec.sessions) == 3
        assert sleep.call_count == 2   # backoff after the first and third failure

    # -- A1/A2: alert failures never interfere with recovery --------------

    def test_alert_failure_does_not_block_retry(self, tmp_path):
        """A1: primary failure + alert failure -> retry still occurs. The
        supervisor must swallow a secondary alert() exception (disk full,
        unwritable logs dir, ...) and proceed with the bounded backoff, so a
        diagnostic failure can never kill recovery."""
        cfg = _daemon_cfg(tmp_path, max_daemon_restarts=2)
        db = Database(str(cfg["paths"]["db_file"]))
        rec = SessionRecorder()
        count = {"n": 0}
        sleep = mock.MagicMock()

        def iteration_work(c, d, ses):
            count["n"] += 1
            if count["n"] == 1:
                raise RuntimeError("boom during fanout")
            raise _LoopEnd()

        def failing_alert(cfg, message):
            raise OSError("disk full: cannot write alerts.log")

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=db), \
                mock.patch("main._daemon_iteration", side_effect=iteration_work), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert", side_effect=failing_alert):
            with pytest.raises(_LoopEnd):
                main.supervise_daemon(cfg)

        assert len(rec.sessions) == 2                 # retry happened
        assert rec.sessions[1].start.call_count == 1  # fresh session started
        assert sleep.call_count == 1                  # backoff still ran

    def test_alert_failure_does_not_mask_original_exception(self, tmp_path, caplog):
        """A2: retry exhaustion + alert failure -> the *original* daemon
        exception escalates (process exits non-zero), not the alert's OSError,
        and the alert failure is logged as a diagnostic."""
        cfg = _daemon_cfg(tmp_path, max_daemon_restarts=1)
        rec = SessionRecorder()
        sleep = mock.MagicMock()
        original = RuntimeError("primary crash, must surface")

        def always_crash(c, d, s):
            raise original

        def failing_alert(cfg, message):
            raise OSError("cannot write alerts.log")

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("main._daemon_iteration", side_effect=always_crash), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert", side_effect=failing_alert):
            with pytest.raises(RuntimeError) as exc_info:
                main.supervise_daemon(cfg)

        assert exc_info.value is original            # primary exception survives
        assert "primary crash" in str(exc_info.value)
        assert sleep.call_count == 0                 # escalation: no backoff sleep

    # -- A3: the helper succeeds for a recoverable alert failure -----------

    def test_best_effort_alert_logs_failure_but_returns(self, tmp_path, caplog):
        """A3: _best_effort_alert swallows the alert failure (so callers never
        raise) and records a diagnostic warning."""
        failing = mock.MagicMock(side_effect=OSError("disk full"))
        with mock.patch("main.alert", failing):
            ret = main._best_effort_alert(_daemon_cfg(tmp_path), "op warning")
        assert ret is None                            # never raises
        warn = [r for r in caplog.records if "failed to send" in r.getMessage()]
        assert warn and "op warning" in warn[-1].getMessage()
        assert warn[-1].exc_info is not None          # underlying OSError logged

    def test_alert_failure_does_not_affect_intentional_stop(self, tmp_path):
        """the intentional login/captcha stop also uses the helper: an alert
        failure there must not turn a planned clean shutdown into a crash"""
        cfg = _daemon_cfg(tmp_path)
        rec = SessionRecorder()
        sleep = mock.MagicMock()

        def failing_alert(cfg, message):
            raise OSError("cannot write alerts.log")

        with mock.patch("publisher.x_publisher.XSession", new=rec.factory()), \
                mock.patch("main._make_db", return_value=Database(str(cfg["paths"]["db_file"]))), \
                mock.patch("tracker.maybe_check_followers"), \
                mock.patch("scheduler.remaining_slots", return_value=[9999999999.0]), \
                mock.patch("scheduler.sleep_until"), \
                mock.patch("main.attempt_slot",
                           return_value={"outcome": "failed", "reason": "captcha"}), \
                mock.patch("main.time.sleep", sleep), \
                mock.patch("main.alert", side_effect=failing_alert):
            main.supervise_daemon(cfg)   # returns cleanly, no exception

        assert not sleep.called           # no recovery backoff
        assert len(rec.sessions) == 1     # one session, cleanly stopped
        assert rec.sessions[0].stop.call_count == 1

    # -- I: publishing lock stays held during recovery --------------------

    def test_publish_lock_held_across_internal_restart(self, tmp_path):
        """I. while the (simulated) daemon is 'between restarts', another
        publishing process is still denied; once it dies the OS frees the lock."""
        pub_path = str(tmp_path / "pub.lock")
        br_path = str(tmp_path / "br.lock")
        holder = _spawn_helper(HOLDER_SCRIPT, pub_path, br_path)
        try:
            assert _readline(holder) == "HELD"
            denied = _spawn_helper(PROBE_SCRIPT, "PublishLock", pub_path)
            assert _readline(denied) == "DENIED"
            denied.wait(timeout=30)
        finally:
            _kill(holder)
        ok = _spawn_helper(PROBE_SCRIPT, "PublishLock", pub_path)
        assert _readline(ok) == "HELD"
        ok.wait(timeout=30)

    # -- J: browser-profile lock stays held during recovery ----------------

    def test_browser_profile_lock_held_during_internal_restart(self, tmp_path):
        """During internal daemon recovery the browser profile is still owned:
        a browser-only command is denied; after the daemon dies it may enter."""
        holder_path = str(tmp_path / "pub2.lock")
        br_path = str(tmp_path / "br2.lock")
        holder = _spawn_helper(HOLDER_SCRIPT, holder_path, br_path)
        try:
            assert _readline(holder) == "HELD"
            denied = _spawn_helper(PROBE_SCRIPT, "BrowserProfileLock", br_path)
            assert _readline(denied) == "DENIED"
            denied.wait(timeout=30)
        finally:
            _kill(holder)
        ok = _spawn_helper(PROBE_SCRIPT, "BrowserProfileLock", br_path)
        assert _readline(ok) == "HELD"
        ok.wait(timeout=30)