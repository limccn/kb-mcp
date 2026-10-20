"""Unit tests for Reranker module (kb-mcp RAG pipeline)."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import httpx

from kb_mcp_lite.reranker import (
    HttpReranker,
    NullReranker,
    RerankConfig,
    RerankError,
    RerankItem,
    load_rerank_config,
    make_reranker,
)
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import VaultManager


# ---------------------------------------------------------------------------
# RerankConfig & Endpoints
# ---------------------------------------------------------------------------


def test_rerank_config_endpoint_variations() -> None:
    # 1. Base URL without version
    cfg1 = RerankConfig(base_url="https://api.siliconflow.cn", model="bge-reranker")
    assert cfg1.endpoint == "https://api.siliconflow.cn/v1/rerank"

    # 2. Base URL with /v1
    cfg2 = RerankConfig(base_url="https://api.siliconflow.cn/v1", model="bge-reranker")
    assert cfg2.endpoint == "https://api.siliconflow.cn/v1/rerank"

    # 3. Base URL already ending with /rerank
    cfg3 = RerankConfig(base_url="https://api.jina.ai/v1/rerank", model="jina-reranker")
    assert cfg3.endpoint == "https://api.jina.ai/v1/rerank"

    # 4. Trailing slash cleanup
    cfg4 = RerankConfig(base_url="https://api.cohere.com/v2/", model="rerank-v3.5")
    assert cfg4.endpoint == "https://api.cohere.com/v2/rerank"


# ---------------------------------------------------------------------------
# NullReranker
# ---------------------------------------------------------------------------


def test_null_reranker() -> None:
    reranker = NullReranker()
    assert not reranker.enabled

    docs = ["First doc", "Second doc", "Third doc"]
    items = reranker.rerank("query", docs)
    assert len(items) == 3
    assert [it.index for it in items] == [0, 1, 2]
    assert [it.score for it in items] == [0.0, 0.0, 0.0]

    # With top_n
    items_top2 = reranker.rerank("query", docs, top_n=2)
    assert len(items_top2) == 2


# ---------------------------------------------------------------------------
# HttpReranker
# ---------------------------------------------------------------------------


def test_http_reranker_success_standard_format(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = RerankConfig(
        base_url="https://api.siliconflow.cn/v1",
        model="BAAI/bge-reranker-v2-m3",
        api_key="sk-test",
    )
    reranker = HttpReranker(cfg)
    assert reranker.enabled
    assert reranker.model == "BAAI/bge-reranker-v2-m3"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {"index": 1, "relevance_score": 0.98},
            {"index": 0, "relevance_score": 0.42},
            {"index": 2, "relevance_score": 0.15},
        ]
    }

    def fake_post(url, headers, content, timeout):
        assert url == "https://api.siliconflow.cn/v1/rerank"
        assert headers["Authorization"] == "Bearer sk-test"
        req_body = json.loads(content)
        assert req_body["query"] == "how to use sqlite"
        assert len(req_body["documents"]) == 3
        return mock_resp

    monkeypatch.setattr("httpx.post", fake_post)

    docs = ["Doc A (irrelevant)", "Doc B (very relevant)", "Doc C (weak)"]
    items = reranker.rerank("how to use sqlite", docs)
    assert len(items) == 3
    # Top item should be index 1
    assert items[0].index == 1
    assert items[0].score == 0.98
    assert items[0].document == "Doc B (very relevant)"
    assert items[1].index == 0
    assert items[2].index == 2


def test_http_reranker_success_data_format(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = RerankConfig(
        base_url="https://api.example.com/v1",
        model="custom-reranker",
    )
    reranker = HttpReranker(cfg)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"index": 0, "score": 0.85},
            {"index": 1, "score": 0.95},
        ]
    }
    monkeypatch.setattr("httpx.post", lambda *a, **kw: mock_resp)

    docs = ["Doc A", "Doc B"]
    items = reranker.rerank("query", docs, top_n=1)
    assert len(items) == 1
    assert items[0].index == 1
    assert items[0].score == 0.95


def test_http_reranker_empty_inputs() -> None:
    cfg = RerankConfig(base_url="http://localhost:8000", model="m")
    reranker = HttpReranker(cfg)

    # Empty docs -> []
    assert reranker.rerank("query", []) == []

    # Empty query -> raises RerankError
    with pytest.raises(RerankError, match="query must not be empty"):
        reranker.rerank("", ["doc"])


def test_http_reranker_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = RerankConfig(base_url="http://localhost:8000", model="m")
    reranker = HttpReranker(cfg)

    # 1. HTTP 500 error
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    monkeypatch.setattr("httpx.post", lambda *a, **kw: mock_resp)
    with pytest.raises(RerankError, match="HTTP 500"):
        reranker.rerank("q", ["doc"])

    # 2. Connection error
    def raise_conn_err(*a, **kw):
        raise httpx.ConnectError("failed to connect")

    monkeypatch.setattr("httpx.post", raise_conn_err)
    with pytest.raises(RerankError, match="HTTP error"):
        reranker.rerank("q", ["doc"])


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------


def test_load_rerank_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_RERANK_URL", "https://api.siliconflow.cn/v1/rerank")
    monkeypatch.setenv("KB_MCP_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setenv("KB_MCP_RERANK_API_KEY", "sk-custom")

    cfg = load_rerank_config()
    assert cfg is not None
    assert cfg.base_url == "https://api.siliconflow.cn/v1/rerank"
    assert cfg.model == "BAAI/bge-reranker-v2-m3"
    assert cfg.api_key == "sk-custom"


def test_load_rerank_config_siliconflow_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_MCP_RERANK_URL", raising=False)
    monkeypatch.delenv("KB_MCP_RERANK_MODEL", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-siliconflow-key")

    cfg = load_rerank_config()
    assert cfg is not None
    assert cfg.base_url == "https://api.siliconflow.cn/v1/rerank"
    assert cfg.model == "BAAI/bge-reranker-v2-m3"
    assert cfg.api_key == "sk-siliconflow-key"


def test_load_rerank_config_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kb_mcp_lite.reranker._load_dotenv", lambda: None)
    monkeypatch.delenv("KB_MCP_RERANK_URL", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
rerank:
  url: "https://api.jina.ai/v1"
  model: "jina-reranker-v2"
  api_key: "jina-key-123"
  timeout: 45
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KB_MCP_RERANK_CONFIG", str(cfg_file))

    cfg = load_rerank_config()
    assert cfg is not None
    assert cfg.base_url == "https://api.jina.ai/v1"
    assert cfg.model == "jina-reranker-v2"
    assert cfg.api_key == "jina-key-123"
    assert cfg.timeout == 45.0


def test_make_reranker_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kb_mcp_lite.reranker.load_rerank_config", lambda: None)
    r = make_reranker()
    assert isinstance(r, NullReranker)
    assert not r.enabled


# ---------------------------------------------------------------------------
# SqliteStore.search(..., rerank=True) Integration
# ---------------------------------------------------------------------------


class MockReranker:
    """Mock reranker reversing results to prove re-sorting takes effect."""

    def __init__(self) -> None:
        self.enabled = True

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[RerankItem]:
        # Give higher score to later documents
        items = []
        for i in range(len(documents)):
            items.append(RerankItem(index=i, score=float(i * 10 + 1)))
        items.sort(key=lambda x: x.score, reverse=True)
        if top_n:
            items = items[:top_n]
        return items


def test_store_search_with_rerank(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "test_rerank.db")

    store.add(
        Document(
            id="doc1",
            type="lesson",
            title="Overview of Redis",
            body="Redis is an in-memory key-value store with caching capabilities.",
        )
    )
    store.add(
        Document(
            id="doc2",
            type="lesson",
            title="PostgreSQL Full Text",
            body="PostgreSQL provides rich indexing and relational querying.",
        )
    )
    store.add(
        Document(
            id="doc3",
            type="lesson",
            title="Redis Cluster Failover",
            body="How to handle Redis sentinel and cluster topology changes.",
        )
    )

    mock_rrk = MockReranker()

    # Search with rerank enabled
    hits = store.search("Redis", mode="lexical", rerank=True, reranker=mock_rrk, limit=2)
    assert len(hits) == 2

    # Verify that mock reranker scores were applied
    assert hits[0].score > hits[1].score
    store.close()


def test_cli_search_with_rerank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner
    from kb_mcp_lite.cli import cli

    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    store = SqliteStore(tmp_path / "default" / "kb.db")
    store.add(Document(id="doc1", type="lesson", title="Redis Cache", body="Cache tips"))
    store.close()

    runner = CliRunner()
    res = runner.invoke(cli, ["search", "Redis", "--rerank", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert len(data) == 1
    assert data[0]["doc"]["id"] == "doc1"


def test_mcp_search_with_rerank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kb_mcp_lite.mcp_server import _make_server

    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    vm = VaultManager(tmp_path)
    store = SqliteStore(vm.resolve_path("default"))
    store.add(Document(id="doc1", type="lesson", title="Redis Cache", body="Cache tips"))
    store.close()

    mcp = _make_server()
    res = mcp._tool_manager._tools["kb_search"].fn(query="Redis", rerank=True)
    assert res["count"] == 1
    assert res["hits"][0]["id"] == "doc1"
