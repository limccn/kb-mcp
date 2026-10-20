"""Store Protocol — the interface every storage backend must implement.

This module defines the **contract** only. The concrete SQLite
implementation lives in :mod:`kb_mcp_lite.store.sqlite`. Tests against this
Protocol are in ``tests/test_store_contract.py``.

Why a Protocol
--------------

The Store is the single integration point between kb-mcp and its data
layer. By depending on a Protocol (PEP 544 structural subtyping) instead
of a concrete class:

- the CLI, MCP server, and Markdown I/O modules can be developed and
  unit-tested against a ``StubStore`` (in-memory dict) without touching
  SQLite;
- alternative backends (Postgres, in-memory, HTTP) can be added later by
  implementing the same Protocol;
- the E2E tests in Wave 2 swap implementations to exercise both paths.

Concurrency
------------

The Protocol does **not** promise thread safety. v0.1 uses SQLite in WAL
mode with one connection per process; multi-process access (e.g. two
``kb`` CLI invocations against the same DB) is supported because SQLite
WAL serialises writers, but a single process must serialise its own
calls. The MCP server and CLI are both single-threaded; this is not a
concern for v0.1.

Soft delete
-----------

``delete()`` sets ``deleted_at`` rather than removing the row. ``get()``,
``list()``, ``search()`` all filter out soft-deleted rows. ``prune()``
hard-deletes soft-deleted rows older than a configurable grace period.
This makes ``kb import`` idempotent and recoverable.

Failure modes
-------------

Every method either:

- returns the documented value (no exception on "not found" for
  ``list`` / ``search``);
- raises one of the exceptions from :mod:`kb_mcp_lite.schema` (``NotFoundError``,
  ``DuplicateError``, ``ValidationError``, ``IntegrityError``).

Callers MUST handle these. Callers MUST NOT catch the generic
``Exception`` to keep error semantics intact.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol, runtime_checkable
from collections.abc import Iterable

from kb_mcp_lite.schema import (
    Document,
    DoctorReport,
    ImportReport,
    Link,
    SearchHit,
)


@runtime_checkable
class Store(Protocol):
    """The storage interface every backend must implement.

    See module docstring for thread-safety and soft-delete semantics.
    """

    # ---- write ----------------------------------------------------------

    def add(self, doc: Document) -> str:
        """Insert a new document. Returns the stored id.

        Implementations MAY auto-generate the id when ``doc.id`` is empty;
        the canonical generator is :func:`kb_mcp_lite.schema.make_id`.

        Raises:
            DuplicateError: if a document with the same id (or, where the
                backend enforces it, same ``(type, title)``) already
                exists.
            ValidationError: if any field fails pydantic validation.
        """
        ...

    def update(self, doc_id: str, **fields: object) -> Document:
        """Patch fields on an existing document and return the updated
        instance.

        ``updated_at`` is set to the current UTC time. ``id``, ``type``,
        ``created_at`` cannot be changed via this method.

        Raises:
            NotFoundError: if ``doc_id`` does not exist.
            ValidationError: if any new field value fails validation.
        """
        ...

    def update_source(self, doc_id: str, source: str | None) -> None:
        """Update only the ``source`` field, without touching ``updated_at``,
        version history, or the audit log.

        This is the export write-back path: ``export_dir`` records where each
        document was written so a later import matches by source. Using
        :meth:`update` for that would bump ``updated_at`` after the file was
        written, leaving every document newer than its export and defeating
        incremental export.

        Raises:
            NotFoundError: if ``doc_id`` does not exist.
        """
        ...

    def delete(self, doc_id: str) -> None:
        """Soft-delete a document. Sets ``deleted_at``.

        Idempotent: deleting an already-deleted document is a no-op.

        Raises:
            NotFoundError: if ``doc_id`` does not exist (active or deleted).
        """
        ...

    # ---- read -----------------------------------------------------------

    def get(self, doc_id: str, include_deleted: bool = False) -> Document:
        """Fetch a single document by id.

        Raises:
            NotFoundError: if the id is unknown.
        """
        ...

    def list(
        self,
        type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> list[Document]:
        """List documents, sorted by ``updated_at DESC``.

        ``tags`` filters to documents carrying **all** listed tags (AND).

        ``limit`` is capped at 1000 to keep callers honest. ``offset`` is
        for pagination; callers needing more should switch to
        :meth:`search`.
        """
        ...

    def search(
        self,
        query: str,
        type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Full-text search via the backend's FTS / equivalent engine.

        Returns ranked results with snippets. Empty query returns ``[]``.

        ``limit`` is capped at 100.

        Raises:
            ValidationError: if ``query`` is empty after stripping or
                ``limit`` is out of range.
        """
        ...

    # ---- links ----------------------------------------------------------

    def link(self, from_id: str, to_id: str, rel: str = "relates-to") -> Link:
        """Create a typed edge. Idempotent on ``(from_id, to_id, rel)``.

        Returns the canonical :class:`Link` (with ``created_at`` populated).

        Raises:
            NotFoundError: if either endpoint id does not exist.
            ValidationError: if ``rel`` is empty or contains illegal chars.
        """
        ...

    def unlink(
        self,
        from_id: str,
        to_id: str,
        rel: str | None = None,
    ) -> int:
        """Remove edges. If ``rel`` is None, all edges from
        ``(from_id, to_id)`` are removed. Returns count removed."""
        ...

    def backlinks(self, doc_id: str) -> list[Link]:
        """Return all edges pointing **to** ``doc_id`` (i.e. where
        ``to_id == doc_id``)."""
        ...

    def outlinks(self, doc_id: str) -> list[Link]:
        """Return all edges originating from ``doc_id``."""
        ...

    # ---- bulk / io ------------------------------------------------------

    def import_many(self, docs: Iterable[Document]) -> ImportReport:
        """Bulk insert/update. Backends SHOULD batch this in a single
        transaction.

        Update-by-source semantics: if a doc has ``source`` set and a
        document with that source already exists, it is updated rather
        than inserted (idempotent re-import).
        """
        ...

    def export_all(self, include_deleted: bool = False) -> list[Document]:
        """Return every document. Used by ``kb export``."""
        ...

    # ---- embedding / similarity -----------------------------------------

    def similar_docs(self, doc_id: str, limit: int = 10) -> list[tuple[Document, float]]:
        """Return documents most similar to ``doc_id`` by embedding
        cosine distance, sorted nearest-first.

        Each tuple is ``(document, cosine_distance)`` where 0.0 = identical.

        Raises:
            ValidationError: similarity is unavailable — the embedder is
                disabled, the vec0 extension is missing, or the doc has no
                stored embedding. An empty list is a legitimate answer
                ("no other similar documents"); these causes must not be
                mistaken for it.
        """
        ...

    def suggest_tags(self, doc_id: str, limit: int = 10) -> list[tuple[str, float]]:
        """Suggest tags for ``doc_id`` based on similar documents' tags.

        Returns ``[(tag, weight), ...]`` sorted by descending weight.
        Weight is the sum of (1 - distance) contributions from each
        similar document that carries the tag.
        """
        ...

    def suggest_type(self, doc_id: str, limit: int = 10) -> list[tuple[str, float]]:
        """Suggest a document type for ``doc_id`` based on similar docs.

        Returns ``[(type, weight), ...]`` sorted by descending weight,
        where weight is the count of similar docs with that type
        modulated by similarity (1 - distance).
        """
        ...

    def find_duplicates(
        self, threshold: float = 0.15, limit: int = 50
    ) -> list[tuple[str, str, float]]:
        """Scan all documents and find near-duplicate pairs.

        ``threshold`` is the cosine-distance cutoff (0.0 = identical,
        lower = more similar). Returns ``[(id_a, id_b, distance), ...]``
        sorted by distance ascending, limited to ``limit`` pairs.

        Raises:
            ValidationError: duplicate detection is unavailable — the
                embedder is disabled, the vec0 extension is missing, or no
                active document has a stored embedding. ``[]`` means
                "vectors exist, but no pair is within ``threshold``".
        """
        ...

    # ---- maintenance ----------------------------------------------------

    def doctor(self) -> DoctorReport:
        """Run health checks. Always returns a report; never raises.

        Checks include: PRAGMA integrity_check, FTS row count == documents
        row count, no orphan links, all docs have non-empty type/title."""
        ...

    def prune(self, older_than: timedelta = timedelta(days=30)) -> int:
        """Hard-delete soft-deleted documents whose ``deleted_at`` is
        older than the cutoff. Returns count removed."""
        ...

    # ---- history / audit ------------------------------------------------

    def document_history(self, doc_id: str, limit: int = 50) -> list[dict[str, object]]:
        """Return the version history of a document.

        Each entry contains: ``version_id``, ``doc_id``, ``action``
        (create/update/delete), ``snapshot`` (full JSON of the document
        at that point), ``created_at``, ``actor``, ``note``.

        Returns empty list if the document has never been recorded
        (e.g. imported before migration 0004).
        """
        ...

    def audit_log(self, limit: int = 100) -> list[dict[str, object]]:
        """Return the global audit log, newest first.

        Each entry contains: ``audit_id``, ``entity_type``, ``entity_id``,
        ``action``, ``detail``, ``created_at``, ``actor``, ``note``.
        """
        ...

    def restore(self, doc_id: str, version_id: int | None = None) -> Document:
        """Restore a document to a previous version.

        If ``version_id`` is None, restores to the most recent version.
        Creates a new version snapshot before applying the restore.

        Raises:
            NotFoundError: if ``doc_id`` or ``version_id`` does not exist.
            ValidationError: if the snapshot fails validation.
        """
        ...

    def diff(
        self,
        doc_id: str,
        version_a: int,
        version_b: int,
    ) -> dict[str, object]:
        """Compare two document versions and return field-level differences.

        Returns a dict with keys: ``added`` (fields in B not in A),
        ``removed`` (fields in A not in B), ``changed`` (dict of
        ``{field: {"from": ..., "to": ...}}``).

        Raises:
            NotFoundError: if either version does not exist.
        """
        ...

    def restore_deleted(self, doc_id: str) -> Document:
        """Restore a soft-deleted document by clearing ``deleted_at``.

        Creates a version snapshot recording the restore action.

        Raises:
            NotFoundError: if the document id does not exist at all.
            ValidationError: if the document is not soft-deleted.
        """
        ...

    def resolve_alias(self, alias: str) -> str | None:
        """Resolve an alias to a canonical document id.

        Returns the document id, or ``None`` if the alias does not exist.
        """
        ...

    # ---- lifecycle ------------------------------------------------------

    def close(self) -> None:
        """Release resources. Idempotent. Safe to call on error paths."""
        ...

    def __enter__(self) -> Store: ...
    def __exit__(self, *exc: object) -> None: ...


__all__ = ["Store"]
