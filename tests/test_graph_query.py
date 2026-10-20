from pathlib import Path
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.graph_query import GraphQueryEngine, doctor_fix_hygiene


def test_multi_hop_path_and_query(tmp_path: Path):
    db_path = tmp_path / "graph_kb.db"
    store = SqliteStore(db_path)

    # A -> B -> C
    doc_a = Document(id="proj/service-a", type="project", title="Service A", body="Service A")
    doc_b = Document(
        id="dec/cache-strategy", type="decision", title="Cache Strategy", body="Cache ADR"
    )
    doc_c = Document(
        id="lesson/redis-oom", type="lesson", title="Redis OOM", body="Lesson about memory"
    )

    store.add(doc_a)
    store.add(doc_b)
    store.add(doc_c)

    store.link("proj/service-a", "dec/cache-strategy", rel="governs")
    store.link("dec/cache-strategy", "lesson/redis-oom", rel="relates-to")

    engine = GraphQueryEngine(store)

    # 1. Test find_path from A to C
    path = engine.find_path("proj/service-a", "lesson/redis-oom")
    assert path is not None
    assert len(path) == 2
    assert path[0]["from"] == "proj/service-a"
    assert path[0]["to"] == "dec/cache-strategy"
    assert path[1]["to"] == "lesson/redis-oom"

    # 2. Test multi-hop relations query from A
    reached = engine.query_relations("proj/service-a", max_depth=2)
    assert len(reached) == 2
    reached_ids = {r["id"] for r in reached}
    assert "dec/cache-strategy" in reached_ids
    assert "lesson/redis-oom" in reached_ids

    # 3. Test doctor auto fix
    fix_res = doctor_fix_hygiene(store)
    assert fix_res["ok"] is True

    store.close()
