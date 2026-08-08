"""Regression tests for bounded logging setup (Issue 2 defect B).

Covers `main.setup_logging`: it must install both a rotating bot.log file
handler AND a real sys.stdout console handler, without ever mistaking the file
handler for a console handler (RotatingFileHandler is a StreamHandler subclass),
and repeated calls must not duplicate either handler.

Tests isolate the root logger before every run and restore it afterwards so
they never interfere with pytest/caplog or other suites.
"""

import io
import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

import main


def _cfg(tmp_path, **overrides):
    ret = {
        "log_max_bytes": 4096,
        "log_backup_count": 2,
        "interval_hours": 24,
        "media_days": 7,
        "temp_hours": 24,
        "enabled": True,
    }
    ret.update(overrides)
    return {"paths": {"logs_dir": "logs", "assets_dir": "assets"}, "retention": ret}


@pytest.fixture(autouse=True)
def isolated_root():
    """Snapshot and clear the root logger, restore it after the test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = []
    yield root
    log_dir = None
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            p = Path(getattr(h, "baseFilename", ""))
            if p.name in ("bot.log", "alerts.log"):
                log_dir = p.parent
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    # Close the alerts logger's handler too (it is a separate logger).
    alerts = logging.getLogger("alerts")
    for h in list(alerts.handlers):
        alerts.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.level = saved_level
    root.handlers = saved_handlers


def _file_handlers(handlers):
    return [
        h for h in handlers
        if isinstance(h, logging.FileHandler)
        and Path(getattr(h, "baseFilename", "")).name == "bot.log"
    ]


def _stdout_handlers(handlers):
    return [
        h for h in handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stdout
    ]


class TestSetupLogging:
    def test_a_stdout_handler_installed_with_file_handler(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        main.setup_logging(_cfg(tmp_path))
        root = logging.getLogger()
        assert len(_file_handlers(root.handlers)) == 1
        assert len(_stdout_handlers(root.handlers)) == 1

    def test_b_file_handler_not_mistaken_for_stdout(self, tmp_path, monkeypatch):
        # Pre-seed a RotatingFileHandler; setup must still add a stdout handler.
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(tmp_path / "logs" / "bot.log"), maxBytes=4096, backupCount=2, encoding="utf-8"
        )
        root = logging.getLogger()
        root.addHandler(fh)
        monkeypatch.setattr(main, "BASE", tmp_path)
        main.setup_logging(_cfg(tmp_path))
        root = logging.getLogger()
        assert len(_stdout_handlers(root.handlers)) == 1
        assert len(_file_handlers(root.handlers)) == 1  # pre-seeded recognized too

    def test_c_repeated_setup_no_stdout_dup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        root = logging.getLogger()
        main.setup_logging(_cfg(tmp_path))
        main.setup_logging(_cfg(tmp_path))
        assert len(_stdout_handlers(root.handlers)) == 1

    def test_d_repeated_setup_no_bot_log_dup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        root = logging.getLogger()
        main.setup_logging(_cfg(tmp_path))
        main.setup_logging(_cfg(tmp_path))
        assert len(_file_handlers(root.handlers)) == 1

    def test_e_console_marker_reaches_stdout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        root = logging.getLogger()
        main.setup_logging(_cfg(tmp_path))
        buf = io.StringIO()
        for h in _stdout_handlers(root.handlers):
            h.stream = buf
        root.warning("CONSOLE-MARKER")
        for h in _stdout_handlers(root.handlers):
            h.flush()
        assert "CONSOLE-MARKER" in buf.getvalue()

    def test_f_bot_log_receives_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        root = logging.getLogger()
        main.setup_logging(_cfg(tmp_path))
        root.warning("FILE-MARKER")
        for h in _file_handlers(root.handlers):
            h.flush()
        log_path = tmp_path / "logs" / "bot.log"
        assert log_path.exists()
        assert "FILE-MARKER" in log_path.read_text(encoding="utf-8")

    def test_g_rotation_stays_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        root = logging.getLogger()
        main.setup_logging(_cfg(tmp_path, log_max_bytes=2048, log_backup_count=2))
        for i in range(400):
            root.error("PADDING-" + "z" * 400)
        for h in _file_handlers(root.handlers):
            h.flush()
        produced = list((tmp_path / "logs").glob("bot.log*"))
        assert len(produced) >= 2          # rotation happened
        assert len(produced) <= 3          # backup_count=2 + the current file

    def test_h_alert_log_remains_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        root = logging.getLogger()
        main.setup_logging(_cfg(tmp_path, log_max_bytes=2048, log_backup_count=2))
        main.alert(_cfg(tmp_path), "ALERT-MARKER")
        alerts = logging.getLogger("alerts")
        for h in alerts.handlers:
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.flush()
        am = tmp_path / "logs" / "alerts.log"
        assert am.exists()
        assert "ALERT-MARKER" in am.read_text(encoding="utf-8")
        # alerts.log handler is a RotatingFileHandler (bounded), not plain File.
        assert any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).name == "alerts.log"
            for h in alerts.handlers
        )

    def test_i_foreign_stderr_handler_does_not_suppress_stdout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "BASE", tmp_path)
        root = logging.getLogger()
        foreign = logging.StreamHandler(sys.stderr)
        root.addHandler(foreign)
        main.setup_logging(_cfg(tmp_path))
        root = logging.getLogger()
        assert len(_stdout_handlers(root.handlers)) == 1
        assert main._is_stdout_handler(foreign) is False