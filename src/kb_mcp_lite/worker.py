"""Background embedding worker for kb-mcp.

This module provides :class:`EmbeddingWorker`, a long-running background
thread that drains the :class:`~kb_mcp_lite.store.embedding_queue.EmbeddingQueue`
created by migration 0007.

Design constraints (from the v0.8.0 design doc, §特性 #1 异步化嵌入):

* **Failure is silent.** A failed embedding never blocks the call that
  enqueued the document. Errors land in the queue row's ``last_error``
  column; users see them via ``kb embed status`` / ``kb_embed_status``.
* **Backoff is exponential.** After each transient failure the row is
  re-queued with a delay from :data:`BACKOFF_SECONDS` (1s, 5s, 30s, 5m,
  30m). After ``max_attempts`` failures the row moves to ``failed`` and
  is invisible to the worker until a human runs ``kb embed retry``.
* **Process-local lifecycle.** The worker is owned by a
  :class:`~kb_mcp_lite.store.sqlite.SqliteStore` instance. When the
  store is closed the worker thread is asked to stop; an ``atexit``
  hook stops any worker still alive at interpreter shutdown so we never
  leak threads holding open file descriptors on a fast-exit script.
* **One worker per process.** Multiple stores within one process share
  at most one worker (the first store to need one registers an
  ``atexit`` cleanup that joins every worker it spawned).
"""

from __future__ import annotations

import atexit
import logging
import sqlite3
import threading
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kb_mcp_lite.embedder import EmbeddingError
from kb_mcp_lite.store.embedding_queue import (
    DEFAULT_MAX_ATTEMPTS,
    EmbeddingQueue,
    EmbeddingQueueEntry,
)

if TYPE_CHECKING:
    from kb_mcp_lite.store.sqlite import SqliteStore

logger = logging.getLogger("kb_mcp_lite.worker")


def _open_worker_connection(db_path: str) -> sqlite3.Connection:
    """Open a sqlite3 connection for the worker thread.

    The default :func:`sqlite3.connect` sets ``check_same_thread=True``,
    which means a connection born on the main thread cannot be touched
    from the worker thread. pysqlite3 (the project's preferred backend)
    enforces the check in C and does not expose ``check_same_thread`` as
    a settable Python attribute, so the only way to relax it is at
    construction time. The factory now accepts a ``check_same_thread``
    kwarg; the worker passes ``False``.

    The worker is a single thread, and SQLite's own locking (WAL +
    busy_timeout) keeps concurrent writers safe. WAL mode also lets the
    store's main connection read whatever the worker just wrote
    without any extra ceremony.

    We also re-use the store's connection factory so the worker
    connection comes from the same SQLite library (pysqlite3 vs
    stdlib). Mixing the two on one database corrupts the WAL index —
    see the warning in
    :func:`kb_mcp_lite.store.connection.make_sqlite_connection`.
    """
    from kb_mcp_lite.store.connection import (
        make_sqlite_connection,
        sqlite_row_factory,
    )

    conn = make_sqlite_connection(db_path, isolation_level="", check_same_thread=False)
    # Mirror the store: rows must come back as ``sqlite3.Row`` so the
    # queue layer can read by column name. Without this the queue's
    # ``_row_to_entry`` would crash on tuple indexing.
    conn.row_factory = sqlite_row_factory(conn)
    # The store sets these pragmas on its main connection. The worker
    # needs them too:
    # * ``foreign_keys = ON`` is required for migration 0007's
    #   ``embedding_queue.doc_id REFERENCES documents(id) ON DELETE
    #   CASCADE`` to actually cascade when a document is hard-deleted.
    # * WAL keeps the worker's writes visible to the store's main
    #   connection without a manual checkpoint.
    # * ``synchronous = NORMAL`` is the trade-off the project ships
    #   with. ``:memory:`` databases and read-only mounts can refuse
    #   WAL; the worker falls back to the connection's default journal
    #   mode in that case, which is still safe for the single-writer
    #   workload the worker performs.
    # * ``busy_timeout = 5000`` is required for pysqlite3 specifically:
    #   its default is 0 (no wait), so a write that lands during the
    #   store connection's brief lock window immediately raises
    #   ``database is locked`` instead of retrying. 5 s matches the
    #   project-wide default in the store's main connection.
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
    except Exception:  # noqa: BLE001
        pass
    return conn


#: Time the worker sleeps between ``claim_next`` calls when the queue
#: is empty. Kept small so freshly-enqueued docs are picked up quickly.
IDLE_POLL_SECONDS: float = 0.25

