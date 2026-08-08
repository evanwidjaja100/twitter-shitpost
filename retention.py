"""Bounded disk retention for a long-running unattended daemon.

Only bot-owned generated artifacts are ever touched:

* *Durable media* under the configured ``assets_dir`` — downloaded sources,
  prepared images (``<hash>.jpg``), FFmpeg clips (``<stem>_clip.mp4``) and X
  media downloads. Removed once older than ``media_days``. Explicitly owned by
  the bot: the post history that mattered is persisted in SQLite, so the raw
  media is regenerable and safe to reclaim.
* *Stale temporary artifacts* that survived a crash — ``*.part`` files (an
  aborted ``pipeline.media.download`` leaves one behind only if the process
  died mid-transfer) and ``.ytdl_*`` work directories (aborted yt-dlp runs).
  Removed once older than ``temp_hours``. A fresh ``.part``/``.ytdl_*`` is
  preserved: the age check is what protects in-flight work, and the daemon
  never runs cleanup concurrently with a media operation.

Everything else is ignored: config files, the SQLite database, browser
profiles, ``.venv``, scripts, and any file whose suffix is not a known
generated-media suffix. Symlinks under the managed tree are never followed and
never deleted, so a symlink cannot cause removal outside the managed dirs.

Cleanup is maintenance, not publication correctness: per-entry failures are
counted and skipped, races (file gone before unlink) are tolerated, and
``maybe_run_retention`` swallows top-level exceptions so a broken cleanup can
never kill the daemon.
"""

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("retention")

# Known bot-generated media suffixes. Foreign files (e.g. notes.txt) are not
# owned by the bot and are never removed.
MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v",
}
PART_SUFFIX = ".part"
YTDL_TEMP_PREFIX = ".ytdl_"

DEFAULTS = {
    "enabled": True,
    "media_days": 7.0,
    "temp_hours": 24.0,
    "log_max_bytes": 5_242_880,
    "log_backup_count": 5,
    "interval_hours": 24.0,
}


def retention_settings(cfg: dict) -> dict:
    """Effective retention settings, with conservative defaults for configs
    that predate the ``retention`` section."""
    r = cfg.get("retention") or {}
    return {
        "enabled": bool(r.get("enabled", DEFAULTS["enabled"])),
        "media_days": float(r.get("media_days", DEFAULTS["media_days"])),
        "temp_hours": float(r.get("temp_hours", DEFAULTS["temp_hours"])),
        "log_max_bytes": int(r.get("log_max_bytes", DEFAULTS["log_max_bytes"])),
        "log_backup_count": int(r.get("log_backup_count", DEFAULTS["log_backup_count"])),
        "interval_hours": float(r.get("interval_hours", DEFAULTS["interval_hours"])),
    }


def log_settings(cfg: dict) -> tuple[int, int]:
    """Rotating log sizing (``max_bytes``, ``backup_count``) for bot.log and
    alerts.log, taken from the same retention section."""
    s = retention_settings(cfg)
    return s["log_max_bytes"], s["log_backup_count"]


def cleanup_due(last_ts, now_ts, settings: dict) -> bool:
    """Whether a cleanup run is due (`None` last run means due)."""
    if not settings["enabled"]:
        return False
    if last_ts is None:
        return True
    return now_ts - last_ts >= settings["interval_hours"] * 3600.0


def _unlink_file(path: Path):
    Path(path).unlink(missing_ok=True)


def _unlink_tree(path: Path):
    # Python 3.14 removed the follow_symlinks kwarg from rmtree, so delete the
    # tree manually. os.walk never descends into symlinked dirs, and symlinked
    # entries are unlinked (not traversed), so a stale temp dir can never pull
    # in targets outside the managed tree.
    for root, dirs, files in os.walk(path, topdown=False):
        r = Path(root)
        for name in files:
            entry = r / name
            try:
                entry.unlink()
            except OSError:
                pass
        for name in dirs:
            entry = r / name
            try:
                if entry.is_symlink():
                    entry.unlink()
                else:
                    entry.rmdir()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass


