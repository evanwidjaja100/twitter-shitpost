"""State-transition logging regressions for exhausted daemon windows.

All scheduler, browser, database, and clock behavior is faked. These tests
exercise the production transition state and daemon iteration without network
access, a real browser, or real sleeping.
"""

import logging
from datetime import datetime
from unittest import mock

import pytest

import main
from tests.test_daemon_recovery import _daemon_cfg


MESSAGE = "no remaining posting slots in the current window; waiting"
OVERNIGHT_POSTING = {
    "active_hours_start": 16,
    "active_hours_end": 1,
}


def _messages(caplog, level):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "daemon"
        and record.levelno == level
        and record.getMessage() == MESSAGE
    ]


def _iteration(cfg, notice, *, slots, key="W1", attempt=None):
    with mock.patch("retention.maybe_run_retention"), \
            mock.patch("tracker.maybe_check_followers"), \
            mock.patch("scheduler.remaining_slots", return_value=slots), \
            mock.patch("main._logical_posting_window_key", return_value=key), \
            mock.patch("scheduler.sleep_until") as sleep_until, \
            mock.patch(
                "main.attempt_slot",
                return_value=attempt or {"outcome": "posted", "reason": "posted"},
            ) as attempt_slot, \
            mock.patch("main.time.sleep") as poll_sleep:
        main._daemon_iteration(cfg, mock.Mock(), mock.Mock(), notice)
    return sleep_until, attempt_slot, poll_sleep


def test_first_no_slots_observation_logs_info(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="daemon")
    notice = main.NoSlotsNoticeState()

    _iteration(_daemon_cfg(tmp_path), notice, slots=[])

    assert len(_messages(caplog, logging.INFO)) == 1
    assert _messages(caplog, logging.DEBUG) == []


def test_run_daemon_reuses_one_notice_state_across_real_loop_iterations(
    tmp_path, caplog
):
    class LoopEnd(Exception):
        pass

    caplog.set_level(logging.DEBUG, logger="daemon")
    cfg = _daemon_cfg(tmp_path)
    session = mock.Mock()

    with mock.patch("publisher.x_publisher.XSession", return_value=session), \
            mock.patch("main._make_db", return_value=mock.Mock()), \
            mock.patch("retention.maybe_run_retention"), \
            mock.patch("tracker.maybe_check_followers"), \
            mock.patch("scheduler.remaining_slots", side_effect=[[], [], LoopEnd()]), \
            mock.patch("main._logical_posting_window_key", return_value="W1"), \
            mock.patch("main.time.sleep") as poll_sleep:
        with pytest.raises(LoopEnd):
            main._run_daemon(cfg)

    assert len(_messages(caplog, logging.INFO)) == 1
    assert len(_messages(caplog, logging.DEBUG)) == 1
    assert [call.args[0] for call in poll_sleep.call_args_list] == [60, 60]
    session.start.assert_called_once()
    session.stop.assert_called_once()


def test_one_hundred_same_window_polls_log_info_once_and_still_sleep(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG, logger="daemon")
    cfg = _daemon_cfg(tmp_path)
    notice = main.NoSlotsNoticeState()
    sleeps = []

    for _ in range(100):
        *_, poll_sleep = _iteration(cfg, notice, slots=[], key="W1")
        sleeps.extend(call.args[0] for call in poll_sleep.call_args_list)

    assert len(_messages(caplog, logging.INFO)) == 1
    assert len(_messages(caplog, logging.DEBUG)) == 99
    assert sleeps == [60] * 100


def test_available_slot_resets_then_same_window_exhaustion_logs_again(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG, logger="daemon")
    cfg = _daemon_cfg(tmp_path)
    notice = main.NoSlotsNoticeState()

    _iteration(cfg, notice, slots=[], key="W1")
    _iteration(cfg, notice, slots=[], key="W1")
    sleep_until, attempt_slot, poll_sleep = _iteration(
        cfg, notice, slots=[1_700_000_000.0], key="W1"
    )
    _iteration(cfg, notice, slots=[], key="W1")

    assert len(_messages(caplog, logging.INFO)) == 2
    sleep_until.assert_called_once_with(1_700_000_000.0)
    attempt_slot.assert_called_once()
    poll_sleep.assert_called_once_with(60)


def test_new_logical_window_logs_again_without_available_transition(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG, logger="daemon")
    cfg = _daemon_cfg(tmp_path)
    notice = main.NoSlotsNoticeState()

    _iteration(cfg, notice, slots=[], key="W1")
    _iteration(cfg, notice, slots=[], key="W1")
    _iteration(cfg, notice, slots=[], key="W2")

    assert len(_messages(caplog, logging.INFO)) == 2
    assert len(_messages(caplog, logging.DEBUG)) == 1


def test_overnight_midnight_uses_one_existing_logical_window(caplog):
    caplog.set_level(logging.DEBUG, logger="daemon")
    notice = main.NoSlotsNoticeState()
    times = (
        datetime(2026, 12, 10, 23, 50),
        datetime(2026, 12, 11, 0, 10),
        datetime(2026, 12, 11, 0, 50),
    )

    keys = [main._logical_posting_window_key(OVERNIGHT_POSTING, now) for now in times]
    for key in keys:
        notice.observe_no_slots(logging.getLogger("daemon"), key)

    assert keys == ["2026-12-10"] * 3
    assert len(_messages(caplog, logging.INFO)) == 1
    assert len(_messages(caplog, logging.DEBUG)) == 2


def test_next_evening_window_can_emit_new_info(caplog):
    caplog.set_level(logging.DEBUG, logger="daemon")
    notice = main.NoSlotsNoticeState()
    old_key = main._logical_posting_window_key(
        OVERNIGHT_POSTING, datetime(2026, 12, 11, 0, 50)
    )
    new_key = main._logical_posting_window_key(
        OVERNIGHT_POSTING, datetime(2026, 12, 11, 16, 1)
    )

    notice.observe_no_slots(logging.getLogger("daemon"), old_key)
    notice.observe_no_slots(logging.getLogger("daemon"), new_key)

    assert (old_key, new_key) == ("2026-12-10", "2026-12-11")
    assert len(_messages(caplog, logging.INFO)) == 2


def test_fresh_daemon_state_may_log_once_during_exhausted_window(caplog):
    caplog.set_level(logging.DEBUG, logger="daemon")
    logger = logging.getLogger("daemon")

    first_process = main.NoSlotsNoticeState()
    first_process.observe_no_slots(logger, "W1")
    first_process.observe_no_slots(logger, "W1")
    restarted_process = main.NoSlotsNoticeState()
    restarted_process.observe_no_slots(logger, "W1")

    assert len(_messages(caplog, logging.INFO)) == 2
    assert len(_messages(caplog, logging.DEBUG)) == 1


def test_available_slot_still_reaches_post_attempt_after_exhausted_window(tmp_path):
    cfg = _daemon_cfg(tmp_path)
    notice = main.NoSlotsNoticeState()
    _iteration(cfg, notice, slots=[], key="W1")

    sleep_until, attempt_slot, poll_sleep = _iteration(
        cfg,
        notice,
        slots=[1_800_000_000.0],
        key="W2",
        attempt={"outcome": "posted", "reason": "posted"},
    )

    sleep_until.assert_called_once_with(1_800_000_000.0)
    attempt_slot.assert_called_once()
    poll_sleep.assert_called_once_with(60)


def test_no_slots_notice_state_has_no_persistence_surface():
    notice = main.NoSlotsNoticeState()

    assert vars(notice) == {"window_key": None, "no_slots": False}
    assert not hasattr(notice, "db")
    assert not hasattr(notice, "path")
