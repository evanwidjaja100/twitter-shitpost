"""Single-instance publishing lock backed by the operating system.

Only one publishing process (`daemon` or `once`) may run for a given bot
database at a time. The lock is an exclusive OS lock on a small sidecar file
next to the SQLite database:

* Windows: ``msvcrt.locking(LK_NBLCK)`` on byte 0 of the file
* POSIX:   ``fcntl.flock(LOCK_EX | LOCK_NB)``

The OS automatically releases the lock when the owning process exits or
crashes, so a stale lock file on disk is harmless — the file's existence must
never be treated as "bot is running". The file content (pid/start/command) is
informational only and is never used as the ownership mechanism.

This lock is a process-lifetime lock: it is acquired once before the publishing
browser/profile starts and held for the whole command. It must never be used
to hold a SQLite transaction open while scraping, sleeping, or posting.
"""

import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_FILE_NAME = "publisher.lock"


class PublishLockUnavailable(Exception):
    """Another publishing process already owns the lock."""


class PublishLock:
    """An exclusive OS file lock (non-blocking, non-reentrant).

    ``acquire()`` fails fast with :class:`PublishLockUnavailable` if another
    process holds it; ``release()`` is idempotent and always closes the handle.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def _open_for_lock(self):
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
            raise PublishLockUnavailable(self.path) from None
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


def lock_path_from_cfg(cfg: dict) -> Path:
    """Deterministic single-instance lock path for a bot instance.

    Derived from the configured database location, resolved against the
    repository root (never the shell CWD), so ``daemon``/``once``/tests always
    target the same lock for the same bot regardless of where Python was
    launched. The lock sits next to the bot's SQLite database.
    """
    base = Path(__file__).resolve().parent
    db_file = Path(str(cfg["paths"]["db_file"]))
    if not db_file.is_absolute():
        db_file = base / db_file
    return db_file.parent / LOCK_FILE_NAME


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
        msg = ("Another publishing instance is already running. "
               "Refusing to start a second publisher.")
        print(msg)
        logging.getLogger("lock").error("%s refused: %s", command, msg)
        sys.exit(1)
    try:
        yield lock
    finally:
        lock.release()