from pathlib import Path
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore


def test_cjk_search(tmp_path: Path):
    db_path = tmp_path / "cjk_kb.db"
    store = SqliteStore(db_path)

    doc1 = Document(
        id="lesson/redis-crash",
        type="lesson",
        title="缓存击穿排障记录",
        body="生产环境由于高并发导致Redis缓存击穿，数据库负载过高崩溃。",
        tags=["redis", "线上故障"],
    )
    doc2 = Document(
        id="dec/use-jwt",
        type="decision",
        title="认证架构选型决策",
        body="鉴权机制采用无状态的JWT Token与非对称加密算法。",
        tags=["架构", "安全"],
    )
    store.add(doc1)
    store.add(doc2)

    # 1. Exact CJK keyword search
    hits = store.search("缓存击穿", mode="hybrid")
    assert len(hits) >= 1
    assert hits[0].doc.id == "lesson/redis-crash"

    # 2. Fuzzy / partial CJK search
    hits_partial = store.search("非对称加密", mode="hybrid")
    assert len(hits_partial) >= 1
    assert hits_partial[0].doc.id == "dec/use-jwt"

    # 3. Mixed English and CJK search
    hits_mixed = store.search("Redis 崩溃", mode="hybrid")
    assert len(hits_mixed) >= 1
    assert hits_mixed[0].doc.id == "lesson/redis-crash"

    store.close()
