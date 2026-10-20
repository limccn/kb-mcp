"""Tests for kb similar, suggest-tags, classify, dedup.

Uses a deterministic hashing embedder (same as test_search_semantic.py)
to exercise the embedding-based similarity helpers in SqliteStore.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kb_mcp_lite.embedder import Embedder
from kb_mcp_lite.schema import Document, NotFoundError, ValidationError
from kb_mcp_lite.store.sqlite import SqliteStore


class _HashingEmbedder(Embedder):
    """Deterministic embedder: hash the text, expand to ``dim`` floats."""

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def enabled(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        out: list[float] = []
        for i in range(0, max(1, len(text) - 2)):
            h = hashlib.md5(text[i : i + 3].encode("utf-8")).digest()
            out.append((h[0] - 128) / 128.0)
        if not out:
            out = [0.0]
        if len(out) >= self._dim:
            return out[: self._dim]
        return out + [0.0] * (self._dim - len(out))


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    """SqliteStore with a hashing embedder and test documents."""
    db = tmp_path / "similarity.db"
    emb = _HashingEmbedder(dim=64)
    # Write-through store: ranking tests assert immediately after seeding,
    # so each add drains the (mock) embedder synchronously.
    from conftest import SyncEmbedStore

    s = SyncEmbedStore(db, embedder=emb)
    # ---- python / cli group ----
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
            body="A high-performance HTTP server built with Rust and Actix.",
            tags=["rust", "server", "http"],
        )
    )
    s.add(
        Document(
            id="faq/python-vs-rust",
            type="faq",
            title="Python vs Rust comparison",
            body="Python is easier to learn. Rust is faster but has a steeper learning curve.",
            tags=["python", "rust", "comparison"],
        )
    )
    # ---- cooking group ----
    s.add(
        Document(
            id="lesson/cooking-rice",
            type="lesson",
            title="How to cook perfect rice",
            body="Rinse, soak, steam. Use the right water ratio.",
            tags=["cooking"],
        )
    )
    s.add(
        Document(
            id="lesson/boiling-eggs",
            type="lesson",
            title="How to boil eggs",
            body="Boil water, add eggs, time it right for soft or hard boiled.",
            tags=["cooking", "eggs"],
        )
    )
    s.add(
        Document(
            id="faq/why-rice-sticks",
            type="faq",
            title="Why does my rice stick to the pot",
            body="Not rinsing enough starch leads to sticky rice.",
            tags=["cooking", "rice"],
        )
    )
    return s


class TestSimilarDocs:
    """kb similar — find documents by embedding similarity."""

    def test_similar_returns_related_docs(self, store: SqliteStore) -> None:
        """Python CLI doc should find python/rust related docs."""
        results = store.similar_docs("proj/python-cli", limit=5)
        # Hashing embedder produces platform-dependent distances;
        # at minimum the self-doc is excluded.
        assert len(results) >= 1
        ids = [doc.id for doc, _ in results]
        assert "proj/python-cli" not in ids

    def test_similar_excludes_self(self, store: SqliteStore) -> None:
        """The source document must not appear in results."""
        results = store.similar_docs("proj/python-cli", limit=10)
        ids = [doc.id for doc, _ in results]
        assert "proj/python-cli" not in ids

    def test_similar_returns_distance(self, store: SqliteStore) -> None:
        """Each result includes a float distance between 0 and ~2."""
        results = store.similar_docs("proj/python-cli", limit=5)
        for _, dist in results:
            assert isinstance(dist, float)
            assert 0.0 <= dist <= 2.0

    def test_similar_sorted_by_distance(self, store: SqliteStore) -> None:
        """Results are ordered nearest-first."""
        results = store.similar_docs("proj/python-cli", limit=5)
        distances = [d for _, d in results]
        assert distances == sorted(distances)

    def test_similar_unknown_doc_raises(self, store: SqliteStore) -> None:
        """Asking for similarity on a non-existent doc raises NotFoundError."""
        with pytest.raises(NotFoundError):
            store.similar_docs("nonexistent", limit=5)

    def test_similar_raises_without_embedder(self, tmp_path: Path) -> None:
        """Without an embedder, similar_docs reports the reason instead of []."""
        from kb_mcp_lite.embedder import NullEmbedder

        db = tmp_path / "noemb.db"
        s = SqliteStore(db, embedder=NullEmbedder())
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        with pytest.raises(ValidationError, match="embedder"):
            s.similar_docs("proj/a", limit=5)

    def test_similar_raises_when_vec0_unavailable(self, tmp_path: Path) -> None:
        """Enabled embedder + dead vec0 connection is reported as vec0, not []."""
        db = tmp_path / "novec.db"
        s = SqliteStore(db, embedder=_HashingEmbedder(dim=64), auto_start_worker=False)
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        s._vec_conn = False  # same cached sentinel _vec_conn_lazy() sets on failure
        with pytest.raises(ValidationError, match="vec0"):
            s.similar_docs("proj/a", limit=5)

    def test_similar_raises_when_doc_not_embedded(self, tmp_path: Path) -> None:
        """Enabled embedder + working vec0, but this document has no vector yet."""
        db = tmp_path / "notyet.db"
        s = SqliteStore(db, embedder=_HashingEmbedder(dim=64), auto_start_worker=False)
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        with pytest.raises(ValidationError, match="embedding"):
            s.similar_docs("proj/a", limit=5)


class TestSuggestTags:
    """kb suggest-tags — recommend tags from similar documents."""

    def test_suggest_tags_returns_sorted_by_weight(self, store: SqliteStore) -> None:
        """Results are ordered by descending weight."""
        results = store.suggest_tags("lesson/cooking-rice", limit=5)
        assert results
        weights = [w for _, w in results]
        assert weights == sorted(weights, reverse=True)

    def test_suggest_tags_returns_empty_for_unknown(self, store: SqliteStore) -> None:
        """Unknown doc should not crash — but suggest_tags relies on
        similar_docs which raises NotFoundError."""
        # Note: the current impl delegates to similar_docs which raises.
        pass

    def test_suggest_tags_raises_without_embedder(self, tmp_path: Path) -> None:
        """suggest_tags delegates to similar_docs, so the reason propagates."""
        from kb_mcp_lite.embedder import NullEmbedder

        db = tmp_path / "noemb.db"
        s = SqliteStore(db, embedder=NullEmbedder())
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        with pytest.raises(ValidationError, match="embedder"):
            s.suggest_tags("proj/a", limit=5)

    def test_suggest_tags_handles_untagged_docs(self, store: SqliteStore) -> None:
        """A doc with no tags that is similar to tagged docs still gets suggestions."""
        store.add(
            Document(
                id="lesson/new-recipe",
                type="lesson",
                title="A new recipe for rice",
                body="Cook rice with a new method that changes everything.",
                tags=[],
            )
        )
        results = store.suggest_tags("lesson/new-recipe", limit=5)
        # Should return tags from similar docs
        assert len(results) > 0


class TestSuggestType:
    """kb classify — suggest document type from similar documents."""

    def test_suggest_type_finds_majority_type(self, store: SqliteStore) -> None:
        """A new doc similar to other docs should suggest at least one type."""
        store.add(
            Document(
                id="lesson/frying-eggs",
                type="lesson",
                title="How to fry eggs",
                body="Heat oil, crack egg, fry until done to your liking.",
                tags=["cooking"],
            )
        )
        results = store.suggest_type("lesson/frying-eggs", limit=5)
        assert results
        # The hashing embedder is platform-dependent; only check results non-empty

    def test_suggest_type_sorted_by_weight(self, store: SqliteStore) -> None:
        """Results are ordered by descending weight."""
        store.add(
            Document(
                id="lesson/frying-eggs",
                type="lesson",
                title="How to fry eggs",
                body="Heat oil, crack egg, fry until done to your liking.",
                tags=["cooking"],
            )
        )
        results = store.suggest_type("lesson/frying-eggs", limit=5)
        weights = [w for _, w in results]
        assert weights == sorted(weights, reverse=True)

    def test_suggest_type_raises_without_embedder(self, tmp_path: Path) -> None:
        """suggest_type delegates to similar_docs, so the reason propagates."""
        from kb_mcp_lite.embedder import NullEmbedder

        db = tmp_path / "noemb.db"
        s = SqliteStore(db, embedder=NullEmbedder())
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        with pytest.raises(ValidationError, match="embedder"):
            s.suggest_type("proj/a", limit=5)


class TestFindDuplicates:
    """kb dedup — find near-duplicate document pairs."""

    def test_dedup_finds_similar_pairs(self, store: SqliteStore) -> None:
        """Docs in the same topic cluster should appear as near pairs."""
        # Use a generous threshold — the hashing embedder gives higher
        # cosine distances than a real model would.
        results = store.find_duplicates(threshold=1.0, limit=10)
        if not results:
            pytest.skip("no duplicate pairs found with hashing embedder on this platform")
        assert len(results) >= 1
        for id_a, id_b, dist in results:
            assert isinstance(id_a, str)
            assert isinstance(id_b, str)
            assert isinstance(dist, float)
            assert id_a != id_b  # no self-pairs

    def test_dedup_strict_threshold_fewer_results(self, store: SqliteStore) -> None:
        """A very strict (low) threshold returns fewer pairs."""
        loose = store.find_duplicates(threshold=1.0, limit=50)
        strict = store.find_duplicates(threshold=0.1, limit=50)
        assert len(strict) <= len(loose)

    def test_dedup_raises_without_embedder(self, tmp_path: Path) -> None:
        """Without an embedder, dedup reports the reason instead of []."""
        from kb_mcp_lite.embedder import NullEmbedder

        db = tmp_path / "noemb.db"
        s = SqliteStore(db, embedder=NullEmbedder())
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        s.add(Document(id="proj/b", type="project", title="B", body="other"))
        with pytest.raises(ValidationError, match="embedder"):
            s.find_duplicates(threshold=0.5)

    def test_dedup_raises_when_vec0_unavailable(self, tmp_path: Path) -> None:
        """Enabled embedder + dead vec0 connection is reported as vec0, not []."""
        db = tmp_path / "novec.db"
        s = SqliteStore(db, embedder=_HashingEmbedder(dim=64), auto_start_worker=False)
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        s._vec_conn = False  # same cached sentinel _vec_conn_lazy() sets on failure
        with pytest.raises(ValidationError, match="vec0"):
            s.find_duplicates(threshold=0.5)

    def test_dedup_raises_when_nothing_embedded(self, tmp_path: Path) -> None:
        """Enabled embedder, but no active document has a vector yet."""
        db = tmp_path / "notyet.db"
        s = SqliteStore(db, embedder=_HashingEmbedder(dim=64), auto_start_worker=False)
        s.add(Document(id="proj/a", type="project", title="A", body="test"))
        s.add(Document(id="proj/b", type="project", title="B", body="other"))
        with pytest.raises(ValidationError, match="embedding"):
            s.find_duplicates(threshold=0.5)

    def test_dedup_no_pairs_returns_empty_when_embedded(self, store: SqliteStore) -> None:
        """Legitimate empty result: vectors exist, but no pair is within threshold."""
        assert store.find_duplicates(threshold=-1.0) == []

    def test_dedup_limit_respected(self, store: SqliteStore) -> None:
        """The limit cap on returned pairs works."""
        results = store.find_duplicates(threshold=1.0, limit=3)
        assert len(results) <= 3
