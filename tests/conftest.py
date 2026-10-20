"""Shared test fixtures / helpers for kb-mcp.

Currently holds one helper: :class:`SyncEmbedStore`, a test-only
:class:`~kb_mcp_lite.store.sqlite.SqliteStore` subclass used by the
semantic / similarity ranking suites (``test_search_semantic.py``,
``test_similarity.py``).
"""

from __future__ import annotations

from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


class SyncEmbedStore(SqliteStore):
    """``SqliteStore`` that drains the embedding queue after each write.

    Since v0.8, ``add``/``update``/``restore_deleted`` only *enqueue*
    a document for the background worker — the vec0 row appears once a
    worker (or ``process_embedding_queue``) picks the job up. The
    semantic-search and similarity suites assert on *ranking right
    after seeding*, so those tests need write-through embedding: every
    mutation embeds synchronously via :meth:`process_embedding_queue`.

    This is intentionally a test-only shim. It does **not** weaken the
    async coverage (``test_worker.py`` / ``test_embedding_queue.py``
    exercise the queue/worker directly); it just keeps ranking tests
    focused on ranking instead of on queue timing. The embedder used in
    those tests is a fast in-process mock, so the drain is instant.
    """

    def add(self, doc: Document) -> str:
        doc_id = super().add(doc)
        self.process_embedding_queue()
        return doc_id

    def update(self, doc_id: str, **kwargs: object) -> Document:
        doc = super().update(doc_id, **kwargs)
        self.process_embedding_queue()
        return doc

    def restore_deleted(self, doc_id: str) -> Document:
        doc = super().restore_deleted(doc_id)
        self.process_embedding_queue()
        return doc
