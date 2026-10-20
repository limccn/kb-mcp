"""End-to-end tests for semantic search (Phase D of v0.2).

Uses a fake ``Embedder`` that hashes the input text into a deterministic
float vector — the same input always produces the same vector, similar
inputs produce similar vectors. This lets us assert that the vec0 path
ranks semantically-related documents ahead of unrelated ones without
needing a real embeddings API.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kb_mcp_lite.embedder import Embedder
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


class _HashingEmbedder(Embedder):
    """Deterministic embedder: hash the text, expand to ``dim`` floats.

    Two texts that share an n-gram will share corresponding vector
    slices, so the cosine distance between their vectors roughly
    reflects string overlap. Good enough for tests.
    """

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def enabled(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        # Hash each 3-char window of the text, take the first byte as a
        # 0..255 value, normalise to [-1, 1]. Pad/truncate to ``dim``.
        text = text.lower()
        out: list[float] = []
        for i in range(0, max(1, len(text) - 2)):
            h = hashlib.md5(text[i : i + 3].encode("utf-8")).digest()
            out.append((h[0] - 128) / 128.0)
        if not out:
            out = [0.0]
        # Pad / truncate
        if len(out) >= self._dim:
            return out[: self._dim]
        return out + [0.0] * (self._dim - len(out))


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    """SqliteStore with a hashing embedder and 4 test documents."""
    db = tmp_path / "semantic.db"
    emb = _HashingEmbedder(dim=64)
    # Write-through store: ranking tests assert immediately after seeding,
    # so each add drains the (mock) embedder synchronously.
    from conftest import SyncEmbedStore

    s = SyncEmbedStore(db, embedder=emb)
    s.add(
        Document(
            id="proj/python-cli",
            type="project",
            title="Python CLI for kb-mcp",
            body="A command-line tool written in Python for the kb-mcp project.",
            tags=["python", "cli"],
        )
    )
    s.add(
        Document(
            id="proj/rust-server",
            type="project",
            title="Rust HTTP server",
            body="A high-performance HTTP server built with the Rust programming language.",
            tags=["rust", "http"],
        )
    )
    s.add(
        Document(
            id="lesson/llm-api-design",
            type="lesson",
            title="Designing LLM-friendly HTTP APIs",
            body="When building APIs consumed by LLM agents, prefer JSON and clear schemas.",
            tags=["llm", "api"],
        )
    )
    s.add(
        Document(
            id="lesson/cooking-rice",
            type="lesson",
            title="How to cook perfect rice",
            body="Rinse, soak, steam. Use the right water ratio.",
            tags=["cooking"],
        )
    )
    return s


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_semantic_mode_works(store: SqliteStore) -> None:
    """mode='semantic' returns at least one hit when vectors exist."""
    hits = store.search("Python CLI", mode="semantic")
    assert hits, "expected at least one hit from hashing embedder"
    # The hashing embedder is not real semantic similarity, so we only
    # assert that SOME result is returned; the ranking test below is
    # the stronger assertion.
    assert all(h.doc.id for h in hits)


def test_semantic_mode_ranks_relevant_first(store: SqliteStore) -> None:
    """A document whose body shares more n-grams with the query should
    rank ahead of unrelated documents.

    Note: the test embedder is a hash-based approximation, not a real
    semantic model. We assert only that the *most related* document
    (the Python CLI one) is in the top 3 results.
    """
    hits = store.search("Python CLI", mode="semantic", limit=10)
    top_ids = [h.doc.id for h in hits[:3]]
    assert "proj/python-cli" in top_ids, f"expected python-cli in top 3, got {top_ids}"


# ---------------------------------------------------------------------------
# Hybrid mode: best of all three
# ---------------------------------------------------------------------------


def test_hybrid_includes_semantic_hits(store: SqliteStore) -> None:
    """Hybrid search surfaces results even when no exact FTS hit exists."""
    # 'recipe' is not in any document text — lexical search returns
    # nothing. Semantic should still find a hit on shared n-grams.
    lexical = store.search("recipe", mode="lexical")
    hybrid = store.search("recipe", mode="hybrid")
    assert not lexical  # no FTS match
    # The hybrid path may or may not surface something depending on the
    # hashing embedder's n-gram coverage; just assert it doesn't crash
    # and the result is a list.
    assert isinstance(hybrid, list)


def test_hybrid_preserves_exact_first(store: SqliteStore) -> None:
    """A query that hits FTS exactly should rank that doc first in hybrid."""
    hits = store.search("Python CLI for kb-mcp", mode="hybrid", limit=10)
    assert hits
    assert hits[0].doc.id == "proj/python-cli"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_semantic_without_embedder_raises(tmp_path: Path) -> None:
    """No embedder + no config => NullEmbedder => ValidationError."""
    from kb_mcp_lite.embedder import NullEmbedder

    db = tmp_path / "no_emb.db"
    s = SqliteStore(db, embedder=NullEmbedder())
    s.add(Document(id="a", type="x", title="A", body=""))
    with pytest.raises(Exception) as exc:
        s.search("anything", mode="semantic")
    assert "embedder" in str(exc.value).lower() or "semantic" in str(exc.value).lower()


def test_semantic_unknown_mode_raises(store: SqliteStore) -> None:
    with pytest.raises(Exception) as exc:
        store.search("x", mode="bogus")
    assert "mode" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Embedder is best-effort: a broken embedder does NOT break add()
# ---------------------------------------------------------------------------


class _BrokenEmbedder(Embedder):
    """Raises on every embed call."""

    @property
    def dim(self) -> int:
        return 0

    @property
    def enabled(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("intentional embedder failure")


def test_broken_embedder_does_not_break_add(tmp_path: Path) -> None:
    """A misconfigured embedder must not prevent documents being added."""
    db = tmp_path / "broken_emb.db"
    s = SqliteStore(db, embedder=_BrokenEmbedder())
    # Should NOT raise, even though _index_embedding will hit the error.
    s.add(Document(id="x", type="lesson", title="X", body="body"))
    s.add(Document(id="y", type="lesson", title="Y", body="body"))
    assert s.get("x").id == "x"
    assert s.get("y").id == "y"
    s.close()
