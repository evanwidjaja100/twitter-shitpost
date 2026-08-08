"""Deterministic regression tests for bounded disk retention (Issue 1).

Only temporary directories are used; no real user data, real logs or the real
Task Scheduler are ever touched. A fixed ``now`` and explicit mtimes make every
cutoff decision deterministic regardless of wall clock.
"""

import os
from pathlib import Path

import pytest

import retention


NOW = 1_800_000_000.0  # constant; timezone-independent
DAY = 86_400.0
HOUR = 3_600.0


def _cfg(tmp_path, **overrides):
    cfg = {
        "paths": {"assets_dir": "assets", "logs_dir": "logs"},
        "retention": {
            "enabled": True,
            "media_days": 7,
            "temp_hours": 24,
            "log_max_bytes": 4096,
            "log_backup_count": 2,
            "interval_hours": 24,
        },
    }
    cfg["retention"].update(overrides)
    return cfg


def _assets(tmp_path) -> Path:
    a = tmp_path / "assets"
    a.mkdir(parents=True, exist_ok=True)
    return a


def _mk(root: Path, rel: str, size: int = 10, age_s: float = 1_000_000):
    """Create a file under `root` with a synthetic mtime `NOW - age_s`."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    mtime = NOW - age_s
    os.utime(p, (mtime, mtime))
    return p


def _run(tmp_path, **cfg_over):
    return retention.run_retention(tmp_path, _cfg(tmp_path, **cfg_over), now=NOW)


class TestMediaRetention:
    def test_a_old_media_removed(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/old.mp4", size=50, age_s=8 * DAY)
        _mk(a, "x/old.jpg", size=40, age_s=30 * DAY)
        s = _run(tmp_path)
        assert not (a / "youtube/old.mp4").exists()
        assert not (a / "x/old.jpg").exists()
        assert s["media_removed"] == 2

    def test_b_recent_media_preserved(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/recent.mp4", age_s=30 * 60)
        _mk(a, "x/recent.jpg", age_s=2 * DAY)
        s = _run(tmp_path)
        assert (a / "youtube/recent.mp4").exists()
        assert (a / "x/recent.jpg").exists()
        assert s["media_removed"] == 0

    def test_c_exact_cutoff_consistent(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "one_sec_newer.jpg", age_s=7 * DAY - 1)  # mtime > cutoff => kept
        _mk(a, "at_cutoff.jpg", age_s=7 * DAY)          # mtime == cutoff => removed
        s = _run(tmp_path)
        assert (a / "one_sec_newer.jpg").exists()
        assert not (a / "at_cutoff.jpg").exists()
        assert s["media_removed"] == 1

    def test_d_unrelated_file_preserved(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/notes.txt", age_s=90 * DAY)
        _mk(a, "noprefix-notes.md", age_s=90 * DAY)
        s = _run(tmp_path)
        assert (a / "youtube/notes.txt").exists()
        assert (a / "noprefix-notes.md").exists()
        assert s["media_removed"] == 0

    def test_e_fresh_active_file_preserved(self, tmp_path):
        a = _assets(tmp_path)
        # A file created now (mid-operation) is active and must survive.
        _mk(a, "youtube/being_prepared.mp4", age_s=0)
        s = _run(tmp_path)
        assert (a / "youtube/being_prepared.mp4").exists()
        assert s["media_removed"] == 0

    def test_database_and_config_survive(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/old.mp4", size=10, age_s=60 * DAY)
        data = tmp_path / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "bot.db").write_bytes(b"sqlite")
        (tmp_path / "config.json").write_text("{}")
        (tmp_path / "browser_profile").mkdir(exist_ok=True)
        (tmp_path / "browser_profile" / "Default").write_bytes(b"profile")
        s = _run(tmp_path)
        assert not (a / "youtube/old.mp4").exists()
        assert (data / "bot.db").exists()
        assert (tmp_path / "config.json").exists()
        assert (tmp_path / "browser_profile" / "Default").read_bytes() == b"profile"


class TestTempRetention:
    def test_f_stale_part_removed(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/old.mp4.part", size=9, age_s=3 * DAY)
        s = _run(tmp_path)
        assert not (a / "youtube/old.mp4.part").exists()
        assert s["temp_removed"] == 1

    def test_g_fresh_part_preserved(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/active.mp4.part", size=9, age_s=30)
        s = _run(tmp_path)
        assert (a / "youtube/active.mp4.part").exists()
        assert s["temp_removed"] == 0

    def test_h_stale_ytdl_dir_removed_safely(self, tmp_path):
        a = _assets(tmp_path)
        work = a / "youtube/.ytdl_abc123"
        work.mkdir(parents=True)
        (work / "clip.mp4").write_bytes(b"x" * 100)
        old = NOW - 3 * DAY
        os.utime(work, (old, old))
        s = _run(tmp_path)
        assert not work.exists()
        assert s["temp_removed"] == 1

    def test_i_fresh_ytdl_dir_preserved(self, tmp_path):
        a = _assets(tmp_path)
        work = a / "youtube/.ytdl_abc123"
        work.mkdir(parents=True)
        (work / "clip.mp4").write_bytes(b"x" * 100)
        fresh = NOW - 30
        os.utime(work, (fresh, fresh))
        s = _run(tmp_path)
        assert work.exists()
        assert (work / "clip.mp4").exists()
        assert s["temp_removed"] == 0


class TestErrorTolerance:
    def test_j_one_file_error_does_not_abort_cleanup(self, tmp_path, monkeypatch):
        a = _assets(tmp_path)
        _mk(a, "youtube/evil.jpg", age_s=8 * DAY)
        _mk(a, "youtube/fine.jpg", age_s=8 * DAY)
        real = retention._unlink_file

        def flaky(path: Path):
            if Path(path).name == "evil.jpg":
                raise PermissionError("locked")
            return real(path)

        monkeypatch.setattr(retention, "_unlink_file", flaky)
        s = _run(tmp_path)
        assert s["errors"] == 1
        assert not (a / "youtube/fine.jpg").exists()  # continued past the error
        assert (a / "youtube/evil.jpg").exists()       # the locked file remains

    def test_j_rmtree_error_does_not_abort_cleanup(self, tmp_path, monkeypatch):
        a = _assets(tmp_path)
        work = a / "youtube/.ytdl_bad"
        work.mkdir(parents=True)
        (work / "a.mp4").write_bytes(b"x")
        os.utime(work, (NOW - 3 * DAY, NOW - 3 * DAY))
        _mk(a, "youtube/fine.jpg", age_s=8 * DAY)
        monkeypatch.setattr(
            retention, "_unlink_tree",
            lambda p: (_ for _ in ()).throw(PermissionError("locked")),
        )
        s = _run(tmp_path)
        assert s["errors"] == 1
        assert not (a / "youtube/fine.jpg").exists()

    def test_j_file_disappeared_between_scan_and_unlink_is_not_an_error(
        self, tmp_path, monkeypatch
    ):
        a = _assets(tmp_path)
        _mk(a, "youtube/gone.jpg", age_s=8 * DAY)

        def vanished_unlink(path):
            # Simulate the file being deleted by another process between the
            # scan (mtime seen) and the actual unlink.
            raise FileNotFoundError("vanished")

        monkeypatch.setattr(retention, "_unlink_file", vanished_unlink)
        s = _run(tmp_path)
        assert s["errors"] == 0
        assert s["media_removed"] == 1  # counted as removed, not as an error


class TestSummariesAndThrottle:
    def test_k_bytes_freed_matches_removed_sizes(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/old.mp4", size=100, age_s=10 * DAY)
        _mk(a, "x/old.jpg", size=50, age_s=9 * DAY)
        _mk(a, "youtube/stale.part", size=30, age_s=2 * DAY)
        work = a / "youtube/.ytdl_dir"
        work.mkdir(parents=True)
        (work / "a.mp4").write_bytes(b"y" * 20)
        os.utime(work, (NOW - 2 * DAY, NOW - 2 * DAY))
        s = _run(tmp_path)
        assert s["media_removed"] == 2
        assert s["temp_removed"] == 2
        assert s["bytes_freed"] == 100 + 50 + 30 + 20

    def test_l_symlink_targets_untouched(self, tmp_path):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks unsupported on this platform")
        outside = tmp_path / "outside"
        outside.mkdir(exist_ok=True)
        target = outside / "keep.txt"
        target.write_bytes(b"precious")
        a = _assets(tmp_path)
        try:
            (a / "link.jpg").symlink_to(target)
        except OSError:
            pytest.skip("cannot create symlinks on this host")
        try:
            (a / "dirlink").symlink_to(outside, target_is_directory=True)
        except OSError:
            pass
        s = _run(tmp_path)
        assert target.read_bytes() == b"precious"
        assert (a / "link.jpg").exists()          # symlink itself untouched
        if (a / "dirlink").exists():
            assert (outside / "keep.txt").read_bytes() == b"precious"

    def test_throttle_interval(self, tmp_path):
        cfg = _cfg(tmp_path)
        st = retention.retention_settings(cfg)
        assert retention.cleanup_due(None, NOW, st) is True
        assert retention.cleanup_due(NOW - HOUR, NOW, st) is False
        assert retention.cleanup_due(NOW - 25 * HOUR, NOW, st) is True

    def test_maybe_run_throttled(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/old.mp4", age_s=8 * DAY)
        state = {}
        first = retention.maybe_run_retention(tmp_path, _cfg(tmp_path), state, now=NOW)
        assert first["skipped"] is False
        assert first["media_removed"] == 1
        second = retention.maybe_run_retention(tmp_path, _cfg(tmp_path), state, now=NOW + HOUR)
        assert second["skipped"] == "not_due"
        third = retention.maybe_run_retention(tmp_path, _cfg(tmp_path), state, now=NOW + 25 * HOUR)
        assert third["skipped"] is False
        assert third["media_removed"] == 0

    def test_disabled_skips_cleanup(self, tmp_path):
        a = _assets(tmp_path)
        _mk(a, "youtube/old.mp4", age_s=60 * DAY)
        s = _run(tmp_path, enabled=False)
        assert (a / "youtube/old.mp4").exists()
        assert s == {"media_removed": 0, "temp_removed": 0, "bytes_freed": 0, "errors": 0}

    def test_top_level_failure_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            retention, "run_retention",
            lambda *a, **k: (_ for _ in ()).throw(OSError("fs gone")),
        )
        res = retention.maybe_run_retention(tmp_path, _cfg(tmp_path), {}, now=NOW)
        assert res["skipped"] == "error"


class TestTreeDeletionReporting:
    """Reported stats must match actual filesystem outcomes for .ytdl_* dirs.

    A partial/failed tree deletion must NOT be reported as temp_removed with
    the full dir size claimed freed. Previously _unlink_tree swallowed child
    unlink/rmdir errors and returned normally, so the caller incrmented
    temp_removed/bytes_freed even though the directory still existed.
    """

    def _stale_ytdl(self, root: Path, name: str, child_size: int = 10):
        work = (root / "assets" / name)
        work.mkdir(parents=True)
        child = work / "clip.mp4"
        child.write_bytes(b"y" * child_size)
        os.utime(work, (NOW - 3 * DAY, NOW - 3 * DAY))
        return work, child

    def test_a_child_permission_error_reports_failure(self, tmp_path, monkeypatch):
        a = _assets(tmp_path)
        work, child = self._stale_ytdl(tmp_path, ".ytdl_bad")
        size = child.stat().st_size

        real_unlink = Path.unlink
        def locked_child(self, *args, **kwargs):
            if self.name == "clip.mp4":
                raise PermissionError("locked child")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked_child)
        s = _run(tmp_path)

        assert work.exists() and child.exists()          # actually retained
        assert s["temp_removed"] == 0                    # not counted as removed
        assert s["bytes_freed"] == 0                     # full dir not claimed
        assert s["errors"] >= 1                          # failure surfaced

    def test_b_cleanup_continues_after_one_failure(self, tmp_path, monkeypatch):
        a = _assets(tmp_path)
        bad, bad_child = self._stale_ytdl(tmp_path, ".ytdl_bad")
        good, good_child = self._stale_ytdl(tmp_path, ".ytdl_good")
        good_size = good_child.stat().st_size

        real_unlink = Path.unlink
        def locked_bad(self, *args, **kwargs):
            if self.name.startswith("clip.mp4") and ".ytdl_bad" in str(self):
                raise PermissionError("locked child")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked_bad)
        s = _run(tmp_path)

        assert bad.exists() and bad_child.is_file()      # bad retained
        assert not good.exists()                         # good removed
        assert s["errors"] >= 1                          # one failure
        assert s["temp_removed"] == 1                    # only good counted
        assert s["bytes_freed"] == good_size             # only good bytes

    def test_c_successful_dir_removal_reports_truthfully(self, tmp_path):
        a = _assets(tmp_path)
        work, child = self._stale_ytdl(tmp_path, ".ytdl_ok")
        size = child.stat().st_size
        s = _run(tmp_path)
        assert not work.exists()
        assert s["temp_removed"] == 1
        assert s["bytes_freed"] == size
        assert s["errors"] == 0

    def test_d_disappearing_child_race_is_harmless(self, tmp_path, monkeypatch):
        a = _assets(tmp_path)
        work, child = self._stale_ytdl(tmp_path, ".ytdl_race")

        real_unlink = Path.unlink
        def vanished(self, *args, **kwargs):
            if self.name == "clip.mp4":
                real_unlink(self, *args, **kwargs)   # another process wins first
                raise FileNotFoundError("already gone")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", vanished)
        s = _run(tmp_path)

        assert not work.exists()                       # whole target ultimately gone
        assert s["temp_removed"] == 1
        assert s["errors"] == 0                        # race is not an error

    def test_e_root_rmdir_failure_reports_failure(self, tmp_path, monkeypatch):
        a = _assets(tmp_path)
        work, child = self._stale_ytdl(tmp_path, ".ytdl_rmdir")
        # child unlinks fine; only the final path.rmdir() is blocked.
        real_rmdir = Path.rmdir
        def blocked_rmdir(self, *args, **kwargs):
            if self.name == ".ytdl_rmdir":
                raise PermissionError("locked root dir")
            return real_rmdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "rmdir", blocked_rmdir)
        s = _run(tmp_path)

        assert work.exists()
        assert not child.exists()                      # children were removed
        assert s["temp_removed"] == 0                # dir not fully removed
        assert s["bytes_freed"] == 0
        assert s["errors"] >= 1

    def test_f_symlink_target_untouched_by_tree_removal(self, tmp_path, monkeypatch):
        if not hasattr(os, "symlink"):
            pytest.skip("symlinks unsupported on this platform")
        a = _assets(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir(exist_ok=True)
        target = outside / "keep.txt"
        target.write_bytes(b"precious")
        try:
            (a / "link.jpg").symlink_to(target)
        except OSError:
            pytest.skip("cannot create symlinks on this host")
        s = _run(tmp_path)
        assert target.read_bytes() == b"precious"   # external target untouched
        assert (a / "link.jpg").exists()            # symlink itself untouched

    def test_g_successful_dir_removal_returns_true_helper_contract(self, tmp_path):
        work, child = self._stale_ytdl(tmp_path, ".ytdl_contract")
        stats = {"media_removed": 0, "temp_removed": 0, "bytes_freed": 0, "errors": 0}
        assert retention._try_remove_tree(work, stats) is True
        assert not work.exists()
        assert stats["errors"] == 0

    def test_g_failed_dir_removal_returns_false_helper_contract(self, tmp_path, monkeypatch):
        work, child = self._stale_ytdl(tmp_path, ".ytdl_contract_fail")
        real_unlink = Path.unlink

        def locked_child(self, *args, **kwargs):
            if self.name == "clip.mp4":
                raise PermissionError("locked child")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked_child)
        stats = {"media_removed": 0, "temp_removed": 0, "bytes_freed": 0, "errors": 0}
        assert retention._try_remove_tree(work, stats) is False
        assert work.exists()
        assert stats["errors"] == 1