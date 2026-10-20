"""Tests for ``SqliteStore``. Real SQLite temp file; no mocks."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kb_mcp_lite import (
    Document,
    DuplicateError,
    Link,
    NotFoundError,
    ValidationError,
)
from kb_mcp_lite.store.sqlite import SqliteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> Generator[SqliteStore, None, None]:
    db = tmp_path / "kb.db"
    s = SqliteStore(db)
    yield s
    s.close()


def _doc(
    id: str = "test/x",
    type: str = "project",
    title: str = "Test",
    body: str = "",
    tags: list[str] | None = None,
    source: str | None = None,
) -> Document:
    """Build a Document with sensible defaults. Uses ``model_construct``
    (no validation) so the test fixture itself never raises — the test
    asserts on whatever validator/store raises downstream."""
    return Document.model_construct(
        id=id,
        type=type,
        title=title,
        body=body,
        tags=tags or [],
        source=source,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        deleted_at=None,
    )


# ---------------------------------------------------------------------------
# Construction / migration
# ---------------------------------------------------------------------------


def test_creates_db_and_schema(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    assert not db.exists()
    s = SqliteStore(db)
    s.close()
    assert db.exists()
    # Verify tables exist via raw connection.
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','trigger','index')"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "documents" in names
    assert "links" in names
    assert "docs_fts" in names
    assert "schema_version" in names
    conn.close()


def test_schema_version_recorded(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    s = SqliteStore(db)
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    # Migration 0003 (vec0) applies when the connection can load sqlite-vec
    # (dev/vec extras install pysqlite3, which carries extension support).
    # Migration 0007 (embedding_queue) is part of v0.8.0 and always applies.
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8]
    conn.close()
    s.close()


def test_reopening_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "kb.db"
    s1 = SqliteStore(db)
    s1.add(_doc(id="proj/x", title="X"))
    s1.close()
    s2 = SqliteStore(db)
    assert s2.get("proj/x").title == "X"
    s2.close()


def test_wal_mode_and_fk_enabled(tmp_path: Path) -> None:
    db = tmp_path / "kb.db"
    s = SqliteStore(db)
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    fk = s._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    s.close()


# ---------------------------------------------------------------------------
# add / get / list / search
# ---------------------------------------------------------------------------


def test_add_and_get(store: SqliteStore) -> None:
    d = _doc(id="proj/kb-mcp", title="kb-mcp", body="hello", tags=["open-source"])
    store.add(d)
    got = store.get("proj/kb-mcp")
    assert got.id == "proj/kb-mcp"
    assert got.title == "kb-mcp"
    assert got.tags == ["open-source"]


def test_document_history_and_audit_log(store: SqliteStore) -> None:
    store.add(_doc(id="proj/history", title="History"))
    store.update("proj/history", body="new body")
    store.delete("proj/history")

    history = store.document_history("proj/history")
    actions = [entry["action"] for entry in history]
    assert actions[:3] == ["delete", "update", "create"]

    audit = store.audit_log(limit=10)
    assert any(
        entry["entity_type"] == "document" and entry["action"] == "create" for entry in audit
    )


def test_add_empty_id_generated(store: SqliteStore) -> None:
    d = _doc(id="", type="project", title="Auto ID")
    new_id = store.add(d)
    assert new_id == "auto-id"
    assert store.get(new_id).title == "Auto ID"


def test_add_duplicate_raises(store: SqliteStore) -> None:
    store.add(_doc(id="proj/x", title="X"))
    with pytest.raises(DuplicateError):
        store.add(_doc(id="proj/x", title="X again"))


def test_add_invalid_id_raises(store: SqliteStore) -> None:
    """Pydantic rejects invalid ids before the store sees them."""
    bad = _doc(id="Has Spaces", title="X")
    with pytest.raises(ValidationError):
        store.add(bad)


def test_get_missing_raises(store: SqliteStore) -> None:
    with pytest.raises(NotFoundError):
        store.get("nope/missing")


def test_list_filtered_by_type(store: SqliteStore) -> None:
    store.add(_doc(id="proj/a", type="project", title="A"))
    store.add(_doc(id="dec/b", type="decision", title="B"))
    store.add(_doc(id="dec/c", type="decision", title="C"))
    decisions = store.list(type="decision")
    assert {d.id for d in decisions} == {"dec/b", "dec/c"}


def test_list_filtered_by_tags(store: SqliteStore) -> None:
    store.add(_doc(id="a", tags=["python", "mcp"]))
    store.add(_doc(id="b", tags=["python"]))
    store.add(_doc(id="c", tags=["rust"]))
    py = store.list(tags=["python"])
    assert {d.id for d in py} == {"a", "b"}
    both = store.list(tags=["python", "mcp"])
    assert {d.id for d in both} == {"a"}


def test_list_limit_offset(store: SqliteStore) -> None:
    for i in range(5):
        store.add(_doc(id=f"d/{i}", title=f"D{i}"))
    page1 = store.list(limit=2, offset=0)
    page2 = store.list(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {d.id for d in page1}.isdisjoint({d.id for d in page2})


def test_search_finds_matches(store: SqliteStore) -> None:
    store.add(_doc(id="a", body="SQLite is a C library"))
    store.add(_doc(id="b", body="Python is high level"))
    store.add(_doc(id="c", body="sqlite-vss adds vector search"))
    hits = store.search("sqlite")
    ids = {h.doc.id for h in hits}
    assert ids == {"a", "c"}


def test_search_empty_query_raises(store: SqliteStore) -> None:
    with pytest.raises(ValidationError):
        store.search("   ")


def test_search_limit_bounds(store: SqliteStore) -> None:
    with pytest.raises(ValidationError):
        store.search("x", limit=0)
    with pytest.raises(ValidationError):
        store.search("x", limit=200)


def test_search_snippet_contains_terms(store: SqliteStore) -> None:
    store.add(_doc(id="a", body="the quick brown fox jumps over the lazy dog"))
    hits = store.search("fox")
    assert len(hits) == 1
    assert "fox" in hits[0].snippet


def test_search_filters_soft_deleted(store: SqliteStore) -> None:
    store.add(_doc(id="a", body="hello world"))
    store.add(_doc(id="b", body="hello again"))
    store.delete("a")
    hits = store.search("hello")
    assert {h.doc.id for h in hits} == {"b"}


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_mutates_fields(store: SqliteStore) -> None:
    store.add(_doc(id="a", title="Old", body="x"))
    store.update("a", title="New", body="y")
    got = store.get("a")
    assert got.title == "New"
    assert got.body == "y"


def test_update_disallows_id(store: SqliteStore) -> None:
    store.add(_doc(id="a", title="X"))
    with pytest.raises(ValidationError):
        store.update("a", id="b")


def test_update_no_fields_raises(store: SqliteStore) -> None:
    store.add(_doc(id="a", title="X"))
    with pytest.raises(ValidationError):
        store.update("a")


def test_update_missing_raises(store: SqliteStore) -> None:
    with pytest.raises(NotFoundError):
        store.update("nope", title="X")


# ---------------------------------------------------------------------------
# update_source (export write-back)
# ---------------------------------------------------------------------------


def test_update_source_sets_source(store: SqliteStore) -> None:
    store.add(_doc(id="a", title="X"))
    store.update_source("a", "a.md")
    assert store.get("a").source == "a.md"


def test_update_source_preserves_updated_at_and_history(store: SqliteStore) -> None:
    """Unlike update(), the export write-back bumps neither updated_at
    nor the version history — otherwise incremental export never skips."""
    store.add(_doc(id="a", title="X"))
    before = store.get("a")
    history_before = store.document_history("a")

    store.update_source("a", "a.md")

    after = store.get("a")
    assert after.updated_at == before.updated_at
    assert store.document_history("a") == history_before


def test_update_source_missing_raises(store: SqliteStore) -> None:
    with pytest.raises(NotFoundError):
        store.update_source("nope", "nope.md")


# ---------------------------------------------------------------------------
# delete / soft delete / prune
# ---------------------------------------------------------------------------


def test_delete_soft(store: SqliteStore) -> None:
    store.add(_doc(id="a", title="X"))
    store.delete("a")
    with pytest.raises(NotFoundError):
        store.get("a")
    # Still on disk
    got = store.get("a", include_deleted=True)
    assert got.deleted_at is not None


def test_delete_idempotent_missing_raises(store: SqliteStore) -> None:
    with pytest.raises(NotFoundError):
        store.delete("nope")


def test_delete_idempotent_already_deleted(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.delete("a")
    # Second delete: doc exists but already soft-deleted → idempotent no-op.
    store.delete("a")
    # Still gone from the active view.
    with pytest.raises(NotFoundError):
        store.get("a")


def test_prune_hard_deletes_old(store: SqliteStore) -> None:
    d = _doc(id="a")
    d.deleted_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    store.add(d)
    store.delete("a")  # sets deleted_at to now (overrides)
    # Soft-delete again with old timestamp directly via SQL.
    store._conn.execute(
        "UPDATE documents SET deleted_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", "a"),
    )
    n = store.prune(older_than=timedelta(days=30))
    assert n == 1
    with pytest.raises(NotFoundError):
        store.get("a", include_deleted=True)


def test_prune_skips_recent(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.delete("a")
    n = store.prune(older_than=timedelta(days=30))
    assert n == 0


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------


def test_link_creates_edge(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.add(_doc(id="b"))
    link = store.link("a", "b", rel="relates-to")
    assert isinstance(link, Link)
    assert (link.from_id, link.to_id, link.rel) == ("a", "b", "relates-to")
    assert store.backlinks("b") == [link]
    assert store.outlinks("a") == [link]


def test_link_idempotent(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.add(_doc(id="b"))
    l1 = store.link("a", "b")
    l2 = store.link("a", "b")
    assert l1.created_at == l2.created_at


def test_link_unknown_endpoint_raises(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    with pytest.raises(NotFoundError):
        store.link("a", "ghost")


def test_link_invalid_rel_raises(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.add(_doc(id="b"))
    with pytest.raises(ValidationError):
        store.link("a", "b", rel="")


def test_unlink_specific_rel(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.add(_doc(id="b"))
    store.link("a", "b", rel="blocks")
    store.link("a", "b", rel="supersedes")
    n = store.unlink("a", "b", rel="blocks")
    assert n == 1
    remaining = store.outlinks("a")
    assert [link.rel for link in remaining] == ["supersedes"]


def test_unlink_all(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.add(_doc(id="b"))
    store.link("a", "b", rel="x")
    store.link("a", "b", rel="y")
    n = store.unlink("a", "b")
    assert n == 2
    assert store.outlinks("a") == []


def test_link_cascade_on_delete(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.add(_doc(id="b"))
    store.link("a", "b")
    store.delete("b")
    # soft-delete does NOT cascade (per FK ON DELETE CASCADE only fires on hard DELETE).
    # That's the intended semantics: a tombstone document may still appear in graph queries.
    assert store.outlinks("a") != []  # edge still there
    # Hard delete via prune
    store._conn.execute(
        "UPDATE documents SET deleted_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", "b"),
    )
    store.prune()
    assert store.outlinks("a") == []


# ---------------------------------------------------------------------------
# import_many / export_all
# ---------------------------------------------------------------------------


def test_import_many_inserts_and_updates(store: SqliteStore) -> None:
    d1 = _doc(id="a", source="dir/a.md")
    store.import_many([d1])
    assert store.get("a").title == "Test"
    # re-import with updated title
    d1_updated = Document(
        id="a",
        type="project",
        title="Renamed",
        body="x",
        source="dir/a.md",
        created_at=d1.created_at,
        updated_at=d1.updated_at,
    )
    report = store.import_many([d1_updated])
    assert report.updated == 1
    assert store.get("a").title == "Renamed"


def test_export_all_excludes_deleted(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    store.add(_doc(id="b"))
    store.delete("a")
    ids = {d.id for d in store.export_all()}
    assert ids == {"b"}
    ids_inc = {d.id for d in store.export_all(include_deleted=True)}
    assert ids_inc == {"a", "b"}


# ---------------------------------------------------------------------------
# doctor / reindex
# ---------------------------------------------------------------------------


def test_doctor_clean_db(store: SqliteStore) -> None:
    store.add(_doc(id="a"))
    report = store.doctor()
    assert report.ok
    assert {c.name for c in report.checks} == {
        "integrity_check",
        "fts_sync",
        "no_orphan_links",
        "valid_type_title",
    }


def test_reindex_runs_clean(store: SqliteStore) -> None:
    """``reindex`` is a no-op on a clean DB and survives bulk inserts.

    FTS drift detection requires bypassing the sync triggers directly
    on the FTS table, which is fragile across SQLite versions. We
    verify the public contract: reindex returns without error and
    leaves the DB in a consistent state.
    """
    for i in range(50):
        store.add(_doc(id=f"d/{i}", title=f"D{i}", body=f"body {i}"))
    store.reindex()
    report = store.doctor()
    assert report.ok
    # Search still works.
    hits = store.search("body", limit=100)
    assert len(hits) == 50


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager(tmp_path: Path) -> None:
    db = tmp_path / "ctx.db"
    with SqliteStore(db) as s:
        s.add(_doc(id="a"))
        assert s.get("a").title == "Test"


# ---------------------------------------------------------------------------
# Versioning / restore / diff
# ---------------------------------------------------------------------------


def test_restore_to_previous_version(store: SqliteStore) -> None:
    """restore() reverts a document to a prior version snapshot."""
    store.add(_doc(id="proj/versioned", title="v1", body="first"))
    store.update("proj/versioned", body="second")
    store.update("proj/versioned", body="third", title="v3")

    history = store.document_history("proj/versioned")
    assert len(history) >= 3
    # Restore to the oldest version (body="first", title="v1")
    first_version = history[-1]["version_id"]
    restored = store.restore("proj/versioned", version_id=first_version)
    assert restored.body == "first"
    assert restored.title == "v1"


def test_restore_latest_without_version_id(store: SqliteStore) -> None:
    """restore() without version_id restores to the most recent version."""
    store.add(_doc(id="proj/v2", title="orig", body="a"))
    store.update("proj/v2", body="b")
    store.update("proj/v2", body="c")

    restored = store.restore("proj/v2")
    # Latest version in history is the update to body="c"
    assert restored.body == "c"


def test_restore_nonexistent_version_raises(store: SqliteStore) -> None:
    """restore() with invalid version_id raises NotFoundError."""
    store.add(_doc(id="proj/v3", title="orig", body="a"))
    with pytest.raises(NotFoundError):
        store.restore("proj/v3", version_id=99999)


def test_diff_two_versions(store: SqliteStore) -> None:
    """diff() returns field-level differences between two versions."""
    store.add(_doc(id="proj/diffable", title="v1", body="hello"))
    store.update("proj/diffable", body="world", title="v2")

    history = store.document_history("proj/diffable")
    v1_id = history[-1]["version_id"]  # create: body="hello", title="v1"
    v2_id = history[-2]["version_id"]  # update: body="world", title="v2"

    result = store.diff("proj/diffable", v1_id, v2_id)
    assert "changed" in result
    changed = result["changed"]
    assert "body" in changed or "title" in changed


def test_restore_deleted_document(store: SqliteStore) -> None:
    """restore_deleted() clears deleted_at and makes the doc active again."""
    store.add(_doc(id="proj/undead", title="Zombie", body="brr"))
    store.delete("proj/undead")

    doc = store.restore_deleted("proj/undead")
    assert doc.deleted_at is None
    assert doc.title == "Zombie"
    # Verify it shows up in normal listing
    active = store.list(type="project", limit=100)
    assert any(d.id == "proj/undead" for d in active)


def test_restore_deleted_not_deleted_raises(store: SqliteStore) -> None:
    """restore_deleted() on an active doc raises ValidationError."""
    store.add(_doc(id="proj/alive", title="Alive", body=""))
    with pytest.raises(ValidationError):
        store.restore_deleted("proj/alive")


def test_get_versions_alias(store: SqliteStore) -> None:
    """get_versions() is an alias for document_history()."""
    store.add(_doc(id="proj/alias-test", title="orig"))
    store.update("proj/alias-test", body="updated")
    versions = store.get_versions("proj/alias-test")
    history = store.document_history("proj/alias-test")
    assert len(versions) == len(history)


def test_diff_versions_alias(store: SqliteStore) -> None:
    """diff_versions() is an alias for diff()."""
    store.add(_doc(id="proj/diff-alias", title="a", body="x"))
    store.update("proj/diff-alias", body="y")
    history = store.document_history("proj/diff-alias")
    v1 = history[-1]["version_id"]
    v2 = history[-2]["version_id"]
    r1 = store.diff("proj/diff-alias", v1, v2)
    r2 = store.diff_versions("proj/diff-alias", v1, v2)
    assert r1 == r2


def test_restore_version_alias(store: SqliteStore) -> None:
    """restore_version() is an alias for restore()."""
    store.add(_doc(id="proj/rv-alias", title="t1", body="b1"))
    store.update("proj/rv-alias", body="b2")
    history = store.document_history("proj/rv-alias")
    first_id = history[-1]["version_id"]
    doc = store.restore_version("proj/rv-alias", first_id)
    assert doc.body == "b1"


def test_audit_log_has_entries(store: SqliteStore) -> None:
    """audit_log() returns entries for created/updated/deleted documents."""
    store.add(_doc(id="proj/audited", title="Audit"))
    store.update("proj/audited", body="changed")
    store.delete("proj/audited")

    audit = store.audit_log(limit=100)
    actions = {entry["action"] for entry in audit}
    assert "create" in actions
    assert "update" in actions
    assert "delete" in actions
