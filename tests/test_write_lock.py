"""Tests for the cross-process write lock (kb-mcp v0.8.0)."""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from kb_mcp_lite.concurrency import (
    ResourceBusyError,
    WriteLock,
    flock_acquire,
    flock_release,
)
from kb_mcp_lite.concurrency.write_lock import write_lock
from kb_mcp_lite.schema import KbMcpError


# ---------------------------------------------------------------------------
# Module-level fixtures and constants
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A sidecar-empty db file under tmp_path."""
    p = tmp_path / "kb.db"
    p.touch()
    return p


# ---------------------------------------------------------------------------
# Unit tests: single-process behaviour
# ---------------------------------------------------------------------------


def test_acquire_release_round_trip(db_path: Path) -> None:
    w = WriteLock(db_path, timeout=2.0)
    assert not w.acquired
    w.acquire()
    assert w.acquired
    w.release()
    assert not w.acquired


def test_context_manager_protocol(db_path: Path) -> None:
    with WriteLock(db_path, timeout=2.0) as w:
        assert w.acquired
        lock_file = db_path.with_suffix(db_path.suffix + ".lock")
        assert lock_file.exists()
    # After __exit__ the lock file may still exist on disk, but the kernel
    # lock is released; a second acquire should succeed immediately.
    with WriteLock(db_path, timeout=2.0) as w2:
        assert w2.acquired


def test_functional_write_lock_context(db_path: Path) -> None:
    with write_lock(db_path, timeout=2.0):
        assert db_path.with_suffix(db_path.suffix + ".lock").exists()


def test_acquire_is_idempotent(db_path: Path) -> None:
    w = WriteLock(db_path, timeout=2.0)
    w.acquire()
    w.acquire()  # second call is a no-op
    assert w.acquired
    w.release()
    assert not w.acquired


def test_invalid_timeout_rejected(db_path: Path) -> None:
    with pytest.raises(ValueError):
        WriteLock(db_path, timeout=-1.0)


def test_heartbeat_contains_current_pid(db_path: Path) -> None:
    w = WriteLock(db_path, timeout=2.0)
    w.acquire()
    try:
        lock_file = db_path.with_suffix(db_path.suffix + ".lock")
        contents = lock_file.read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in contents
    finally:
        w.release()


# ---------------------------------------------------------------------------
# Timeout behaviour
# ---------------------------------------------------------------------------


def test_timeout_zero_succeeds_when_free(db_path: Path) -> None:
    w = WriteLock(db_path, timeout=0.0)
    w.acquire()
    w.release()


def test_timeout_zero_raises_when_busy(db_path: Path) -> None:
    holder = WriteLock(db_path, timeout=10.0)
    holder.acquire()
    try:
        contender = WriteLock(db_path, timeout=0.0)
        with pytest.raises(ResourceBusyError) as exc:
            contender.acquire()
        # holder_pid should be reported
        assert exc.value.holder_pid == os.getpid()
    finally:
        holder.release()


def test_timeout_elapses_then_raises(db_path: Path) -> None:
    holder = WriteLock(db_path, timeout=10.0)
    holder.acquire()
    try:
        t0 = time.monotonic()
        contender = WriteLock(db_path, timeout=0.3, poll_interval=0.05)
        with pytest.raises(ResourceBusyError) as exc:
            contender.acquire()
        elapsed = time.monotonic() - t0
        # Should wait approximately the timeout (allow 2x jitter for slow CI)
        assert 0.2 < elapsed < 1.5, f"unexpected wait: {elapsed:.2f}s"
        assert exc.value.timeout == pytest.approx(0.3, rel=0.5)
    finally:
        holder.release()


# ---------------------------------------------------------------------------
# ResourceBusyError contract
# ---------------------------------------------------------------------------


def test_resource_busy_error_is_kb_mcp_error() -> None:
    err = ResourceBusyError("/tmp/x.db", 5.0, holder_pid=1234)
    assert isinstance(err, KbMcpError)
    assert err.db_path == "/tmp/x.db"
    assert err.timeout == 5.0
    assert err.holder_pid == 1234
    assert "1234" in str(err)


def test_resource_busy_error_without_pid() -> None:
    err = ResourceBusyError("/tmp/x.db", 5.0)
    assert err.holder_pid is None
    assert "Holder PID" not in str(err)


# ---------------------------------------------------------------------------
# SqliteStore integration
# ---------------------------------------------------------------------------


def test_store_exposes_write_lock(tmp_path: Path) -> None:
    from kb_mcp_lite.store.sqlite import SqliteStore

    db = tmp_path / "store.db"
    store = SqliteStore(db, strict_lock=False, lock_timeout=1.0)
    try:
        wl = store.write_lock
        assert isinstance(wl, WriteLock)
        # Use it: acquire and release
        with wl:
            assert wl.acquired
        assert not wl.acquired
    finally:
        store.close()


def test_store_init_accepts_strict_lock_and_timeout(tmp_path: Path) -> None:
    from kb_mcp_lite.store.sqlite import SqliteStore

    db = tmp_path / "store.db"
    store = SqliteStore(db, strict_lock=True, lock_timeout=2.5)
    try:
        assert store._strict_lock is True
        assert store._lock_timeout == 2.5
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Cross-process mutual exclusion
# ---------------------------------------------------------------------------


def _lock_worker(
    db_path: str,
    hold_for: float,
    ready_evt: mp.Event,
    done_evt: mp.Queue,
) -> None:
    """Process entry point. Reports 'ok' on acquire, 'busy' on timeout."""
    from kb_mcp_lite.concurrency import ResourceBusyError, WriteLock

    w = WriteLock(db_path, timeout=0.5)
    try:
        w.acquire()
    except ResourceBusyError as e:
        ready_evt.set()
        done_evt.put(("busy", e.holder_pid))
        return
    ready_evt.set()
    time.sleep(hold_for)
    w.release()
    done_evt.put(("ok", os.getpid()))


def test_cross_process_mutual_exclusion(tmp_path: Path) -> None:
    """Process B cannot steal Process A's lock; holder_pid is reported."""
    db = tmp_path / "multi.db"
    db.touch()

    ready_a = mp.Event()
    done_a: mp.Queue = mp.Queue()
    pa = mp.Process(target=_lock_worker, args=(str(db), 1.0, ready_a, done_a), daemon=True)
    pa.start()
    assert ready_a.wait(timeout=3.0), "process A did not start"

    ready_b = mp.Event()
    done_b: mp.Queue = mp.Queue()
    pb = mp.Process(target=_lock_worker, args=(str(db), 0.0, ready_b, done_b), daemon=True)
    pb.start()
    assert ready_b.wait(timeout=3.0), "process B did not start"

    pa.join(timeout=5)
    pb.join(timeout=5)

    result_a = done_a.get(timeout=2)
    result_b = done_b.get(timeout=2)
    assert result_a[0] == "ok"
    assert result_b[0] == "busy"
    assert result_b[1] == result_a[1], (
        f"B should report A's pid as holder: got {result_b[1]} vs A's {result_a[1]}"
    )


# ---------------------------------------------------------------------------
# Low-level flock_* helpers
# ---------------------------------------------------------------------------


def test_flock_helpers_round_trip(db_path: Path) -> None:
    """flock_acquire / flock_release on a real fd succeed."""
    lock_file = db_path.with_suffix(db_path.suffix + ".lock")
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        assert flock_acquire(fd) is True
        # A second non-blocking attempt on the same fd should fail because
        # flock locks are per-OFD (open file description) and this is a
        # different fd pointing to the same file.
        fd2 = os.open(str(lock_file), os.O_RDWR, 0o644)
        try:
            assert flock_acquire(fd2) is False
        finally:
            os.close(fd2)
        # Release the original.
        flock_release(fd)
        # And now a fresh fd can acquire.
        fd3 = os.open(str(lock_file), os.O_RDWR, 0o644)
        try:
            assert flock_acquire(fd3) is True
            flock_release(fd3)
        finally:
            os.close(fd3)
    finally:
        os.close(fd)
