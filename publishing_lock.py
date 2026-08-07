"""Process-level single-instance OS file locks for the bot.

Two distinct resources are guarded, so they get two logical locks backed by the
same small, non-blocking OS file-lock primitive (:class:`ProcessFileLock`):

* **PUBLISHING lock** — at most one publishing process per bot database
  (``daemon`` and ``once``). Guards publication ownership.
* **BROWSER-PROFILE lock** — at most one opener of the configured persistent
  Brave profile (``daemon``, ``once``, ``sources``, online ``stats``,
  ``login``). Guards browser/cookie/profile ownership.

Both are authoritative **OS-held locks**:

* Windows: ``msvcrt.locking(LK_NBLCK)`` on byte 0 of the file
* POSIX:   ``fcntl.flock(LOCK_EX | LOCK_NB)``

The OS automatically releases a lock when its owning process exits or crashes,
so a stale lock file alone is never treated as "running" — file existence,
directory existence, PID text or lock metadata are never ownership. The file
content (pid/start/command) is informational only.

Lock ordering is globally fixed — never acquire in the reverse order:

    publishing lock -> browser-profile lock -> browser startup

so `daemon`/`once` hold the publishing lock first and the browser lock second,
and browser-only commands (sources / stats / login) hold the browser lock only.
No lock is ever held across a long-running SQLite transaction; they are
process-lifetime ownership locks acquired before browser startup and released
in a ``finally`` on every exit path.
"""

import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_FILE_NAME = "publisher.lock"
BROWSER_LOCK_SUFFIX = ".browser.lock"


class LockUnavailable(Exception):
    """The requested OS file lock is held by another process."""


class PublishLockUnavailable(LockUnavailable):
    """Another publishing process already owns the lock."""


class BrowserProfileLockUnavailable(LockUnavailable):
    """Another bot command already opened the configured browser profile."""


class ProcessFileLock:
    """A non-blocking, exclusive OS file lock (cross-platform).

    ``acquire()`` fails fast with :class:`LockUnavailable` (via the class-level
    ``unavailable_error``) if another process holds it; ``release()`` is
    idempotent and always closes the fd, which is what actually frees the lock
    with the OS. The lock file's parent directory is created on demand so fresh
    installs (where ``data/`` or the profile's parent may not exist yet) work
    identically to established ones.
    """

    unavailable_error = LockUnavailable

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def _open_for_lock(self):
        # Parent dir may be absent on a fresh install (the natural DB/profile
        # directory is created later by Browser/Database startup). Idempotent:
        # never an error if it already exists, never deletes/recreates it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # r+b requires an existing file; create it (with a byte of metadata)
        # when it is missing so there is always something to lock on Windows.
        try:
            return open(self.path, "r+b")
        except FileNotFoundError:
            return open(self.path, "w+b")

    def acquire(self, command: str | None = None):
        if self.held:
            return  # same instance already owns it
        fh = self._open_for_lock()
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._close_failed_lock_handle(fh)
            raise self.unavailable_error(self.path) from None
        # Lock owned: metadata (informational only) is written now so a losing
        # process never writes into a region the winner has locked.
        try:
            fh.seek(0)
            meta = (
                f"pid={os.getpid()} "
                f"started={time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"command={command or ''}\n"
            )
            fh.write(meta.encode("utf-8"))
            fh.flush()
        except OSError:
            pass  # metadata is informational; the OS lock is authoritative
        self._fh = fh

    @staticmethod
    def _close_failed_lock_handle(fh):
        """Close a handle whose lock attempt failed.

        On Windows the CRT can make a subsequent close() of that fd fail with
        EACCES after a failed msvcrt.locking(); an explicit unlock attempt
        clears the stale state before closing.
        """
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except OSError:
            pass

    def release(self):
        if not self.held:
            return
        fh, self._fh = self._fh, None
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # release is best-effort; close still frees the OS lock
        finally:
            fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


class PublishLock(ProcessFileLock):
    """Semantic wrapper: the single-instance publishing lock."""

    unavailable_error = PublishLockUnavailable


class BrowserProfileLock(ProcessFileLock):
    """Semantic wrapper: the single-opener persistent-browser lock."""

    unavailable_error = BrowserProfileLockUnavailable


def _repo_base() -> Path:
    return Path(__file__).resolve().parent


def lock_path_from_cfg(cfg: dict) -> Path:
    """Deterministic single-instance PUBLISHING lock path for a bot instance.

    Derived from the configured database location, resolved against the
    repository root (never the shell CWD), so ``daemon``/``once``/tests always
    target the same lock for the same bot regardless of where Python was
    launched. The lock sits next to the bot's SQLite database.
    """
    base = _repo_base()
    db_file = Path(str(cfg["paths"]["db_file"]))
    if not db_file.is_absolute():
        db_file = base / db_file
    return db_file.parent / LOCK_FILE_NAME


def browser_lock_path_from_cfg(cfg: dict) -> Path:
    """Deterministic BROWSER-PROFILE lock path for the configured profile.

    Keyed to the *resolved persistent browser profile* (never merely the
    database), so two bots configured with the same profile (regardless of DB)
    collide here, while different profiles get different lock paths. Relative
    profiles resolve against the same repository/``_base`` used by the real
    browser code (repo root by default), making the result absolute and
    independent of the shell CWD. The lock is a sidecar sibling of the profile
    directory — nothing is written inside Brave's own profile internals.
    """
    paths = cfg.get("paths", {}) or {}
    base = Path(paths.get("_base") or _repo_base())
    profile = paths.get("browser_profile") or "browser_profile"
    p = Path(profile)
    if not p.is_absolute():
        p = base / p
    resolved = p.resolve()
    return resolved.parent / (resolved.name + BROWSER_LOCK_SUFFIX)


def _refuse(lock_log_name: str, command: str, message: str):
    print(message)
    logging.getLogger("lock").error("%s refused: %s", command, message)
    sys.exit(1)


@contextmanager
def publishing_lock(cfg: dict, command: str):
    """Acquire the single-instance publishing lock for `command`.

    On contention the process refuses cleanly and exits non-zero BEFORE any
    browser/XSession/publishing work happens. The lock is released in a
    ``finally`` on every exit path, including exceptions, so a normal command
    exception does not strand the lock.
    """
    lock = PublishLock(lock_path_from_cfg(cfg))
    try:
        lock.acquire(command=command)
    except PublishLockUnavailable:
        _refuse(
            "publish",
            command,
            "Another publishing instance is already running. "
            "Refusing to start a second publisher.",
        )
    try:
        yield lock
    finally:
        lock.release()


@contextmanager
def browser_profile_lock(cfg: dict, command: str):
    """Acquire the browser-profile lock for the configured persistent profile.

    Any command that will open the configured Brave profile (daemon/once/
    sources/online stats/login) must hold this while the browser is open, and
    it must be acquired BEFORE ``XSession()``/``sync_playwright``/browser
    startup. On contention the command refuses quickly (non-zero exit) without
    ever starting Playwright.
    """
    lock = BrowserProfileLock(browser_lock_path_from_cfg(cfg))
    try:
        lock.acquire(command=command)
    except BrowserProfileLockUnavailable:
        _refuse(
            "browser-profile",
            command,
            "The configured Brave profile is already in use by another bot command. "
            "Refusing to open a second persistent browser session.",
        )
    try:
        yield lock
    finally:
        lock.release()