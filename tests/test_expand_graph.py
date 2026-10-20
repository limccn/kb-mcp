"""Tests for multi-hop RAG / graph-enhanced search (v0.8 特性 #5)."""

import pytest
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


class TestSearchExpandGraph:
    """Test the expand_graph feature on SqliteStore.search()."""

    def test_search_returns_related(self, tmp_path):
        """Search hits should have related docs from graph edges."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="a", type="decision", title="Alpha Decision", body="alpha content"))
        store.add(Document(id="b", type="lesson", title="Beta Lesson", body="beta content"))
        store.add(Document(id="c", type="lesson", title="Gamma Lesson", body="gamma content"))
        store.link("a", "b", rel="governs")
        store.link("a", "c", rel="depends-on")

        hits = store.search("Alpha", mode="lexical", limit=5, expand_graph=True)
        assert len(hits) >= 1
        hit = hits[0]
        assert hit.doc.id == "a"
        related_ids = {r.doc.id for r in hit.related}
        assert "b" in related_ids
        assert "c" in related_ids
        store.close()

    def test_expand_disabled(self, tmp_path):
        """When expand_graph=False, related should be empty."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="x", type="decision", title="X", body="x content"))
        store.add(Document(id="y", type="lesson", title="Y", body="y content"))
        store.link("x", "y", rel="governs")

        hits = store.search("X", mode="lexical", limit=5, expand_graph=False)
        assert len(hits) >= 1
        assert hits[0].related == []
        store.close()

    def test_max_neighbors_limit(self, tmp_path):
        """Related count should not exceed max_neighbors."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="hub", type="decision", title="Hub", body="hub content"))
        for i in range(8):
            store.add(
                Document(id=f"spoke{i}", type="lesson", title=f"Spoke{i}", body=f"spoke{i} content")
            )
            store.link("hub", f"spoke{i}", rel="governs")

        hits = store.search("Hub", mode="lexical", limit=5, expand_graph=True, max_neighbors=3)
        assert len(hits) >= 1
        assert len(hits[0].related) <= 3
        store.close()

    def test_decay_applied(self, tmp_path):
        """Related scores should be parent_score * decay."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="main", type="decision", title="Main", body="main content"))
        store.add(Document(id="sub", type="lesson", title="Sub", body="sub content"))
        store.link("main", "sub", rel="governs")

        hits = store.search("Main", mode="lexical", limit=5, expand_graph=True, decay=0.5)
        assert len(hits) >= 1
        if hits[0].related:
            parent_score = hits[0].score
            expected = parent_score * 0.5
            assert hits[0].related[0].score == pytest.approx(expected, abs=0.01)
        store.close()

    def test_inbound_edges_in_related(self, tmp_path):
        """Inbound links should also appear in related."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="target", type="lesson", title="Target", body="target content"))
        store.add(Document(id="source", type="decision", title="Source", body="source content"))
        store.link("source", "target", rel="governs")

        hits = store.search("Target", mode="lexical", limit=5, expand_graph=True)
        assert len(hits) >= 1
        related_ids = {r.doc.id for r in hits[0].related}
        assert "source" in related_ids
        # Check direction is inbound (source → target means target has inbound from source)
        source_rel = [r for r in hits[0].related if r.doc.id == "source"][0]
        assert source_rel.direction == "inbound"
        store.close()

    def test_empty_search_no_expand(self, tmp_path):
        """Empty search results should not cause errors."""
        store = SqliteStore(tmp_path / "test.db")
        hits = store.search("nonexistent", mode="lexical", expand_graph=True)
        assert hits == []
        store.close()
