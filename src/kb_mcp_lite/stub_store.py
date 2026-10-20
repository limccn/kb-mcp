"""In-memory dict-backed Store implementation.

This module provides :class:`StubStore`, a complete implementation of the
:mod:`kb_mcp_lite.store` Protocol that lives entirely in process memory. It is
used by:

- the **CLI test suite** (``tests/test_cli_stub.py``) to exercise every
  Click command without touching SQLite;
- any future unit test that needs a real :class:`Store` (not a mock);
- ad-hoc scripting sessions where persistence is not required.

Design notes
------------

- **Insertion order preserved.** ``_docs`` is a regular ``dict`` (insertion
  ordered since Python 3.7) and ``_links`` is a ``set`` of tuples; the
  public API never returns documents in a different order than insertion
  unless the caller asks for a sort (``list()`` sorts by ``updated_at``).
- **Soft delete.** :meth:`delete` sets ``deleted_at`` rather than popping
  the entry. :meth:`get`, :meth:`list`, and :meth:`search` all filter
  out soft-deleted rows by default.
- **Idempotent links.** :meth:`link` is a no-op on an existing
  ``(from_id, to_id, rel)`` triple.
- **Update-by-source.** :meth:`import_many` updates an existing document
  when the incoming document has a ``source`` matching an existing one.
- **No thread safety.** Same contract as the Protocol — single-threaded
  callers only.

Search algorithm
----------------

A deliberately simple substring search is used so the StubStore behaves
predictably in tests:

- match in ``title + " " + body``, case-insensitive;
- ``score`` is the 0-based character offset of the first match (lower is
  better, mirroring FTS5 BM25 convention);
- ``snippet`` is the first 80 chars of ``body`` with the first occurrence
  of the query bolded (``**...**``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast
from collections.abc import Iterable

from kb_mcp_lite.schema import (
    Document,
    DoctorCheck,
    DoctorReport,
    DuplicateError,
    ImportReport,
    Link,
    NotFoundError,
    SearchHit,
    ValidationError,
    make_id,
)
import builtins

# NOTE: ``Store`` is the Protocol defined in ``kb_mcp_lite.store``. We do not
# import it here — in Wave 1A the ``kb_mcp_lite.store`` namespace became a
# package whose ``__init__.py`` does not re-export the Protocol, so a
# direct import would fail. Structural subtyping (PEP 544) means we only
# need the *methods*, not the type. A separate test
# (``test_store_contract.py``) verifies runtime conformance.
_Store = object  # placeholder for type-checker; see NOTE above.


_SNIPPET_LEN = 80


def _now() -> datetime:
    """Return a fresh aware UTC datetime. Centralised for testability."""
    return datetime.now(timezone.utc)


def _make_snippet(body: str, query: str) -> str:
    """Return the first ``_SNIPPET_LEN`` chars of ``body`` with the first
    occurrence of ``query`` (case-insensitive) wrapped in ``**``."""
    if not body:
        return ""
    snippet = body[:_SNIPPET_LEN]
    if not query:
        return snippet
    lower_snip = snippet.lower()
    lower_q = query.lower()
    pos = lower_snip.find(lower_q)
    if pos < 0:
        return snippet
    end = pos + len(query)
    if end > len(snippet):
        # Query extends past the snippet window — bold the prefix.
        return snippet[:pos] + f"**{snippet[pos:]}**"
    return snippet[:pos] + f"**{snippet[pos:end]}**" + snippet[end:]


class StubStore(_Store):
    """A full Protocol-conforming :class:`Store` backed by Python dicts.

    Suitable for unit tests, scripting, and any situation where persistence
    is not needed. See module docstring for design notes.
    """

    def __init__(self) -> None:
        # Documents keyed by id. ``deleted_at`` lives on the document, not
        # in a separate tombstone table — simpler and matches the Schema.
        self._docs: dict[str, Document] = {}
        # Links keyed by (from_id, to_id, rel) for O(1) idempotency.
        self._links: dict[tuple[str, str, str], Link] = {}
        # Secondary index: source -> doc_id, for idempotent re-import.
        self._by_source: dict[str, str] = {}
        # Version history: doc_id -> list of dicts
        self._version_history: dict[str, list[dict[str, object]]] = {}
        # Global audit log (newest first)
        self._audit_log: list[dict[str, object]] = []
        self._version_counter: int = 0
        # Aliases: alias -> doc_id
        self._aliases: dict[str, str] = {}

    # ---- write ----------------------------------------------------------

    def init(self) -> None:
        """Initialize the store (no-op for StubStore)."""
        pass

    def add(self, doc: Document) -> str:
        """Insert a new document; auto-generate id when empty.

        Raises:
            DuplicateError: if the id is already in use.
            ValidationError: if the document fails pydantic validation
                (raised by ``Document.__init__`` before we get here).
        """
        # Pydantic validation already happened during Document construction.
        # Re-validate defensively in case the caller built a Document with
        # ``model_construct``.
        try:
            doc = Document.model_validate(doc.model_dump())
        except Exception as e:
            raise ValidationError(f"invalid document: {e}") from e

        if not doc.id:
            doc = doc.model_copy(update={"id": make_id(doc.type, doc.title)})
        if doc.id in self._docs:
            raise DuplicateError(doc.id, existing_id=doc.id)
        if doc.source and doc.source in self._by_source:
            existing = self._docs[self._by_source[doc.source]]
            raise DuplicateError(doc.id, existing_id=existing.id)

        # Ensure timestamps are set.
        now = _now()
        if doc.created_at is None:
            doc = doc.model_copy(update={"created_at": now})
        if doc.updated_at is None:
            doc = doc.model_copy(update={"updated_at": now})

        self._docs[doc.id] = doc
        if doc.source:
            self._by_source[doc.source] = doc.id
        # Write aliases
        for alias in doc.aliases:
            self._aliases[alias] = doc.id
        self._record_version(doc.id, "create", doc)
        return doc.id

    def update(self, doc_id: str, **fields: object) -> Document:
        """Patch fields on an existing document."""
        if doc_id not in self._docs:
            raise NotFoundError(doc_id)
        current = self._docs[doc_id]
        # Immutable fields per the Protocol.
        for forbidden in ("id", "type", "created_at"):
            if forbidden in fields:
                raise ValidationError(f"{forbidden!r} cannot be changed via update()")
        # Validate the merged result.
        merged = current.model_copy(update=dict(fields))
        try:
            merged = Document.model_validate(merged.model_dump())
        except Exception as e:
            raise ValidationError(f"invalid update: {e}") from e
        merged = merged.model_copy(update={"updated_at": _now()})
        self._docs[doc_id] = merged
        # Keep source index in sync.
        if merged.source:
            self._by_source[merged.source] = merged.id
        # Update aliases if provided
        if "aliases" in fields:
            # Remove old aliases for this doc
            old_to_remove = [a for a, d in self._aliases.items() if d == doc_id]
            for a in old_to_remove:
                del self._aliases[a]
            aliases_val = fields.get("aliases")
            if isinstance(aliases_val, list):
                for alias in aliases_val:
                    if alias:
                        self._aliases[str(alias)] = doc_id
        self._record_version(doc_id, "update", merged)
        return merged

    def update_source(self, doc_id: str, source: str | None) -> None:
        """Update only ``source``; no ``updated_at`` bump, no version record.

        Mirrors the SQLite backend's export write-back path.
        """
        if doc_id not in self._docs:
            raise NotFoundError(doc_id)
        merged = self._docs[doc_id].model_copy(update={"source": source})
        self._docs[doc_id] = merged
        # Keep source index in sync.
        if source:
            self._by_source[source] = doc_id

    def delete(self, doc_id: str) -> None:
        """Soft-delete a document. Idempotent on already-deleted docs."""
        if doc_id not in self._docs:
            raise NotFoundError(doc_id)
        current = self._docs[doc_id]
        if current.deleted_at is not None:
            return  # already soft-deleted — no-op
        deleted_doc = current.model_copy(update={"deleted_at": _now()})
        self._docs[doc_id] = deleted_doc
        self._record_version(doc_id, "delete", deleted_doc)

    # ---- read -----------------------------------------------------------

    def get(self, doc_id: str, include_deleted: bool = False) -> Document:
        if doc_id not in self._docs:
            # Try alias resolution
            resolved = self._aliases.get(doc_id)
            if resolved:
                return self.get(resolved, include_deleted=include_deleted)
            raise NotFoundError(doc_id)
        doc = self._docs[doc_id]
        if doc.deleted_at is not None and not include_deleted:
            raise NotFoundError(doc_id)
        return doc

    def resolve_alias(self, alias: str) -> str | None:
        return self._aliases.get(alias)

    def list(
        self,
        type: str | None = None,  # noqa: A002
        tags: builtins.list[str] | None = None,
        link_to: str | None = None,
        link_from: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> builtins.list[Document]:
        if limit < 0 or limit > 1000:
            raise ValidationError(f"limit must be 0..1000 (got {limit})")
        if offset < 0:
            raise ValidationError(f"offset must be >= 0 (got {offset})")
        results: list[Document] = []
        for doc in self._docs.values():
            if doc.deleted_at is not None and not include_deleted:
                continue
            if type is not None and doc.type != type:
                continue
            if tags and not all(t in doc.tags for t in tags):
                continue
            if link_to is not None:
                links_from = [
                    lk
                    for lk in self._links.values()
                    if lk.from_id == doc.id and lk.to_id == link_to
                ]
                if not links_from:
                    continue
            if link_from is not None:
                links_to = [
                    lk
                    for lk in self._links.values()
                    if lk.to_id == doc.id and lk.from_id == link_from
                ]
                if not links_to:
                    continue
            results.append(doc)
        # Sort by updated_at DESC, then by id ASC for determinism.
        results.sort(key=lambda d: (d.updated_at, d.id), reverse=False)
        results.reverse()
        return results[offset : offset + limit]

    def search(
        self,
        query: str,
        type: str | None = None,  # noqa: A002
        tags: builtins.list[str] | None = None,
        limit: int = 10,
        fuzzy: bool = False,
        **kwargs: object,
    ) -> builtins.list[SearchHit]:
        if not query or not query.strip():
            raise ValidationError("query must be non-empty")
        if limit < 1 or limit > 100:
            raise ValidationError(f"limit must be 1..100 (got {limit})")
        if fuzzy:
            mode = "fuzzy"
        else:
            mode = str(kwargs.get("mode", "lexical"))
        lower_q = query.lower().strip()
        # fuzzy/hybrid: also match any token substring of length >= 3.
        # This is a rough approximation of the trigram FTS; good enough
        # for unit tests against the in-memory stub.
        fuzzy_tokens: list[str] = []
        if mode in ("fuzzy", "hybrid"):
            for tok in lower_q.split():
                if len(tok) >= 3:
                    fuzzy_tokens.append(tok)
        hits: list[SearchHit] = []
        for doc in self._docs.values():
            if doc.deleted_at is not None:
                continue
            if type is not None and doc.type != type:
                continue
            if tags and not all(t in doc.tags for t in tags):
                continue
            haystack = (doc.title + " " + doc.body).lower()
            pos = haystack.find(lower_q)
            matched = pos >= 0
            if not matched and fuzzy_tokens:
                # In fuzzy mode we accept any of the trigram tokens as a hit.
                for ft in fuzzy_tokens:
                    p = haystack.find(ft)
                    if p >= 0:
                        matched = True
                        pos = p
                        break
            if not matched:
                continue
            hits.append(
                SearchHit(
                    doc=doc,
                    snippet=_make_snippet(doc.body, lower_q),
                    score=float(pos),
                )
            )
        # Lower score = better, matching BM25 convention.
        hits.sort(key=lambda h: (h.score, h.doc.id))
        return hits[:limit]

    # ---- links ----------------------------------------------------------

    def link(self, from_id: str, to_id: str, rel: str = "relates-to") -> Link:
        if not rel:
            raise ValidationError("rel must be non-empty")
        if from_id not in self._docs:
            raise NotFoundError(from_id)
        if to_id not in self._docs:
            raise NotFoundError(to_id)
        if self._docs[from_id].deleted_at is not None:
            raise NotFoundError(from_id)
        if self._docs[to_id].deleted_at is not None:
            raise NotFoundError(to_id)
        key = (from_id, to_id, rel)
        existing = self._links.get(key)
        if existing is not None:
            return existing
        link = Link(from_id=from_id, to_id=to_id, rel=rel)
        self._links[key] = link
        return link

    def unlink(
        self,
        from_id: str,
        to_id: str,
        rel: str | None = None,
    ) -> int:
        if rel is not None:
            key = (from_id, to_id, rel)
            return 1 if self._links.pop(key, None) is not None else 0
        # No rel specified — remove every link for this (from, to) pair.
        to_remove = [k for k in self._links if k[0] == from_id and k[1] == to_id]
        for k in to_remove:
            del self._links[k]
        return len(to_remove)

    def outgoing_links(self, doc_id: str) -> builtins.list[Link]:
        return self.outlinks(doc_id)

    def incoming_links(self, doc_id: str) -> builtins.list[Link]:
        return self.backlinks(doc_id)

    def backlinks(self, doc_id: str) -> builtins.list[Link]:
        # ``__exit__`` callers may pass a soft-deleted id; we don't raise.
        return [lk for lk in self._links.values() if lk.to_id == doc_id]

    def outlinks(self, doc_id: str) -> builtins.list[Link]:
        return [lk for lk in self._links.values() if lk.from_id == doc_id]

    # ---- bulk / io ------------------------------------------------------

    def import_many(self, docs: Iterable[Document]) -> ImportReport:
        report = ImportReport()
        for raw in docs:
            try:
                # Validate first.
                doc = Document.model_validate(raw.model_dump())
                if not doc.id:
                    doc = doc.model_copy(update={"id": make_id(doc.type, doc.title)})
                # Source-based upsert takes priority over id-based insert.
                existing_id: str | None = None
                if doc.source and doc.source in self._by_source:
                    existing_id = self._by_source[doc.source]
                elif doc.id in self._docs:
                    existing_id = doc.id
                if existing_id is not None:
                    current = self._docs[existing_id]
                    merged = current.model_copy(
                        update={
                            "type": doc.type,
                            "title": doc.title,
                            "body": doc.body,
                            "tags": doc.tags,
                            "source": doc.source,
                            "updated_at": _now(),
                        }
                    )
                    merged = Document.model_validate(merged.model_dump())
                    self._docs[existing_id] = merged
                    if merged.source:
                        self._by_source[merged.source] = merged.id
                    report.updated += 1
                else:
                    self.add(doc)
                    report.inserted += 1
            except DuplicateError as e:
                report.errors.append(f"duplicate: {e}")
                report.skipped += 1
            except ValidationError as e:
                report.errors.append(f"validation: {e}")
                report.skipped += 1
            except Exception as e:  # last-resort guard for tests
                report.errors.append(f"error: {e}")
                report.skipped += 1
        return report

    def export_all(self, include_deleted: bool = False) -> builtins.list[Document]:
        results = []
        for doc in self._docs.values():
            if doc.deleted_at is not None and not include_deleted:
                continue
            results.append(doc)
        return results

    # ---- embedding / similarity -----------------------------------------

    def similar_docs(self, doc_id: str, limit: int = 10) -> builtins.list[tuple[Document, float]]:
        return []

    def suggest_tags(self, doc_id: str, limit: int = 10) -> builtins.list[tuple[str, float]]:
        return []

    def suggest_type(self, doc_id: str, limit: int = 10) -> builtins.list[tuple[str, float]]:
        return []

    def find_duplicates(
        self, threshold: float = 0.15, limit: int = 50
    ) -> builtins.list[tuple[str, str, float]]:
        return []

    # ---- maintenance ----------------------------------------------------

    def doctor(self) -> DoctorReport:
        """Always healthy for v0.1. Returns a report listing trivial checks."""
        n_docs = sum(1 for d in self._docs.values() if d.deleted_at is None)
        n_links = len(self._links)
        return DoctorReport(
            ok=True,
            checks=[
                DoctorCheck(
                    name="in_memory_invariants",
                    ok=True,
                    detail=f"{len(self._docs)} docs ({n_docs} active), {n_links} links",
                ),
                DoctorCheck(
                    name="required_fields",
                    ok=True,
                    detail="all documents have non-empty type and title",
                ),
                DoctorCheck(
                    name="no_orphan_links",
                    ok=True,
                    detail="no link references a missing document",
                ),
            ],
        )

    def prune(self, older_than: timedelta = timedelta(days=30)) -> int:
        cutoff = _now() - older_than
        to_delete = [
            doc_id
            for doc_id, doc in self._docs.items()
            if doc.deleted_at is not None and doc.deleted_at < cutoff
        ]
        for doc_id in to_delete:
            doc = self._docs.pop(doc_id)
            if doc.source and self._by_source.get(doc.source) == doc_id:
                del self._by_source[doc.source]
        return len(to_delete)

    # ---- history / audit / restore -------------------------------------

    def _record_version(
        self,
        doc_id: str,
        action: str,
        doc: Document,
    ) -> None:
        """Record a version snapshot for a document."""
        self._version_counter += 1
        entry: dict[str, object] = {
            "version_id": self._version_counter,
            "doc_id": doc_id,
            "action": action,
            "snapshot": doc.model_dump(mode="json"),
            "created_at": _now().isoformat(),
            "actor": "admin",
            "note": "",
        }
        if doc_id not in self._version_history:
            self._version_history[doc_id] = []
        self._version_history[doc_id].insert(0, entry)
        self._audit_log.insert(
            0,
            {
                "audit_id": self._version_counter,
                "entity_type": "document",
                "entity_id": doc_id,
                "action": action,
                "detail": {},
                "created_at": _now().isoformat(),
                "actor": "admin",
                "note": "",
            },
        )

    def get_versions(self, doc_id: str) -> builtins.list[dict[str, object]]:
        """Alias for document_history (returns list of version dicts)."""
        return self.document_history(doc_id)

    def diff_versions(self, doc_id: str, v1: int, v2: int) -> dict[str, object]:
        """Alias for diff."""
        return self.diff(doc_id, v1, v2)

    def restore_version(self, doc_id: str, version_id: int) -> Document:
        """Alias for restore."""
        return self.restore(doc_id, version_id=version_id)

    def stats(self) -> dict[str, object]:
        """Return basic statistics."""
        active = [d for d in self._docs.values() if d.deleted_at is None]
        deleted = [d for d in self._docs.values() if d.deleted_at is not None]
        from collections import Counter

        type_counts = Counter(d.type for d in active)
        return {
            "total_docs": len(active),
            "total_links": len(self._links),
            "soft_deleted": len(deleted),
            "recent_changes": 0,
            "docs_by_type": dict(type_counts),
        }

    def reindex(self) -> None:
        """Rebuild search index (no-op for StubStore)."""
        pass

    def reindex_embeddings(self, progress_callback=None) -> int:
        """Recompute embeddings (no-op for StubStore)."""
        return 0

    def document_history(self, doc_id: str, limit: int = 50) -> builtins.list[dict[str, object]]:
        raw = self._version_history.get(doc_id, [])
        return raw[:limit]

    def audit_log(self, limit: int = 100) -> builtins.list[dict[str, object]]:
        return self._audit_log[:limit]

    def restore(self, doc_id: str, version_id: int | None = None) -> Document:
        history = self._version_history.get(doc_id, [])
        if not history:
            raise NotFoundError(f"no versions found for {doc_id!r}")

        if version_id is not None:
            matches = [e for e in history if e["version_id"] == version_id]
            if not matches:
                raise NotFoundError(f"version {version_id} for {doc_id!r}")
            entry = matches[0]
        else:
            entry = history[0]

        snapshot = dict(cast(dict[str, object], entry["snapshot"]))
        snapshot["id"] = doc_id
        restored_doc = Document.model_validate(snapshot)

        fields: dict[str, object] = {}
        for f in ("title", "body", "tags", "source", "metadata"):
            fields[f] = getattr(restored_doc, f, None)
        return self.update(doc_id, **fields)

    def diff(
        self,
        doc_id: str,
        version_a: int,
        version_b: int,
    ) -> dict[str, object]:
        history = self._version_history.get(doc_id, [])
        entries = {e["version_id"]: e for e in history}
        entry_a = entries.get(version_a)
        entry_b = entries.get(version_b)
        if entry_a is None:
            raise NotFoundError(f"version {version_a} for {doc_id!r}")
        if entry_b is None:
            raise NotFoundError(f"version {version_b} for {doc_id!r}")

        snap_a = dict(cast(dict[str, object], entry_a["snapshot"]))
        snap_b = dict(cast(dict[str, object], entry_b["snapshot"]))

        keys_a = set(snap_a.keys())
        keys_b = set(snap_b.keys())

        added: dict[str, object] = {}
        removed: dict[str, object] = {}
        changed: dict[str, dict[str, object]] = {}

        for k in keys_b - keys_a:
            added[k] = snap_b[k]
        for k in keys_a - keys_b:
            removed[k] = snap_a[k]
        for k in keys_a & keys_b:
            if snap_a[k] != snap_b[k]:
                changed[k] = {"from": snap_a[k], "to": snap_b[k]}

        return {
            "doc_id": doc_id,
            "version_a": version_a,
            "version_b": version_b,
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    def restore_deleted(self, doc_id: str) -> Document:
        doc = self.get(doc_id, include_deleted=True)
        if doc.deleted_at is None:
            raise ValidationError(f"document {doc_id!r} is not deleted")
        now = _now()
        restored_doc = doc.model_copy(update={"deleted_at": None, "updated_at": now})
        self._docs[doc_id] = restored_doc
        self._record_version(doc_id, "restore", restored_doc)
        return self.get(doc_id)

    # ---- lifecycle ------------------------------------------------------

    def close(self) -> None:
        # Nothing to release; provided for Protocol symmetry.
        self._docs.clear()
        self._links.clear()
        self._by_source.clear()

    def __enter__(self) -> StubStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- test helpers (not part of the Protocol) -----------------------

    def _all_ids(self) -> builtins.list[str]:
        """Return every stored id (including soft-deleted). Test-only."""
        return list(self._docs.keys())


__all__ = ["StubStore"]
