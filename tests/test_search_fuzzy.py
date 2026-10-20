"""Tests for trigram (fuzzy) search introduced in v0.2 (migration 0002).

Covers the three modes (``lexical``, ``fuzzy``, ``hybrid``) end-to-end on a
real SqliteStore, plus the SqliteStore-internal search/​_search_fts/​_search_hybrid
helpers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    db = tmp_path / "fuzzy.db"
    s = SqliteStore(db)
    # Add several documents that exercise fuzzy / exact matching.
    s.add(
        Document(
            id="lesson/sqlite-fts5",
            type="lesson",
            title="Use SQLite FTS5 for full-text search",
            body="FTS5 supports BM25 ranking, snippets, and the trigram tokenizer.",
            tags=["sqlite", "fts"],
        )
    )
    s.add(
        Document(
            id="lesson/sqlite-isolation",
            type="lesson",
            title="SQLite isolation levels explained",
            body="WAL mode lets readers proceed while a writer is active.",
            tags=["sqlite"],
        )
    )
    s.add(
        Document(
            id="proj/fastech-energy",
            type="project",
            title="Fastech Energy Web",
            body="Fastech is the energy-management product line.",
            tags=["fastech", "energy"],
        )
    )
    s.add(
        Document(
            id="proj/fastech-remote",
            type="project",
            title="Fastech remote components",
            body="Reusable micro-frontends shared between Fastech apps.",
            tags=["fastech", "micro-frontend"],
        )
    )
    s.add(
        Document(
            id="glossary/sqlite",
            type="glossary",
            title="SQLite",
            body="An embedded relational database with FTS5 support.",
            tags=["database"],
        )
    )
    return s


# ---------------------------------------------------------------------------
# lexical mode: only exact tokens match
# ---------------------------------------------------------------------------


def test_lexical_exact_match(store: SqliteStore) -> None:
    hits = store.search("SQLite FTS5", mode="lexical")
    ids = [h.doc.id for h in hits]
    assert "lesson/sqlite-fts5" in ids


def test_lexical_typo_misses(store: SqliteStore) -> None:
    """A typo in lexical mode finds nothing — the whole point of fuzzy."""
    hits = store.search("sqlitte", mode="lexical")
    assert all("sqlite" not in h.doc.id for h in hits) or not hits


def test_lexical_separator_mismatch_misses(store: SqliteStore) -> None:
    """Searching 'fastechenergy' (no separator) misses 'fastech-energy'."""
    hits = store.search("fastechenergy", mode="lexical")
    assert not hits


# ---------------------------------------------------------------------------
# fuzzy mode: trigram tolerates typos and prefix matches
# ---------------------------------------------------------------------------


def test_fuzzy_typo_hits(store: SqliteStore) -> None:
    """'sqlit' (missing te) finds sqlite docs via trigram overlap."""
    hits = store.search("sqlit", mode="fuzzy")
    ids = {h.doc.id for h in hits}
    assert "lesson/sqlite-fts5" in ids
    assert "glossary/sqlite" in ids


def test_fuzzy_extra_letter_does_not_match(store: SqliteStore) -> None:
    """Documented limitation: FTS5 trigram matches via 3-gram overlap,
    not edit distance. Inserting a letter (``sqlitte`` vs ``sqlite``)
    breaks the 3-gram chain and is NOT a hit. Use Levenshtein-based
    fuzzy if you need that.
    """
    hits = store.search("sqlitte", mode="fuzzy")
    assert not hits


def test_fuzzy_no_separator_partial(store: SqliteStore) -> None:
    """Documented limitation: token boundary matters for trigram. A
    query like ``fastechenergy`` (no separator) tokenises as a single
    13-char word; the trigram index for ``fastech-energy`` has the
    trigrams for ``fastech``, ``-``, and ``energy`` separately, so they
    do not overlap. For multi-word queries, prefer ``fastech energy``
    (which fuzzy mode handles correctly).
    """
    hits = store.search("fastechenergy", mode="fuzzy")
    assert not hits


def test_fuzzy_with_separator_works(store: SqliteStore) -> None:
    """'fastech energy' (with separator) finds fastech docs."""
    hits = store.search("fastech energy", mode="fuzzy")
    ids = {h.doc.id for h in hits}
    assert "proj/fastech-energy" in ids
    assert "proj/fastech-remote" in ids


def test_fuzzy_prefix(store: SqliteStore) -> None:
    """'faste' (prefix) finds fastech docs."""
    hits = store.search("faste", mode="fuzzy")
    ids = {h.doc.id for h in hits}
    assert "proj/fastech-energy" in ids
    assert "proj/fastech-remote" in ids


# ---------------------------------------------------------------------------
# hybrid mode: union, exact wins
# ---------------------------------------------------------------------------


def test_hybrid_combines_both(store: SqliteStore) -> None:
    """A query that has BOTH exact matches and fuzzy-only matches returns
    exact ones first, then fuzzy-only, truncated to limit."""
    # 'FTS' is an exact token in lesson/sqlite-fts5 only.
    # 'sqlit' is fuzzy for everything containing 'sqlite'.
    hits = store.search("FTS sqlit", mode="hybrid", limit=10)
    ids = [h.doc.id for h in hits]
    # The exact match should come first.
    assert ids[0] == "lesson/sqlite-fts5"
    # Fuzzy-only matches (glossary/sqlite, lesson/sqlite-isolation) follow.
    assert "glossary/sqlite" in ids or "lesson/sqlite-isolation" in ids


def test_hybrid_respects_limit(store: SqliteStore) -> None:
    hits = store.search("sqlite", mode="hybrid", limit=2)
    assert len(hits) == 2


# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------


def test_unknown_mode_raises(store: SqliteStore) -> None:
    with pytest.raises(Exception) as exc:
        store.search("anything", mode="bogus")
    assert "mode" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Trigram index is kept in sync (migration 0002 + triggers)
# ---------------------------------------------------------------------------


def test_trgm_table_exists(store: SqliteStore) -> None:
    names = {
        row["name"]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "docs_fts_trgm" in names


def test_trgm_index_updated_on_add(store: SqliteStore) -> None:
    """Adding a document should add a row to docs_fts_trgm (via trigger)."""
    before = store._conn.execute("SELECT COUNT(*) FROM docs_fts_trgm").fetchone()[0]
    store.add(
        Document(
            id="lesson/trgm-sync",
            type="lesson",
            title="Trigram sync test",
            body="Verifying trigger fires on insert.",
            tags=[],
        )
    )
    after = store._conn.execute("SELECT COUNT(*) FROM docs_fts_trgm").fetchone()[0]
    assert after == before + 1


def test_trgm_index_updated_on_update(store: SqliteStore) -> None:
    store.update("lesson/sqlite-fts5", title="Use SQLite FTS5 (updated)")
    hits = store.search("updated", mode="fuzzy")
    assert any(h.doc.id == "lesson/sqlite-fts5" for h in hits)


def test_trgm_index_updated_on_delete(store: SqliteStore) -> None:
    store.delete("lesson/sqlite-fts5")
    hits = store.search("FTS5", mode="fuzzy")
    assert all(h.doc.id != "lesson/sqlite-fts5" for h in hits)
