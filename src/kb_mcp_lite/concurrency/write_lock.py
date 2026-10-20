"""Cross-process write lock for kb-mcp SQLite databases.

When two processes (MCP server + watcher, two CLI sessions, …) try to write
to the same SQLite database at the same time they race on the WAL index. The
default mitigation is SQLite's own ``busy_timeout`` retry, which silently
serialises writers and surfaces contention only as latency. This module adds
an *opt-in* strict mode: a process-level file lock that surfaces contention
as an explicit :class:`ResourceBusyError`, with a configurable timeout.

The lock is implemented with platform-native ``flock``-style primitives:

* Linux / macOS: ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a sidecar
  ``<db>.lock`` file.
* Windows: ``msvcrt.locking(LK_NBLCK, 1)`` on the same sidecar file.

The lock is *advisory*: every kb-mcp process that wants mutual exclusion
must acquire it before writing. The base protection (SQLite ``busy_timeout``)
remains in place, so even un-cooperating processes can still serialise at
the SQLite layer.

Stale lock recovery
-------------------
A process crash leaves the sidecar file but releases the kernel lock
automatically. There is no need for a separate "stale lock" detector —
``flock`` semantics handle that case correctly.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from collections.abc import Iterator

from kb_mcp_lite.schema import KbMcpError

logger = logging.getLogger("kb_mcp_lite.concurrency")

# A heart-beat written into the lock file. We do not use this for stale
# detection (the kernel handles that), but it helps users debug which
# process currently holds the lock when contention occurs.
_HOLDER_MAGIC = b"kb-mcp-lock\n"


class ResourceBusyError(KbMcpError):
    """Raised when a write lock cannot be acquired within the timeout.

    Attributes:
        db_path: the database that could not be locked.
        timeout: the configured timeout (seconds).
        holder_pid: PID of the process currently holding the lock, if it
            can be determined from the lock file's heartbeat contents. May
            be ``None`` when the lock is held by an older kb-mcp process
            that did not write a heartbeat.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        timeout: float,
        holder_pid: int | None = None,
    ) -> None:
        self.db_path = os.fspath(db_path)
        self.timeout = timeout
        self.holder_pid = holder_pid
        msg = (
            f"another process is writing to {self.db_path!r}; "
            f"wait up to {timeout:.1f}s or check the holder. "
            f"(pass lock_timeout=0 to fail immediately)"
        )
        if holder_pid is not None:
            msg += f" Holder PID: {holder_pid}."
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Platform-specific low-level acquire / release
# ---------------------------------------------------------------------------

_POSIX = sys.platform != "win32"


def _open_lock_fd(path: Path) -> int:
    """Open (creating if missing) the lock file and return its fd."""
    flags = os.O_CREAT | os.O_RDWR
    # 0o644 is the standard permission for shared lock files. They are
    # user-writable so a different user cannot lock the kb.
    return os.open(str(path), flags, 0o644)


def flock_acquire(fd: int) -> bool:
    """Try to acquire an exclusive, non-blocking lock. Return True on success.

    Returns False (rather than raising) so the caller can implement its own
    retry / timeout loop. Windows ``msvcrt.locking`` is not natively
    non-blocking, so we wrap it in a thread + timeout only if necessary;
    for the kb-mcp case the polling loop in :class:`WriteLock` is enough.
    """
    if _POSIX:
        import fcntl  # local import — only available on POSIX

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            return False
    else:
        # Windows: msvcrt.locking raises OSError("Resource temporarily
        # unavailable") when the region is already locked.
        import msvcrt  # type: ignore[import-not-found,unused-ignore]

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except (OSError, BlockingIOError):
            return False


