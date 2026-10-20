"""Async embedding queue for kb-mcp.

This module implements the queue half of the v0.8.0 async-embedding feature
(特性 #1 异步化嵌入). It owns the SQL rows in the ``embedding_queue`` table
created by migration 0007 and exposes a small, race-free API used by:

* :class:`SqliteStore` — to enqueue when documents are added/updated.
* :class:`EmbeddingWorker` (in ``kb_mcp_lite.worker``) — to claim work,
  record outcomes, and inspect the queue.
* The CLI / MCP embedding admin commands — to read status and retry.

State machine
-------------
::

    pending  ──(claim)──>  in_progress
    in_progress  ──(ok)──>  done
    in_progress  ──(transient)──>  pending  (attempts < max_attempts)
    in_progress  ──(permanent)──>  failed  (attempts >= max_attempts)

``done`` and ``failed`` are terminal: a row in either state is invisible
to :meth:`claim_next` until a human (or :meth:`retry`) flips it back to
``pending``. The transitions are enforced both here and by the ``CHECK``
constraint in the SQL schema.

Backoff schedule
----------------
The exponential backoff lives in :data:`BACKOFF_SECONDS`. The worker
reads :attr:`EmbeddingQueueEntry.attempts` and computes the earliest
eligible retry time as ``last_attempt_at + backoff[attempts - 1]`` (or
``now`` if attempts == 0). Because we store ``last_attempt_at`` and
``attempts`` together, the schedule survives a process restart — the
queue is the source of truth for retry timing, not in-memory timers.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from collections.abc import Iterable

logger = logging.getLogger("kb_mcp_lite.store.embedding_queue")


# Five-step exponential backoff. ``BACKOFF_SECONDS[i]`` is the wait time
# after the ``(i+1)``-th failed attempt. After the fifth failure the row
# is moved to ``failed`` (terminal) regardless of the schedule below.
BACKOFF_SECONDS: tuple[int, ...] = (1, 5, 30, 300, 1800)  # 1s, 5s, 30s, 5m, 30m

#: Default ceiling on per-doc retry attempts. A row that fails this many
#: times is parked in the ``failed`` state and surfaces in
#: :meth:`EmbeddingQueue.status` until a human runs ``kb embed retry``.
DEFAULT_MAX_ATTEMPTS: int = 5

#: Number of pending rows :meth:`EmbeddingQueue.claim_next` fetches
#: per call. Bounded so one row that is deep in its backoff window
#: cannot starve the queue: we skip it and try the next candidate
#: instead of returning ``None``. 32 covers a realistic "many stale
#: rows at the head, a fresh job behind" burst without scanning the
#: whole table.
_CLAIM_CANDIDATES: int = 32


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingQueueEntry:
    """One row of the ``embedding_queue`` table.

    Attributes:
        doc_id: Primary key. Mirrors ``documents.id``.
        enqueued_at: ISO-8601 timestamp; used for FIFO ordering of
            ``pending`` rows.
        attempts: Number of failed attempts so far.
        max_attempts: Upper bound; defaults to :data:`DEFAULT_MAX_ATTEMPTS`.
        last_error: Best-effort error message from the most recent failure,
            or ``None`` if the row has never been tried.
        last_attempt_at: ISO-8601 timestamp of the last attempt, or
            ``None`` if the row has never been tried.
        state: One of ``pending``, ``in_progress``, ``done``, ``failed``.
    """

    doc_id: str
    enqueued_at: str
    attempts: int
    max_attempts: int
    last_error: str | None
    last_attempt_at: str | None
    state: str

    @property
    def is_terminal(self) -> bool:
        """A row is terminal if it is in ``done`` or ``failed``."""
        return self.state in ("done", "failed")

    @property
    def is_ready(self) -> bool:
        """``True`` if the next retry is eligible (i.e. enough time passed).

        For ``pending`` rows this is just ``True`` — the row is freshly
        enqueued. For ``in_progress`` rows we never claim them here
        (claim_next does that), so this is effectively a "should the
        worker look at this row?" check.
        """
        if self.state == "pending":
            return True
        if self.state in ("done", "failed", "in_progress"):
            return False
        return False

    def backoff_for(self) -> int:
        """Return the seconds to wait before the next attempt.

        Returns 0 for a row that has not been tried yet.
        """
        if self.attempts <= 0:
            return 0
        idx = min(self.attempts - 1, len(BACKOFF_SECONDS) - 1)
        return BACKOFF_SECONDS[idx]

    def is_backoff_elapsed(self, now_epoch: float | None = None) -> bool:
        """``True`` if at least :meth:`backoff_for` seconds have passed
        since :attr:`last_attempt_at`.

        ``now_epoch`` defaults to :func:`time.time`; tests inject a
        fixed clock here.
        """
        if self.last_attempt_at is None:
            return True
        try:
            last = datetime.fromisoformat(self.last_attempt_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if now_epoch is None:
            now_epoch = time.time()
        return last.timestamp() + self.backoff_for() <= now_epoch


# ---------------------------------------------------------------------------
# Queue class
# ---------------------------------------------------------------------------


class EmbeddingQueue:
    """Thin layer over the ``embedding_queue`` SQL table.

    The class is intentionally stateless apart from the connection
    reference — the database is the source of truth for the queue. This
    keeps it safe to construct multiple ``EmbeddingQueue`` instances
    against the same database (the worker and the store do exactly that).

    All mutating methods are best-effort and never raise for application
    reasons: a missing document is a no-op (the row is auto-cleaned by
    the ``ON DELETE CASCADE``), and a duplicate enqueue updates the
    existing row instead of erroring.
    """

    VALID_STATES = ("pending", "in_progress", "done", "failed")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- enqueue / claim ------------------------------------------------

    def enqueue(self, doc_id: str, *, now: str | None = None) -> None:
        """Add ``doc_id`` to the queue, or reset it to ``pending`` if it
        is already there.

        This is idempotent: calling it twice with the same ``doc_id``
        resets ``attempts`` to 0 and flips the state back to
        ``pending``. That matches the user mental model — re-adding a
        document means "I want this re-embedded from scratch", not
        "preserve the old failure history".

        The row's ``max_attempts`` is preserved across re-enqueues (we
        only update the columns the user-visible semantics care about).

        Note: although the queue connection is opened with
        ``isolation_level=""`` (autocommit-style), pysqlite3 (the
        project's preferred sqlite3 backend) does NOT auto-commit in
        that mode — a write leaves the connection in a
        ``in_transaction == True`` state that blocks other
        connections (notably the background worker) from writing to
        the same row. We therefore call ``commit()`` explicitly after
        every write; on stdlib sqlite3 the same call is a true no-op.
        """
        if now is None:
            now = _now_iso()
        # See docstring: pysqlite3 + ``isolation_level=""`` does not
        # auto-commit, so we have to close the implicit transaction
        # ourselves. Stdlib treats ``commit()`` as a no-op here, so
        # the cost is one extra C call.
        self._conn.execute(
            """
            INSERT INTO embedding_queue (doc_id, enqueued_at, state)
            VALUES (?, ?, 'pending')
            ON CONFLICT(doc_id) DO UPDATE SET
                enqueued_at = excluded.enqueued_at,
                attempts = 0,
                last_error = NULL,
                last_attempt_at = NULL,
                state = 'pending'
            """,
            (doc_id, now),
        )
        self._conn.commit()

    def claim_next(self) -> EmbeddingQueueEntry | None:
        """Atomically pick the next ready ``pending`` row and flip it to
        ``in_progress``.

        "Ready" means ``pending`` AND (it has never been tried OR at
        least the smallest backoff (1s) has elapsed since the last
        failed attempt). Per-attempt backoff windows (5s, 30s, 5m, 30m)
        are evaluated in Python via
        :meth:`EmbeddingQueueEntry.is_backoff_elapsed` because SQLite
        has no portable way to map ``attempts -> seconds`` inside a
        WHERE clause.

        To prevent one row that is deep in its backoff window from
        blocking every row behind it (FIFO starvation), we fetch a
        small batch of eligible candidates and return the first one
        that passes the Python backoff check. ``BACKOFF_CANDIDATES``
        bounds the batch so a single ``claim_next`` call cannot scan
        the whole queue.

        Returns the claimed entry, or ``None`` if the queue has no work
        to do right now.
        """
        now = _now_iso()
        rows = self._conn.execute(
            """
            SELECT doc_id, enqueued_at, attempts, max_attempts,
                   last_error, last_attempt_at, state
            FROM embedding_queue
            WHERE state = 'pending'
              AND (
                last_attempt_at IS NULL
                OR (julianday(?) - julianday(last_attempt_at)) * 86400.0 >= 1.0
              )
            ORDER BY enqueued_at
            LIMIT ?
            """,
            (now, _CLAIM_CANDIDATES),
        ).fetchall()
        for row in rows:
            entry = _row_to_entry(row)
            if not entry.is_backoff_elapsed():
                continue
            # Flip to in_progress. The WHERE clause keeps us safe even
            # if another worker claimed the same row first: we'd simply
            # not update anything and try the next candidate.
            cur = self._conn.execute(
                """
                UPDATE embedding_queue
                SET state = 'in_progress'
                WHERE doc_id = ? AND state = 'pending'
                """,
                (entry.doc_id,),
            )
            # Close the implicit transaction. pysqlite3 leaves the
            # connection ``in_transaction`` after the UPDATE; without
            # this commit a second connection (the background worker
            # writing to the same row) gets ``database is locked``.
            self._conn.commit()
            if cur.rowcount == 0:
                continue
            return EmbeddingQueueEntry(
                doc_id=entry.doc_id,
                enqueued_at=entry.enqueued_at,
                attempts=entry.attempts,
                max_attempts=entry.max_attempts,
                last_error=entry.last_error,
                last_attempt_at=entry.last_attempt_at,
                state="in_progress",
            )
        return None

    def mark_done(self, doc_id: str) -> None:
        """Mark ``doc_id`` as ``done``. No-op if the row is missing.

        See :meth:`enqueue` for the rationale behind the explicit
        ``commit()`` (pysqlite3 quirk).
        """
        self._conn.execute(
            """
            UPDATE embedding_queue
            SET state = 'done',
                last_error = NULL
            WHERE doc_id = ?
            """,
            (doc_id,),
        )
        self._conn.commit()

    def mark_failed(
        self,
        doc_id: str,
        error: str,
        *,
        permanent: bool = False,
        now: str | None = None,
    ) -> EmbeddingQueueEntry | None:
        """Record a failure for ``doc_id``.

        Args:
            doc_id: The document that just failed.
            error: Human-readable error message (truncated to 1 KB).
            permanent: If ``True``, the row goes straight to ``failed``
                regardless of ``attempts``. Used for non-retryable
                errors (e.g. "empty text"). If ``False`` (the default),
                the row is re-queued (``pending``) until
                ``attempts >= max_attempts``, then parked in ``failed``.
            now: ISO-8601 timestamp; defaults to wall clock.

        Returns the updated entry, or ``None`` if the row vanished
        between claim and mark (e.g. the document was hard-deleted).

        NOTE: not safe against multiple concurrent writers. The
        SELECT-then-UPDATE sequence assumes a single consumer; the
        worker's :class:`~kb_mcp_lite.worker.EmbeddingWorker` enforces
        that. Convert to ``UPDATE ... RETURNING`` to support multiple
        workers in v0.8.2.
        # TODO(v0.8.2): convert to ``UPDATE ... SET attempts =
        attempts + 1, ... RETURNING attempts, max_attempts`` so a
        concurrent worker cannot double-increment ``attempts``.
        """
        if now is None:
            now = _now_iso()
        truncated = (error or "")[:1024] or None
        # We need the current attempts/max_attempts to decide between
        # "back to pending" and "park in failed". Read first, then
        # write. See the docstring NOTE above for the single-consumer
        # assumption.
        row = self._conn.execute(
            "SELECT attempts, max_attempts FROM embedding_queue WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        attempts, max_attempts = int(row["attempts"]), int(row["max_attempts"])
        new_attempts = attempts + 1
        exhausted = permanent or new_attempts >= max_attempts
        new_state = "failed" if exhausted else "pending"
        # See :meth:`enqueue` for why this explicit commit is needed
        # under pysqlite3.
        self._conn.execute(
            """
            UPDATE embedding_queue
            SET state = ?,
                attempts = ?,
                last_error = ?,
                last_attempt_at = ?
            WHERE doc_id = ?
            """,
            (new_state, new_attempts, truncated, now, doc_id),
        )
        self._conn.commit()
        logger.debug(
            "embedding_queue: %s -> %s (attempt %d/%d): %s",
            doc_id,
            new_state,
            new_attempts,
            max_attempts,
            truncated,
        )
        return EmbeddingQueueEntry(
            doc_id=doc_id,
            enqueued_at=self._conn.execute(
                "SELECT enqueued_at FROM embedding_queue WHERE doc_id = ?", (doc_id,)
            ).fetchone()["enqueued_at"],
            attempts=new_attempts,
            max_attempts=max_attempts,
            last_error=truncated,
            last_attempt_at=now,
            state=new_state,
        )

    # ---- read / status --------------------------------------------------

    def get(self, doc_id: str) -> EmbeddingQueueEntry | None:
        """Return the entry for ``doc_id``, or ``None`` if absent."""
        row = self._conn.execute(
            """
            SELECT doc_id, enqueued_at, attempts, max_attempts,
                   last_error, last_attempt_at, state
            FROM embedding_queue
            WHERE doc_id = ?
            """,
            (doc_id,),
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def count_by_state(self) -> dict[str, int]:
        """Return a ``{state: count}`` mapping covering all known states.

        States with zero rows are still present in the result, so the
        caller can format a complete report without special-casing.
        """
        rows = self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM embedding_queue GROUP BY state"
        ).fetchall()
        out: dict[str, int] = {s: 0 for s in self.VALID_STATES}
        for r in rows:
            out[str(r["state"])] = int(r["n"])
        return out

    def list_entries(
        self,
        state: str | None = None,
        limit: int = 100,
    ) -> list[EmbeddingQueueEntry]:
        """Return up to ``limit`` rows, optionally filtered by ``state``.

        Results are ordered by ``enqueued_at`` so the worker can see the
        oldest pending rows first.
        """
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be in 1..1000")
        if state is not None and state not in self.VALID_STATES:
            raise ValueError(f"unknown state {state!r}")
        if state is None:
            rows = self._conn.execute(
                """
                SELECT doc_id, enqueued_at, attempts, max_attempts,
                       last_error, last_attempt_at, state
                FROM embedding_queue
                ORDER BY enqueued_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT doc_id, enqueued_at, attempts, max_attempts,
                       last_error, last_attempt_at, state
                FROM embedding_queue
                WHERE state = ?
                ORDER BY enqueued_at
                LIMIT ?
                """,
                (state, limit),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def status(self) -> dict[str, Any]:
        """Return a structured status report for the CLI / MCP layer.

        Shape::

            {
                "counts": {"pending": int, "in_progress": int,
                           "done": int, "failed": int},
                "oldest_pending": {"doc_id": str, "enqueued_at": str} | None,
                "oldest_failed":  {"doc_id": str, "enqueued_at": str,
                                   "last_error": str | None,
                                   "attempts": int} | None,
            }
        """
        counts = self.count_by_state()
        oldest_pending_row = self._conn.execute(
            """
            SELECT doc_id, enqueued_at FROM embedding_queue
            WHERE state = 'pending'
            ORDER BY enqueued_at LIMIT 1
            """
        ).fetchone()
        oldest_failed_row = self._conn.execute(
            """
            SELECT doc_id, enqueued_at, last_error, attempts
            FROM embedding_queue
            WHERE state = 'failed'
            ORDER BY enqueued_at LIMIT 1
            """
        ).fetchone()
        return {
            "counts": counts,
            "oldest_pending": dict(oldest_pending_row) if oldest_pending_row is not None else None,
            "oldest_failed": dict(oldest_failed_row) if oldest_failed_row is not None else None,
        }

    # ---- admin ----------------------------------------------------------

    def retry(self, doc_ids: Iterable[str]) -> int:
        """Flip the given ``doc_ids`` back to ``pending`` for a fresh try.

        Returns the number of rows actually moved. Rows already in
        ``pending`` are left untouched (their ``enqueued_at`` is not
        bumped — that would push them to the back of the queue and
        defeat the purpose of a manual retry).
        """
        moved = 0
        for doc_id in doc_ids:
            # See :meth:`enqueue` for why commit() is required under
            # pysqlite3 even though the connection is in
            # ``isolation_level=""`` mode.
            cur = self._conn.execute(
                """
                UPDATE embedding_queue
                SET state = 'pending',
                    attempts = 0,
                    last_error = NULL,
                    last_attempt_at = NULL
                WHERE doc_id = ? AND state IN ('done', 'failed', 'in_progress')
                """,
                (doc_id,),
            )
            self._conn.commit()
            moved += cur.rowcount
        return moved

    def clear(self, doc_ids: Iterable[str]) -> int:
        """Hard-delete the given rows from the queue. Returns row count."""
        moved = 0
        for doc_id in doc_ids:
            cur = self._conn.execute(
                "DELETE FROM embedding_queue WHERE doc_id = ?",
                (doc_id,),
            )
            self._conn.commit()
            moved += cur.rowcount
        return moved

    # ---- internals ------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_entry(row: sqlite3.Row | dict) -> EmbeddingQueueEntry:
    """Coerce a sqlite3.Row (or plain dict) into an ``EmbeddingQueueEntry``."""
    return EmbeddingQueueEntry(
        doc_id=str(row["doc_id"]),
        enqueued_at=str(row["enqueued_at"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        last_error=row["last_error"],
        last_attempt_at=row["last_attempt_at"],
        state=str(row["state"]),
    )


__all__ = [
    "BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "EmbeddingQueue",
    "EmbeddingQueueEntry",
]
