"""Tests for the typed relation vocabulary and impact analyzer (v0.8 特性 #6)."""

from kb_mcp_lite.schema import Document
from kb_mcp_lite.relations import (
    STANDARD_RELATIONS,
    ImpactAnalyzer,
    get_relation_spec,
    resolve_relation,
    supersession_chain,
)
from kb_mcp_lite.store.sqlite import SqliteStore


# ---------------------------------------------------------------------------
# RelationSpec
# ---------------------------------------------------------------------------


class TestRelationSpec:
    def test_standard_relations_count(self):
        assert len(STANDARD_RELATIONS) == 11

    def test_relates_to_is_bidirectional(self):
        spec = STANDARD_RELATIONS["relates-to"]
        assert spec.default_direction == "bidirectional"
        assert spec.is_influence is False
        assert spec.is_supersession is False

    def test_supersedes_is_influence_and_supersession(self):
        spec = STANDARD_RELATIONS["supersedes"]
        assert spec.is_influence is True
        assert spec.is_supersession is True
        assert spec.default_direction == "forward"

    def test_governs_is_influence(self):
        spec = STANDARD_RELATIONS["governs"]
        assert spec.is_influence is True
        assert spec.is_supersession is False

    def test_references_is_not_influence(self):
        spec = STANDARD_RELATIONS["references"]
        assert spec.is_influence is False

    def test_contains_is_tree_relation(self):
        """contains: tree edge kb ingest generates (design D1)."""
        spec = STANDARD_RELATIONS["contains"]
        assert spec.forward_label == "包含了"
        assert spec.backward_label == "隶属于"
        assert spec.default_direction == "forward"
        assert spec.traversal_cost == 0.5
        assert spec.is_influence is False
        assert spec.is_supersession is False
        assert spec.description == "目录树关系，kb ingest 生成"

    def test_get_relation_spec_known(self):
        assert get_relation_spec("supersedes") is STANDARD_RELATIONS["supersedes"]

    def test_get_relation_spec_unknown(self):
        assert get_relation_spec("nonexistent") is None

    def test_resolve_relation_known(self):
        assert resolve_relation("depends-on") is STANDARD_RELATIONS["depends-on"]

    def test_resolve_relation_unknown_falls_back(self):
        result = resolve_relation("nonexistent")
        assert result is STANDARD_RELATIONS["relates-to"]


# ---------------------------------------------------------------------------
# ImpactAnalyzer
# ---------------------------------------------------------------------------


class TestImpactAnalyzer:
    def test_influence_edges_only(self, tmp_path):
        """Only is_influence=True relations should be followed."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="root", type="decision", title="Root"))
        store.add(Document(id="a", type="lesson", title="A"))
        store.add(Document(id="b", type="lesson", title="B"))
        store.add(Document(id="c", type="lesson", title="C"))

        # governs = influence → should be followed
        store.link("root", "a", rel="governs")
        # relates-to = NOT influence → should NOT be followed
        store.link("root", "b", rel="relates-to")
        # depends-on = influence → should be followed
        store.link("a", "c", rel="depends-on")

        analyzer = ImpactAnalyzer(store)
        result = analyzer.analyze("root")

        affected_ids = {n.doc.id for n in result}
        assert "a" in affected_ids
        assert "c" in affected_ids
        assert "b" not in affected_ids
        store.close()

    def test_max_depth(self, tmp_path):
        """BFS should stop at max_depth."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="r", type="decision", title="R"))
        store.add(Document(id="a1", type="lesson", title="A1"))
        store.add(Document(id="a2", type="lesson", title="A2"))
        store.add(Document(id="a3", type="lesson", title="A3"))

        store.link("r", "a1", rel="governs")
        store.link("a1", "a2", rel="governs")
        store.link("a2", "a3", rel="governs")

        analyzer = ImpactAnalyzer(store)
        result = analyzer.analyze("r", max_depth=2)
        affected_ids = {n.doc.id for n in result}
        assert "a1" in affected_ids
        assert "a2" in affected_ids
        assert "a3" not in affected_ids  # depth 3, beyond limit
        store.close()

    def test_max_results(self, tmp_path):
        """Should cap results at max_results."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="r", type="decision", title="R"))
        for i in range(10):
            store.add(Document(id=f"d{i}", type="lesson", title=f"D{i}"))
            store.link("r", f"d{i}", rel="governs")

        analyzer = ImpactAnalyzer(store)
        result = analyzer.analyze("r", max_results=3)
        assert len(result) <= 3
        store.close()

    def test_no_influence_edges(self, tmp_path):
        """With no influence edges, result should be empty."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="a", type="decision", title="A"))
        store.add(Document(id="b", type="lesson", title="B"))
        store.link("a", "b", rel="relates-to")

        analyzer = ImpactAnalyzer(store)
        result = analyzer.analyze("a")
        assert result == []
        store.close()

    def test_empty_root(self, tmp_path):
        """Impact of a nonexistent root should be empty."""
        store = SqliteStore(tmp_path / "test.db")
        analyzer = ImpactAnalyzer(store)
        result = analyzer.analyze("nonexistent")
        assert result == []
        store.close()


# ---------------------------------------------------------------------------
# supersession_chain
# ---------------------------------------------------------------------------


class TestSupersessionChain:
    def test_simple_chain(self, tmp_path):
        """v1 → v2 → v3 chain via superseded-by."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="v1", type="decision", title="V1"))
        store.add(Document(id="v2", type="decision", title="V2"))
        store.add(Document(id="v3", type="decision", title="V3"))

        store.link("v1", "v2", rel="superseded-by")
        store.link("v2", "v3", rel="superseded-by")

        chain = supersession_chain(store, "v1")
        assert chain == ["v1", "v2", "v3"]
        store.close()

    def test_reverse_chain(self, tmp_path):
        """v3 → v2 → v1 chain via supersedes."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="v1", type="decision", title="V1"))
        store.add(Document(id="v2", type="decision", title="V2"))

        store.link("v2", "v1", rel="supersedes")

        chain = supersession_chain(store, "v1")
        assert "v1" in chain
        assert "v2" in chain
        store.close()

    def test_no_supersession(self, tmp_path):
        """With no supersession edges, chain is just the root."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="x", type="decision", title="X"))
        store.add(Document(id="y", type="lesson", title="Y"))
        store.link("x", "y", rel="relates-to")

        chain = supersession_chain(store, "x")
        assert chain == ["x"]
        store.close()

    def test_single_doc(self, tmp_path):
        """Single doc with no links returns itself."""
        store = SqliteStore(tmp_path / "test.db")
        store.add(Document(id="solo", type="decision", title="Solo"))

        chain = supersession_chain(store, "solo")
        assert chain == ["solo"]
        store.close()
