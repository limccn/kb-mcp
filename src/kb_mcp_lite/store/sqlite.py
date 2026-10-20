"""SQLite storage implementation."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from collections.abc import Iterable, Iterator

from kb_mcp_lite.schema import (
    Document,
    Link,
    ImportReport,
    NotFoundError,
    DuplicateError,
    ValidationError,
)
from kb_mcp_lite.store.connection import make_sqlite_connection, sqlite_row_factory
from kb_mcp_lite.store.maintenance import MaintenanceMixin
from kb_mcp_lite.store.search import SearchMixin
from kb_mcp_lite.store.versioning import VersioningMixin
from kb_mcp_lite.store.embedding import EmbeddingMixin
import builtins

if TYPE_CHECKING:
    from kb_mcp_lite.concurrency import WriteLock
    from kb_mcp_lite.store.embedding_queue import EmbeddingQueue
    from kb_mcp_lite.worker import EmbeddingWorker

logger = logging.getLogger("kb_mcp_lite.store.sqlite")


T = TypeVar("T")


class SqliteStore(MaintenanceMixin, SearchMixin, VersioningMixin, EmbeddingMixin):
    """SQLite-based storage implementation.

    Combines functionality from multiple mixins:
    - MaintenanceMixin: Health checks, stats, pruning, reindexing
    - SearchMixin: Full-text and semantic search
    - VersioningMixin: Document version history, diff, restore
    - EmbeddingMixin: Vector storage and semantic search support
    """

    def __init__(
        self,
        db_path: str | Path,
        embedder: Any | None = None,
        *,
        strict_lock: bool = False,
        lock_timeout: float = 10.0,
        auto_start_worker: bool = True,
    ) -> None:
        self.db_path = str(db_path)
        self._strict_lock = strict_lock
        self._lock_timeout = float(lock_timeout)
        self._auto_start_worker = bool(auto_start_worker)
        self._conn = self._open_connection()
        self._vec_conn = None
        self._init_db()
        if embedder is None:
            from kb_mcp_lite.embedder import make_embedder

            embedder = make_embedder()
        self._embedder = embedder
        # The queue is bound to the store's main connection because all
        # enqueue / status / retry calls happen on the calling thread.
        # The worker, when started, opens its own cross-thread-safe
        # connection (see :mod:`kb_mcp_lite.worker`).
        from kb_mcp_lite.store.embedding_queue import EmbeddingQueue

        self._embedding_queue = EmbeddingQueue(self._conn)
        self._embedding_worker: EmbeddingWorker | None = None
        # Cache auto-link config (loaded once, avoids per-write YAML parse)
        self._auto_link_cfg: dict = {}
        try:
            from kb_mcp_lite.config import load_config

            _cfg = load_config()
            self._auto_link_cfg = _cfg.get("kb", {}).get("auto_link", {})
        except Exception:
            pass

    def _open_connection(self) -> sqlite3.Connection:
        """Open a connection to the SQLite database.

        Uses the same connection factory as the vec0 side connection so
        every connection to this database shares one SQLite library
        (mixing pysqlite3 and stdlib sqlite3 corrupts a WAL-mode db).
        """
        dir_path = os.path.dirname(self.db_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)

        conn = make_sqlite_connection(self.db_path, isolation_level="")
        conn.row_factory = sqlite_row_factory(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    def _init_db(self) -> None:
        """Initialize database schema and run migrations."""
        from kb_mcp_lite.migrations import apply_migrations

        apply_migrations(self._conn)
        self._conn.commit()

    @property
    def path(self):
        """Return the path to the database file (compatibility alias for db_path)."""
        return self.db_path

    @property
    def write_lock(self) -> WriteLock:
        """Return a :class:`~kb_mcp_lite.concurrency.WriteLock` bound to this store.

        The lock is constructed lazily on first access so stores that do
        not need cross-process serialisation pay nothing. Use as a context
        manager around write operations::

            with store.write_lock:
                store.add(doc)
                store.update(...)
        """
        # Local import to avoid a top-level cycle (concurrency imports
        # KbMcpError from schema, but schema does not need concurrency).
        from kb_mcp_lite.concurrency import WriteLock

        wl = WriteLock(self.db_path, timeout=self._lock_timeout)
        return wl

    def init(self) -> None:
        """Initialize a new empty knowledge base."""
        # Already handled by migrations
        pass

    # ---- embedding queue / worker ---------------------------------------

    @property
    def embedding_queue(self) -> EmbeddingQueue:
        """The :class:`EmbeddingQueue` bound to this store's connection."""
        return self._embedding_queue

    @property
    def embedding_worker(self) -> EmbeddingWorker | None:
        """Lazily construct, start, and return the background worker.

        Returns ``None`` when the embedder is disabled or the store
        was constructed with ``auto_start_worker=False``. The worker
        keeps running until :meth:`close` is called or the process
        exits (``atexit`` cleanup also stops it).
        """
        if not self._auto_start_worker:
            return None
        if not getattr(self._embedder, "enabled", False):
            return None
        if self._embedding_worker is None:
            from kb_mcp_lite.worker import EmbeddingWorker

            self._embedding_worker = EmbeddingWorker(self, embedder=self._embedder)
            self._embedding_worker.start()
        return self._embedding_worker

    def enqueue_embedding(self, doc_id: str) -> bool:
        """Enqueue ``doc_id`` for asynchronous embedding.

        No-op (returns ``False``) when the embedder is disabled.
        Otherwise the doc is added to the queue in ``pending`` state
        and a running worker (if any) picks it up. Returns ``True``
        if the row was enqueued, ``False`` otherwise.
        """
        if not getattr(self._embedder, "enabled", False):
            return False
        self._embedding_queue.enqueue(doc_id)
        return True

    def process_embedding_queue(
        self,
        *,
        max_jobs: int | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Drain the queue synchronously on the calling thread.

        Used by the ``kb embed`` admin commands and by tests. The
        background worker (if any) keeps running in parallel; both
        callers go through the same per-row claim / mark-done
        protocol, so whichever thread claims a row first wins.
        """
        import time as _time

        from kb_mcp_lite.worker import EmbeddingWorker

        # Reuse a single scratch worker across the whole drain so we
        # only pay one extra connection open/close. The scratch worker
        # writes vec0 rows through its own connection; the queue reads /
        # claims run on the store's main connection, so no cross-thread
        # sqlite usage is involved.
        if not getattr(self._embedder, "enabled", False):
            # No embedder => nothing to drain; still report queue state.
            status = self._embedding_queue.status()
            status["processed"] = 0
            return status
        # If the store already has a background worker running (the
        # usual case for an MCP server / long-lived admin process),
        # reuse it instead of spinning up a second worker thread that
        # would race on the same ``embedding_queue`` rows. Two workers
        # on the same store are safe (claim_next uses
        # ``WHERE state='pending'``) but wasteful — each holds its own
        # sqlite3 connection and would call the embedder from a
        # different thread. The background worker drains in parallel
        # with the synchronous loop; both compete fairly.
        background = self.embedding_worker
        if background is not None:
            scratch = background
            owns_scratch = False
        else:
            scratch = EmbeddingWorker(self, embedder=self._embedder)
            owns_scratch = True
        processed = 0
        deadline = None if timeout <= 0 else _time.monotonic() + timeout
        try:
            while True:
                if max_jobs is not None and processed >= max_jobs:
                    break
                if deadline is not None and _time.monotonic() >= deadline:
                    break
                entry = self._embedding_queue.claim_next()
                if entry is None:
                    if deadline is not None and _time.monotonic() < deadline:
                        remaining = deadline - _time.monotonic()
                        _time.sleep(min(0.1, remaining))
                        counts = self._embedding_queue.count_by_state()
                        if counts.get("pending", 0) == 0:
                            break
                        continue
                    break
                scratch._process_one(entry)  # noqa: SLF001
                processed += 1
        finally:
            # Only stop the worker if we constructed it ourselves. The
            # background worker owned by ``self.embedding_worker`` is
            # stopped by ``SqliteStore.close()`` (or ``atexit``);
            # stopping it here would tear down a thread the rest of
            # the process still relies on.
            if owns_scratch:
                scratch.stop()
        status = self._embedding_queue.status()
        status["processed"] = processed
        return status

    def embedding_queue_status(self) -> dict[str, Any]:
        """Return the embedding queue status report (counts + oldest rows).

        Shape is produced by :meth:`EmbeddingQueue.status`.
        """
        return self._embedding_queue.status()

    def embedding_status(self) -> dict[str, Any]:
        """Return a full embedder + queue report for CLI / MCP admin tools.

        Combines the embedder capability (``enabled`` / ``dim``), the
        number of documents that actually carry a ``docs_vec`` row, and
        the queue breakdown. Queue state stays meaningful even when the
        embedder is currently disabled (jobs that piled up while it was
        off are still visible, so a human can retry them after fixing
        the config).
        """
        emb = getattr(self, "_embedder", None)
        enabled = bool(emb and getattr(emb, "enabled", False))
        dim = getattr(emb, "dim", 0) or 0
        n_vec = 0
        if enabled:
            try:
                row = self._conn.execute("SELECT COUNT(*) FROM docs_vec").fetchone()
                n_vec = int(row[0]) if row else 0
            except Exception:  # noqa: BLE001
                n_vec = 0
        queue = self._embedding_queue.status()
        return {
            "embedder_enabled": enabled,
            "dim": dim,
            "indexed_documents": n_vec,
            "queue": queue["counts"],
            "total_enqueued": sum(queue["counts"].values()),
            "oldest_pending": queue["oldest_pending"],
            "oldest_failed": queue["oldest_failed"],
        }

    def retry_embedding(self, doc_id: str | None = None) -> int:
        """Re-queue embedding jobs that are not currently pending.

        Args:
            doc_id: When given, reset that single document (works from
                ``done`` / ``in_progress`` too, so a specific doc can be
                force re-embedded). When omitted, reset every ``failed``
                row so a later drain retries them all.

        Returns:
            The number of rows flipped back to ``pending``.
        """
        queue = self._embedding_queue
        if doc_id is not None:
            return queue.retry([doc_id])
        failed = queue.list_entries(state="failed")
        return queue.retry([e.doc_id for e in failed])

    def close(self) -> None:
        """Close the database connection (including the vec0 side connection).

        Also stops the background embedding worker (if any). Idempotent.
        """
        worker = self._embedding_worker
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                logger.debug("error stopping embedding worker", exc_info=True)
            self._embedding_worker = None
        try:
            self._conn.close()
        except (sqlite3.ProgrammingError, Exception):
            pass
        if self._vec_conn is not None and self._vec_conn is not False:
            try:
                self._vec_conn.close()
            except Exception:
                pass
            self._vec_conn = None

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Cursor]:
        """Start a transaction and yield a cursor. Commits on success, rolls back on error."""
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            cur.close()

    def _row_to_doc(self, row: dict[str, Any]) -> Document:
        """Convert a database row to a Document object."""
        return Document.from_row(row)

    @staticmethod
    def _row_to_link(row: dict[str, Any]) -> Link:
        """Convert a database row to a Link object."""
        return Link(**row)

    @staticmethod
    def _now_iso() -> str:
        """Return the current UTC time as an ISO-8601 string."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        """Parse an ISO-8601 string to datetime, or return None."""
        if not value:
            return None
        s = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    # ---- read operations ------------------------------------------------------

    def get(self, doc_id: str, include_deleted: bool = False) -> Document:
        """Get a document by ID.

        Raises NotFoundError if the document doesn't exist.
        """
        sql = "SELECT * FROM documents WHERE id = ?"
        params: list[Any] = [doc_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"

        row = self._conn.execute(sql, params).fetchone()
        if not row:
            # Try resolving alias via doc_aliases table
            alias_row = self._conn.execute(
                "SELECT doc_id FROM doc_aliases WHERE alias = ?", (doc_id,)
            ).fetchone()
            if alias_row:
                resolved = alias_row["doc_id"]
                return self.get(resolved, include_deleted=include_deleted)
            raise NotFoundError(doc_id)

        doc = self._row_to_doc(row)
        # Fetch aliases from doc_aliases
        aliases_rows = self._conn.execute(
            "SELECT alias FROM doc_aliases WHERE doc_id = ?", (doc.id,)
        ).fetchall()
        doc.aliases = [r["alias"] for r in aliases_rows]
        return doc

    def list(
        self,
        type: str | None = None,
        tags: builtins.list[str] | None = None,
        link_to: str | None = None,
        link_from: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> builtins.list[Document]:
        """List documents with optional filtering.

        Args:
            type: Filter by document type
            tags: Filter by tags (all tags must be present)
            link_to: Filter documents that link to the given document ID
            link_from: Filter documents that are linked from the given document ID
            limit: Maximum number of results to return (1-1000)
            offset: Number of results to skip for pagination
            include_deleted: Include soft-deleted documents in results
        """
        if limit < 1 or limit > 1000:
            raise ValidationError("limit must be in 1..1000")
        if offset < 0:
            raise ValidationError("offset must be >= 0")

        sql_parts = ["SELECT DISTINCT d.* FROM documents d"]
        params: list[object] = []
        joins = []
        conditions = ["1=1"]

        if not include_deleted:
            conditions.append("d.deleted_at IS NULL")

        if type:
            conditions.append("d.type = ?")
            params.append(type)

        if link_to:
            joins.append("LEFT JOIN links l_to ON l_to.from_id = d.id")
            conditions.append("l_to.to_id = ?")
            params.append(link_to)

        if link_from:
            joins.append("LEFT JOIN links l_from ON l_from.to_id = d.id")
            conditions.append("l_from.from_id = ?")
            params.append(link_from)

        if joins:
            sql_parts.extend(joins)

        sql_parts.append("WHERE " + " AND ".join(conditions))
        sql_parts.append("ORDER BY d.updated_at DESC, d.id ASC LIMIT ? OFFSET ?")
        params.extend([limit, offset])

        sql = " ".join(sql_parts)
        rows = self._conn.execute(sql, params).fetchall()
        docs = [self._row_to_doc(r) for r in rows]

        if tags:
            wanted = set(tags)
            docs = [d for d in docs if wanted.issubset(set(d.tags))]

        return docs

    # ---- write operations ------------------------------------------------------

    def add(self, doc: Document) -> str:
        """Add a new document.

        Returns the generated document ID.
        Raises DuplicateError if a document with the same (type, title) already exists.
        Raises ValidationError if the ID format is invalid.
        """
        # Generate ID if not provided
        if not doc.id:
            doc.id = make_id(doc.type, doc.title)
        elif not re.match(r"^[a-z0-9][a-z0-9/_-]*$", doc.id):
            raise ValidationError(f"id must match ^[a-z0-9][a-z0-9/_-]*$ (got {doc.id!r})")

        # Check for duplicate
        try:
            existing = self.get(doc.id, include_deleted=True)
            raise DuplicateError(doc.id, existing.id)
        except NotFoundError:
            pass

        now = datetime.now(timezone.utc)
        # Preserve timestamps set by the caller (e.g. from frontmatter).
        # The pydantic default_factory already sets both to now() if absent.
        if not doc.created_at:
            doc.created_at = now
        if not doc.updated_at:
            doc.updated_at = now

        with self._txn() as cur:
            cur.execute(
                """
                INSERT INTO documents (
                    id, type, title, body, tags, metadata, source, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc.id,
                    doc.type,
                    doc.title,
                    doc.body,
                    json.dumps(doc.tags, ensure_ascii=False),
                    json.dumps(doc.metadata, ensure_ascii=False) if doc.metadata else "{}",
                    doc.source,
                    doc.created_at.isoformat(),
                    doc.updated_at.isoformat(),
                    doc.deleted_at.isoformat() if doc.deleted_at else None,
                ),
            )
            # Add to FTS index
            cur.execute(
                """
                INSERT INTO docs_fts (rowid, title, body)
                VALUES (last_insert_rowid(), ?, ?)
                """,
                (doc.title, doc.body),
            )
            # Create version entry
            self._record_doc_version(cur, doc, action="create")
            self._record_audit(
                cur,
                entity_type="document",
                entity_id=doc.id,
                action="create",
                detail={"title": doc.title, "type": doc.type},
            )
            # Insert aliases
            if doc.aliases:
                now_str = now.isoformat()
                for alias in doc.aliases:
                    cur.execute(
                        """
                        INSERT INTO doc_aliases (alias, doc_id, created_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT (alias) DO NOTHING
                        """,
                        (alias, doc.id, now_str),
                    )

        # Fire-and-forget: enqueue for async embedding. The background
        # worker (started lazily on first embedder-touching call) picks
        # it up. When the embedder is disabled, this is a no-op and
        # we keep the pre-async contract (no vec0 row written).
        self.enqueue_embedding(doc.id)

        # Auto-link body references (v0.8 特性 #4)
        self._sync_body_references(doc.id, doc.body)

        return doc.id

    def update(self, doc_id: str, **kwargs: Any) -> Document:
        """Update fields on an existing document.

        Supported fields: title, body, tags, source, aliases, metadata.
        ``metadata`` replaces the whole dict (pass ``{}`` to clear); it does
        not merge — read-modify-write if a partial update is needed.
        Raises NotFoundError if the document doesn't exist.
        Raises ValidationError if disallowed fields are passed or no fields given.
        """
        doc = self.get(doc_id)
        allowed_fields = {"title", "body", "tags", "source", "aliases", "metadata"}
        bad = set(kwargs.keys()) - allowed_fields
        if bad:
            raise ValidationError(f"cannot update fields: {sorted(bad)}")
        if not kwargs:
            raise ValidationError("update requires at least one field")
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}

        # Update fields
        for k, v in updates.items():
            if k == "tags":
                if isinstance(v, str):
                    doc.tags = [t.strip() for t in v.split(",") if t.strip()]
                else:
                    doc.tags = list(v)
            elif k == "metadata":
                if not isinstance(v, dict):
                    raise ValidationError("metadata must be a dict")
                doc.metadata = dict(v)
            else:
                setattr(doc, k, v)

        doc.updated_at = datetime.now(timezone.utc)

        with self._txn() as cur:
            cur.execute(
                """
                UPDATE documents
                SET title = ?, body = ?, tags = ?, metadata = ?, source = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    doc.title,
                    doc.body,
                    json.dumps(doc.tags, ensure_ascii=False),
                    json.dumps(doc.metadata, ensure_ascii=False) if doc.metadata else "{}",
                    doc.source,
                    doc.updated_at.isoformat(),
                    doc.id,
                ),
            )
            # Update FTS index
            cur.execute(
                """
                UPDATE docs_fts
                SET title = ?, body = ?
                WHERE rowid = (SELECT rowid FROM documents WHERE id = ?)
                """,
                (doc.title, doc.body, doc.id),
            )
            # Create version entry
            self._record_doc_version(cur, doc, action="update")
            self._record_audit(
                cur,
                entity_type="document",
                entity_id=doc_id,
                action="update",
                detail={"fields": sorted(kwargs.keys())},
            )
            # Update aliases if provided
            if "aliases" in kwargs:
                cur.execute("DELETE FROM doc_aliases WHERE doc_id = ?", (doc.id,))
                if doc.aliases:
                    now_str = datetime.now(timezone.utc).isoformat()
                    for alias in doc.aliases:
                        cur.execute(
                            """
                            INSERT INTO doc_aliases (alias, doc_id, created_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT (alias) DO NOTHING
                            """,
                            (alias, doc.id, now_str),
                        )

        # Fire-and-forget: re-enqueue for async re-embedding (the same
        # idempotent upsert the add path uses; enqueue resets attempts).
        self.enqueue_embedding(doc.id)

        # Auto-link body references (v0.8 特性 #4) — only when body changed
        if "body" in kwargs:
            self._sync_body_references(doc.id, doc.body)

        return doc

    def update_source(self, doc_id: str, source: str | None) -> None:
        """Update only the ``source`` field of a document.

        Unlike :meth:`update`, this does not bump ``updated_at`` and does
        not record a version or audit entry — it exists for the export
        write-back path (see :func:`kb_mcp_lite.md_io.export_dir`), where
        refreshing ``updated_at`` would leave the document newer than the
        file just written and defeat incremental export.

        Raises NotFoundError if the document doesn't exist.
        """
        with self._txn() as cur:
            cur.execute(
                "UPDATE documents SET source = ? WHERE id = ?",
                (source, doc_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(doc_id)

    def delete(self, doc_id: str) -> None:
        """Soft-delete a document.

        Raises NotFoundError if the document doesn't exist.
        Idempotent: deleting an already-deleted doc is a no-op.
        """
        # Check if the document exists and is not already deleted
        try:
            doc = self.get(doc_id)
        except NotFoundError:
            # Check if it exists but is already deleted (idempotent)
            existing = self._conn.execute(
                "SELECT deleted_at FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if existing and existing["deleted_at"] is not None:
                return  # already deleted, no-op
            raise

        now = datetime.now(timezone.utc)
        with self._txn() as cur:
            cur.execute(
                "UPDATE documents SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now.isoformat(), now.isoformat(), doc_id),
            )
            # Create version entry
            self._record_doc_version(cur, doc, action="delete")
            self._record_audit(
                cur,
                entity_type="document",
                entity_id=doc_id,
                action="delete",
                detail={"title": doc.title, "type": doc.type},
            )

        # Drop the vec0 row synchronously (a soft-deleted doc must stop
        # matching semantic search immediately) and clear any pending
        # queue row so a worker does not waste a claim on it. If the doc
        # is restored, ``restore_deleted`` re-enqueues and the worker
        # re-embeds.
        self._remove_embedding(doc_id)
        self._embedding_queue.clear([doc_id])

    def restore_deleted(self, doc_id: str) -> Document:
        """Restore a soft-deleted document.

        Raises NotFoundError if the document doesn't exist or is not deleted.
        """
        doc = self.get(doc_id, include_deleted=True)
        if not doc.deleted_at:
            raise ValidationError(f"Document {doc_id} is not deleted")

        now = datetime.now(timezone.utc)
        doc.deleted_at = None
        doc.updated_at = now

        with self._txn() as cur:
            cur.execute(
                "UPDATE documents SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (now.isoformat(), doc_id),
            )
            # Create version entry
            self._record_doc_version(cur, doc, action="restore")

        # Re-enqueue for async embedding. On a soft-delete the vec0 row
        # and queue row were both cleared, so a restored doc always needs
        # a fresh embedding before it matches semantic search again.
        self.enqueue_embedding(doc.id)
        return doc

    # ---- link operations ------------------------------------------------------

    def link(self, from_id: str, to_id: str, rel: str = "relates-to") -> Link:
        """Create a typed link between two documents.

        Raises NotFoundError if either document doesn't exist.
        Raises ValidationError if rel is empty or invalid.
        Returns the created (or existing) Link.
        """
        rel = (rel or "").strip()
        if not rel:
            raise ValidationError("rel must be non-empty")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", rel):
            raise ValidationError(f"rel must match ^[A-Za-z0-9][A-Za-z0-9_-]*$ (got {rel!r})")
        # Check both documents exist and resolve aliases
        from_doc = self.get(from_id)
        to_doc = self.get(to_id)
        real_from_id = from_doc.id
        real_to_id = to_doc.id

        now = datetime.now(timezone.utc)
        with self._txn() as cur:
            cur.execute(
                """
                INSERT INTO links (from_id, to_id, rel, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (from_id, to_id, rel) DO NOTHING
                """,
                (real_from_id, real_to_id, rel, now.isoformat()),
            )
            # Retrieve the link (either just inserted or existing)
            row = cur.execute(
                "SELECT * FROM links WHERE from_id=? AND to_id=? AND rel=?",
                (real_from_id, real_to_id, rel),
            ).fetchone()
            self._record_audit(
                cur,
                entity_type="link",
                entity_id=f"{from_id}|{to_id}|{rel}",
                action="create",
                detail={"from_id": from_id, "to_id": to_id, "rel": rel},
            )

        assert row is not None
        return Link(**row)

    def unlink(self, from_id: str, to_id: str, rel: str | None = None) -> int:
        """Remove a link between two documents.

        If rel is specified, only remove links with that relation type.
        Returns the number of removed links.
        """
        with self._txn() as cur:
            if rel:
                cur.execute(
                    "DELETE FROM links WHERE from_id = ? AND to_id = ? AND rel = ?",
                    (from_id, to_id, rel),
                )
            else:
                cur.execute(
                    "DELETE FROM links WHERE from_id = ? AND to_id = ?",
                    (from_id, to_id),
                )
            removed = cur.rowcount
            if removed:
                self._record_audit(
                    cur,
                    entity_type="link",
                    entity_id=f"{from_id}|{to_id}|{rel or '*'}",
                    action="delete",
                    detail={"from_id": from_id, "to_id": to_id, "rel": rel},
                )

        return removed

    def outgoing_links(self, doc_id: str) -> builtins.list[Link]:
        """Get all outgoing links from a document."""
        rows = self._conn.execute(
            "SELECT * FROM links WHERE from_id = ? ORDER BY created_at DESC",
            (doc_id,),
        ).fetchall()
        return [Link(**row) for row in rows]

    def incoming_links(self, doc_id: str) -> builtins.list[Link]:
        """Get all incoming links to a document."""
        rows = self._conn.execute(
            "SELECT * FROM links WHERE to_id = ? ORDER BY created_at DESC",
            (doc_id,),
        ).fetchall()
        return [Link(**row) for row in rows]

    # ---- compatibility aliases (backlinks / outlinks) -------------------------

    def backlinks(self, doc_id: str) -> builtins.list[Link]:
        """Alias for :meth:`incoming_links`."""
        return self.incoming_links(doc_id)

    def outlinks(self, doc_id: str) -> builtins.list[Link]:
        """Alias for :meth:`outgoing_links`."""
        return self.outgoing_links(doc_id)

    # ---- auto-link (v0.8 特性 #4) -------------------------------------------

    def _sync_body_references(self, doc_id: str, body: str) -> None:
        """Parse body for document references and sync as ``references`` links.

        Called automatically from :meth:`add` and :meth:`update` (when body
        changes).  Silently skipped if auto_link is disabled via config.
        """
        auto_link_cfg = self._auto_link_cfg
        if not auto_link_cfg.get("enabled", True):
            return
        rel = auto_link_cfg.get("rel", "references")

        from kb_mcp_lite.link_parser import sync_body_references

        try:
            sync_body_references(self, doc_id, body, rel=rel)
        except Exception:
            logger.debug("auto-link: failed to sync body references for %s", doc_id, exc_info=True)

    # ---- bulk / io ------------------------------------------------------------

    def import_many(self, docs: Iterable[Document]) -> ImportReport:
        """Bulk-import documents. Uses source-based idempotent upsert.

        When a document has a ``source`` field, a matching document
        (same source, not deleted) is updated instead of inserted.
        """
        report = ImportReport()
        for doc in docs:
            try:
                if doc.source:
                    row = self._conn.execute(
                        "SELECT id FROM documents WHERE source = ? AND deleted_at IS NULL",
                        (doc.source,),
                    ).fetchone()
                    if row is not None:
                        self.update(
                            row["id"],
                            **{
                                k: v
                                for k, v in doc.model_dump().items()
                                if k in {"title", "body", "tags", "source", "aliases"}
                            },
                        )
                        # Restore the original updated_at from the imported doc;
                        # update() unconditionally bumps it to now(), which would
                        # make every re-imported doc appear as pending-modify in
                        # vault status / pending_export.
                        with self._txn() as cur:
                            cur.execute(
                                "UPDATE documents SET updated_at = ? WHERE id = ?",
                                (doc.updated_at.isoformat(), doc.id),
                            )
                        report.updated += 1
                        continue
                self.add(doc)
                report.inserted += 1
            except (DuplicateError, ValidationError) as e:
                report.errors.append(f"{doc.id or doc.title}: {e}")
                report.skipped += 1
        return report

    def export_all(self, include_deleted: bool = False) -> builtins.list[Document]:
        """Export all documents, optionally including soft-deleted ones."""
        sql = "SELECT * FROM documents"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY id"
        rows = self._conn.execute(sql).fetchall()
        return [self._row_to_doc(r) for r in rows]


# Import make_id at the end to avoid circular import
from kb_mcp_lite.schema import make_id  # noqa: E402

__all__ = ["SqliteStore"]