def _dir_bytes(path: Path) -> int:
    total = 0
    stack = [Path(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            break
    return total


def _try_remove_file(path: Path, stats: dict) -> bool:
    try:
        _unlink_file(path)
        return True
    except FileNotFoundError:
        return True  # vanished between scan and unlink: normal race, not an error
    except OSError as e:
        stats["errors"] += 1
        log.debug("retention: could not remove file %s: %s", path, e)
        return False


def _try_remove_tree(path: Path, stats: dict) -> bool:
    try:
        _unlink_tree(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        stats["errors"] += 1
        log.debug("retention: could not remove tree %s: %s", path, e)
        return False


def _walk(root: Path, media_cutoff: float, temp_cutoff: float, stats: dict) -> None:
    """Iterate the managed tree without following symlinks."""
    stack = [Path(root)]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError as e:
            stats["errors"] += 1
            log.debug("retention: cannot list %s: %s", cur, e)
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue  # never delete or traverse symlinks
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    name = entry.name
                    if name.startswith(YTDL_TEMP_PREFIX):
                        try:
                            old = entry.stat().st_mtime <= temp_cutoff
                        except OSError:
                            old = False
                        if old:
                            size = _dir_bytes(path)
                            if _try_remove_tree(path, stats):
                                stats["temp_removed"] += 1
                                stats["bytes_freed"] += size
                        # never descend into a (fresh or removed) work dir
                    else:
                        stack.append(path)
                    continue
                # regular file
                try:
                    st = entry.stat()
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    continue
                if entry.name.endswith(PART_SUFFIX):
                    if mtime <= temp_cutoff and _try_remove_file(path, stats):
                        stats["temp_removed"] += 1
                        stats["bytes_freed"] += size
                elif Path(entry.name).suffix.lower() in MEDIA_SUFFIXES:
                    if mtime <= media_cutoff and _try_remove_file(path, stats):
                        stats["media_removed"] += 1
                        stats["bytes_freed"] += size
                # anything else: not owned -> ignored
            except OSError:
                stats["errors"] += 1


def run_retention(base_dir, cfg: dict, now: float | None = None) -> dict:
    """Run one media/temp cleanup pass; returns a summary dict.

    ``base_dir`` is the repository root (config paths are relative to it).
    ``now`` is injectable for deterministic tests. Only raises on a top-level
    config/scanning failure; individual file errors are counted, not raised.
    """
    now_clock = time.time() if now is None else float(now)
    s = retention_settings(cfg)
    stats = {"media_removed": 0, "temp_removed": 0, "bytes_freed": 0, "errors": 0}
    if not s["enabled"]:
        log.info("retention cleanup: skipped (disabled)")
        return stats

    assets = Path(base_dir) / str(cfg["paths"]["assets_dir"])
    if not assets.is_dir():
        log.info("retention cleanup: assets dir not present: %s", assets)
        return stats

    media_cutoff = now_clock - s["media_days"] * 86400.0
    temp_cutoff = now_clock - s["temp_hours"] * 3600.0
    _walk(assets, media_cutoff, temp_cutoff, stats)
    log.info(
        "retention cleanup: media_removed=%d temp_removed=%d bytes_freed=%d errors=%d",
        stats["media_removed"], stats["temp_removed"], stats["bytes_freed"], stats["errors"],
    )
    return stats


def maybe_run_retention(base_dir, cfg: dict, state: dict | None = None,
                        now: float | None = None) -> dict:
    """Run cleanup at most once per configured interval; never raises.

    ``state`` (a dict that stores ``last_cleanup``) is updated on success so
    the daemon can call this every iteration without a second pass within the
    interval.
    """
    now_clock = time.time() if now is None else float(now)
    s = retention_settings(cfg)
    if not s["enabled"]:
        return {"skipped": "disabled"}
    last = state.get("last_cleanup") if isinstance(state, dict) else None
    if not cleanup_due(last, now_clock, s):
        return {"skipped": "not_due"}
    try:
        stats = run_retention(base_dir, cfg, now=now_clock)
        stats["skipped"] = False
        if isinstance(state, dict):
            state["last_cleanup"] = now_clock
        return stats
    except Exception:
        log.warning("retention maintenance failed", exc_info=True)
        return {"skipped": "error"}