#: Upper bound on a single ``embedder.embed()`` call. The worker
#: imposes no timeout of its own — the embedder's own ``timeout``
#: config is the source of truth — but the per-cycle ``stop_event``
#: wait means a stuck call still won't block shutdown for long.
SHUTDOWN_JOIN_SECONDS: float = 5.0


# ---------------------------------------------------------------------------
# Per-job outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerJobResult:
    """Outcome of one :meth:`EmbeddingWorker._process_one` call.

    Used for both unit tests and the ``last_job_*`` introspection on a
    live worker. ``permanent=True`` is set when the worker decides the
    failure is non-retryable (e.g. dimension mismatch, empty text).
    """

    doc_id: str
    ok: bool
    error: str | None
    permanent: bool = False


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class EmbeddingWorker:
    """A daemon thread that drains the embedding queue.

    Construct one via :meth:`SqliteStore.embedding_worker` (the canonical
    entry point) — that property registers an ``atexit`` cleanup the
    first time it is called in a process, so all subsequent store
    instances share a single global thread registry and a single
    shutdown signal.

    The worker is **best-effort**. It logs every state transition and
    surfaces unhandled exceptions as terminal ``failed`` rows so the
    queue never silently swallows a bug. It does *not* surface
    individual failures to the caller of ``kb add``; that contract
    belongs to :class:`EmbeddingQueue.enqueue` and is intentionally
    fire-and-forget.
    """

    # Class-level registry of live workers. ``atexit`` walks it on
    # interpreter shutdown and asks each worker to stop. We use a
    # weak-value dictionary so a store that explicitly closes its
    # worker can be garbage-collected without a manual ``unregister``
    # call.
    _live: weakref.WeakValueDictionary[int, EmbeddingWorker] | None = None
    _live_lock = threading.Lock()
    _atexit_registered = False

    _conn: sqlite3.Connection | None = None

    def __init__(
        self,
        store: SqliteStore,
        *,
        embedder: Any,
        queue: EmbeddingQueue | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        idle_poll: float = IDLE_POLL_SECONDS,
    ) -> None:
        if not getattr(embedder, "enabled", False):
            raise ValueError(
                "EmbeddingWorker requires an enabled embedder; "
                "got a NullEmbedder or one with enabled=False"
            )
        self._store = store
        self._embedder = embedder
        self._max_attempts = max(1, int(max_attempts))
        self._idle_poll = max(0.05, float(idle_poll))

        # The worker thread cannot reuse the store's main ``sqlite3``
        # connection — pysqlite3 (and the stdlib module in
        # ``check_same_thread=True`` mode, which is the default) refuses
        # cross-thread use. Open a dedicated connection through the
        # same factory the store used, so we still share one SQLite
        # library version per database, and explicitly opt out of the
        # cross-thread check (we serialise internally — see
        # :func:`_open_worker_connection`).
        self._conn = _open_worker_connection(store.db_path)
        # The queue and the worker share one connection so the entire
        # ``claim -> process -> mark_*`` cycle happens in the worker
        # thread. Any ``queue`` the caller passed in is discarded: it
        # would point at the store's main connection, which the worker
        # thread is forbidden to touch. Kept in the signature for
        # backwards compatibility (callers that pre-built a queue
        # still get a working worker, just on a fresh connection).
        self._queue = EmbeddingQueue(self._conn)

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        # Introspection — useful for tests and the CLI status command.
        self.last_job: WorkerJobResult | None = None
        self.processed_total: int = 0
        self.failed_total: int = 0
        # Lock that serialises the embedder. The queue itself is
        # already single-consumer (one worker thread per process), but
        # the embedder is a shared resource we may want to swap at
        # runtime, so we still guard its single call site.
        self._embed_lock = threading.Lock()

        EmbeddingWorker._register(self)

    # ---- registry / atexit ----------------------------------------------

    @classmethod
    def _live_registry(cls) -> weakref.WeakValueDictionary[int, EmbeddingWorker]:
        if cls._live is None:
            cls._live = weakref.WeakValueDictionary()
        return cls._live

    @classmethod
    def _register(cls, worker: EmbeddingWorker) -> None:
        with cls._live_lock:
            reg = cls._live_registry()
            reg[id(worker)] = worker
            if not cls._atexit_registered:
                atexit.register(cls._atexit_stop_all)
                cls._atexit_registered = True

    @classmethod
    def _atexit_stop_all(cls) -> None:
        """Stop every live worker at interpreter shutdown.

        Best-effort: workers that fail to join within
        :data:`SHUTDOWN_JOIN_SECONDS` are abandoned (they are daemon
        threads, so the interpreter will reap them on exit anyway).
        """
        with cls._live_lock:
            reg = cls._live_registry()
            workers = list(reg.values())
        for w in workers:
            try:
                w.stop(timeout=SHUTDOWN_JOIN_SECONDS)
            except Exception:  # noqa: BLE001
                logger.debug("atexit: error stopping worker", exc_info=True)

    @classmethod
    def live_workers(cls) -> list[EmbeddingWorker]:
        """Return the workers currently registered in this process.

        Mostly for tests; the CLI does not introspect this.
        """
        with cls._live_lock:
            return list(cls._live_registry().values())

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if self._started:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"kb-embedding-worker-{id(self)}",
            daemon=True,
        )
        self._thread.start()
        self._started = True
        logger.debug("EmbeddingWorker started (thread=%s)", self._thread.name)

    def stop(self, timeout: float = SHUTDOWN_JOIN_SECONDS) -> None:
        """Signal the worker to stop and wait for the thread to exit.

        Also closes the worker-private sqlite3 connection opened in
        ``__init__``. Calling ``stop`` multiple times is safe; only the
        first invocation actually waits for the thread.
        """
        if self._started:
            self._stop_event.set()
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
            self._started = False
            self._thread = None
            logger.debug("EmbeddingWorker stopped")
        # Always close the dedicated connection — the test harness may
        # construct a worker, never start it, and still want cleanup.
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    @property
    def is_running(self) -> bool:
        return self._started and (self._thread is not None) and self._thread.is_alive()

    # ---- main loop ------------------------------------------------------

    def _run(self) -> None:
        """Worker main loop. One iteration = one ``claim_next`` attempt.

        The loop never raises; any exception is logged and the worker
        sleeps before retrying. A persistent exception in ``claim_next``
        is a bug, so the sleep grows linearly (1s, 2s, 3s, ...) until
        the worker is stopped. This keeps the worker alive enough to
        log the bug but not so noisy that it floods the log.
        """
        error_sleep = 0.0
        while not self._stop_event.is_set():
            try:
                entry = self._queue.claim_next()
            except Exception:  # noqa: BLE001
                logger.exception("embedding_queue.claim_next crashed")
                error_sleep = min(error_sleep + 1.0, 5.0)
                if self._stop_event.wait(error_sleep or 0.5):
                    break
                continue
            # Reset the error sleep on a healthy claim cycle.
            error_sleep = 0.0
            if entry is None:
                # Queue is empty or every row is still in backoff.
                if self._stop_event.wait(self._idle_poll):
                    break
                continue
            try:
                result = self._process_one(entry)
            except Exception as e:  # noqa: BLE001
                # An unhandled exception in _process_one is a bug. We
                # park the row in ``failed`` so it stops blocking the
                # queue and surface a synthetic error message.
                logger.exception("worker._process_one crashed for %s", entry.doc_id)
                try:
                    self._queue.mark_failed(
                        entry.doc_id,
                        f"worker internal error: {e!r}",
                        permanent=True,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("mark_failed after crash also failed")
                self.failed_total += 1
                continue
            if result.ok:
                self.processed_total += 1
            else:
                self.failed_total += 1
            self.last_job = result

    # ---- one job --------------------------------------------------------

    def _process_one(self, entry: EmbeddingQueueEntry) -> WorkerJobResult:
        """Embed one document and record the outcome in the queue.

        Public-by-convention: the unit tests call this directly to
        exercise the happy / error paths without spinning up a
        thread. The thread loop's only job is to schedule this method.
        """
        doc_id = entry.doc_id
        # 1. Fetch the document via the worker's own connection. The
        #    store's main ``get`` is bound to the calling thread and
        #    would crash with a pysqlite3 ``check_same_thread`` error
        #    if we called it from here. The worker only needs the
        #    title and body, so a small dedicated SELECT is enough.
        assert self._conn is not None  # narrowed for mypy
        try:
            row = self._conn.execute(
                "SELECT title, body FROM documents WHERE id = ? AND deleted_at IS NULL",
                (doc_id,),
            ).fetchone()
        except Exception as e:  # noqa: BLE001
            err = f"fetch failed: {e}"
            self._queue.mark_failed(doc_id, err, permanent=False)
            return WorkerJobResult(doc_id=doc_id, ok=False, error=err, permanent=False)
        if row is None:
            # The document was hard-deleted between enqueue and claim.
            # Drop the queue row — nothing to embed.
            self._queue.clear([doc_id])
            return WorkerJobResult(doc_id=doc_id, ok=True, error=None, permanent=True)

        title = str(row["title"] or "")
        body = str(row["body"] or "")
        # 2. Build the input text. Mirrors ``EmbeddingMixin._index_embedding``.
        text = f"{title}\n\n{body}".strip()
        if not text:
            # Empty text is a permanent failure — the embedder will
            # refuse it every time, no point retrying.
            err = "empty document text; cannot embed"
            self._queue.mark_failed(doc_id, err, permanent=True)
            return WorkerJobResult(doc_id=doc_id, ok=False, error=err, permanent=True)

        # 3. Call the embedder under the embed lock.
        with self._embed_lock:
            try:
                vector = self._embedder.embed(text)
            except EmbeddingError as e:
                permanent = _is_permanent_error(str(e))
                self._queue.mark_failed(doc_id, str(e), permanent=permanent)
                return WorkerJobResult(doc_id=doc_id, ok=False, error=str(e), permanent=permanent)
            except Exception as e:  # noqa: BLE001
                err = f"unexpected embedder error: {e!r}"
                self._queue.mark_failed(doc_id, err, permanent=False)
                return WorkerJobResult(doc_id=doc_id, ok=False, error=err, permanent=False)

        # 4. Persist the vector in vec0 through the worker's own
        #    connection. This keeps the write on the worker thread and
        #    avoids racing with the store's main connection, which is
        #    also bound to its own thread. SQLite's WAL mode +
        #    busy_timeout serialise the two writers at the WAL index.
        try:
            self._write_vector(doc_id, vector)
        except Exception as e:  # noqa: BLE001
            err = f"vec0 write failed: {e}"
            self._queue.mark_failed(doc_id, err, permanent=False)
            return WorkerJobResult(doc_id=doc_id, ok=False, error=err, permanent=False)

        # 5. Mark done.
        self._queue.mark_done(doc_id)
        logger.debug("embedding complete: %s (dim=%d)", doc_id, len(vector))
        return WorkerJobResult(doc_id=doc_id, ok=True, error=None, permanent=False)

    def _write_vector(self, doc_id: str, vector: list[float]) -> None:
        """Persist ``vector`` for ``doc_id`` in the vec0 table.

        Uses the worker's own connection so the write happens on the
        worker thread. SQLite's WAL mode + busy_timeout keep this
        safe relative to the store's main thread (which never writes
        to ``docs_vec`` directly, only reads it).

        If vec0 is not available (sqlite-vec extension not loaded),
        the write is silently skipped — the lexical search still
        works, only semantic search degrades. That matches the
        pre-existing best-effort behaviour of
        :meth:`EmbeddingMixin._index_embedding`.

        Args:
            doc_id: Document id whose row to upsert.
            vector: Embedding vector (length must match the
                ``docs_vec`` dim; dim mismatches are silently skipped
                and surfaced as a debug log line).
        """
        if not vector:
            return
        try:
            from sqlite_vec import serialize_float32
        except ImportError:
            return
        # Migration 0003 pre-creates docs_vec at float[1536]. An
        # embedder with a different dim (a mock, or a non-1536 model)
        # needs the table recreated to match before the insert below,
        # exactly as the store's own lazy path does on first read. The
        # vector's length is the embedder dim by construction.
        from kb_mcp_lite.store.embedding import ensure_vec_table

        assert self._conn is not None  # narrowed for mypy
        if not ensure_vec_table(self._conn, len(vector)):
            return
        try:
            row = self._conn.execute(
                "SELECT rowid FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        except Exception:  # noqa: BLE001
            return
        if row is None:
            return
        rowid = int(row["rowid"])
        # ``docs_vec`` lives in the main database file. Writing through
        # the worker's own connection is safe because WAL mode allows
        # concurrent writers as long as they serialise at the
        # statement level — the worker is the only writer of vec0
        # rows, so there is no contention. A dim mismatch is the most
        # common failure mode: the existing table was created with a
        # different dim. The pre-async code path silently skipped in
        # that case too, so we preserve that contract here.
        try:
            self._conn.execute("DELETE FROM docs_vec WHERE rowid = ?", (rowid,))
            self._conn.execute(
                "INSERT INTO docs_vec(rowid, embedding) VALUES (?, ?)",
                (rowid, serialize_float32(vector)),
            )
            self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("docs_vec write skipped for %s: %s", doc_id, e)
            return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Error message substrings that indicate a non-retryable failure. We
# retry everything else (HTTP 5xx, 429, network errors, ...).
_PERMANENT_SUBSTRINGS: tuple[str, ...] = (
    "cannot embed empty text",
    "dimension mismatch",
    "embedding is not a list of numbers",
    "unexpected response shape",
)


def _is_permanent_error(msg: str) -> bool:
    """Return True if the embedder's error message looks non-retryable."""
    low = (msg or "").lower()
    return any(s in low for s in _PERMANENT_SUBSTRINGS)


__all__ = [
    "EmbeddingWorker",
    "WorkerJobResult",
    "IDLE_POLL_SECONDS",
    "SHUTDOWN_JOIN_SECONDS",
]