def flock_release(fd: int) -> None:
    """Release the lock previously taken with :func:`flock_acquire`."""
    if _POSIX:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    else:
        import msvcrt  # type: ignore[import-not-found,unused-ignore]

        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class WriteLock:
    """A context-manager file lock scoped to one kb database file.

    Usage::

        with WriteLock("/path/to/kb.db", timeout=10.0):
            ...  # exclusive write section

    On context exit the lock is released and the file descriptor closed.
    Errors during release are logged and swallowed — the kernel will
    release the lock when the file descriptor is closed anyway.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        timeout: float = 10.0,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout < 0:
            raise ValueError(f"timeout must be >= 0, got {timeout!r}")
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")
        self.timeout = timeout
        self.poll_interval = max(0.01, poll_interval)
        self._fd: int | None = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def _try_acquire(self) -> bool:
        """One-shot acquisition attempt. Internal use only."""
        if self._fd is None:
            self._fd = _open_lock_fd(self.lock_path)
        return flock_acquire(self._fd)

    def _release(self) -> None:
        if self._fd is not None:
            flock_release(self._fd)
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._acquired = False

    def acquire(self) -> None:
        """Block until the lock is acquired or :attr:`timeout` elapses.

        Raises:
            ResourceBusyError: if the lock cannot be acquired in time.
        """
        if self._acquired:
            return

        # Open the fd up-front so the lock file exists even if the very
        # first attempt succeeds; that way external observers can ``stat``
        # the file to see whether the directory holds a kb-mcp lock.
        if self._fd is None:
            self._fd = _open_lock_fd(self.lock_path)

        deadline = None if self.timeout == 0 else time.monotonic() + self.timeout
        attempt = 0
        while True:
            if self._try_acquire():
                # Write a heart-beat line so a contending process can see
                # which PID is holding the lock. Best-effort: failure here
                # is non-fatal (the lock is already ours). ``fsync`` is
                # needed so a contender that opens the file moments later
                # can read the heartbeat (without fsync the data may still
                # be in the writer's page cache).
                try:
                    if self._fd is not None:
                        heartbeat = f"kb-mcp-lock pid={os.getpid()}\n".encode()
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        os.write(self._fd, heartbeat)
                        os.ftruncate(self._fd, len(heartbeat))
                        os.fsync(self._fd)
                except OSError:
                    pass
                self._acquired = True
                logger.debug("Acquired write lock on %s", self.db_path)
                return

            attempt += 1
            if deadline is None:
                # timeout == 0 means "try exactly once and fail".
                self._release()
                raise ResourceBusyError(
                    self.db_path, self.timeout, holder_pid=self._peek_holder_pid()
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._release()
                holder = self._peek_holder_pid()
                raise ResourceBusyError(self.db_path, self.timeout, holder_pid=holder)

            # Poll until either we get the lock or the deadline passes.
            time.sleep(min(self.poll_interval, remaining))

    def _peek_holder_pid(self) -> int | None:
        """Read the holder PID out of the lock file's heartbeat, if present.

        Used purely to enrich the :class:`ResourceBusyError` message. Returns
        ``None`` when the lock file is empty or does not carry a recognisable
        heartbeat.
        """
        try:
            with open(self.lock_path, "rb") as f:
                head = f.read(64).decode("utf-8", errors="ignore").strip()
        except OSError:
            return None
        # Format written above: "kb-mcp-lock pid=<N>"
        if "pid=" not in head:
            return None
        try:
            return int(head.split("pid=", 1)[1].split()[0])
        except (ValueError, IndexError):
            return None

    def release(self) -> None:
        """Release the lock if currently held."""
        if self._acquired:
            self._release()
            logger.debug("Released write lock on %s", self.db_path)

    def __enter__(self) -> WriteLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


@contextmanager
def write_lock(
    db_path: str | os.PathLike[str],
    *,
    timeout: float = 10.0,
) -> Iterator[None]:
    """Functional form of :class:`WriteLock`.

    Example::

        from kb_mcp_lite.concurrency import write_lock
        with write_lock("/path/to/kb.db", timeout=5.0):
            ...  # exclusive write section
    """
    lock = WriteLock(db_path, timeout=timeout)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
