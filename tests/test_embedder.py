"""Tests for the OpenAI-compatible embedder (Phase D of v0.2).

Covers:
* ``NullEmbedder`` is the default when no config is found.
* ``HttpEmbedder`` posts the right body to ``{base_url}/v1/embeddings``
  and parses the OpenAI-shaped response.
* ``load_embedding_config`` honours the priority order
  ``KB_MCP_EMBEDDING_CONFIG`` → ``~/.hermes/shared/kb_mcp_lite.yaml`` →
  ``~/.hermes/config.yaml``.
* The ``Embedder`` is best-effort on errors: a 5xx response raises
  :class:`EmbeddingError`, but the call site in SqliteStore catches
  it (tested separately).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kb_mcp_lite.embedder import (
    EmbeddingConfig,
    EmbeddingError,
    HttpEmbedder,
    NullEmbedder,
    load_embedding_config,
)


# ---------------------------------------------------------------------------
# NullEmbedder
# ---------------------------------------------------------------------------


def test_null_embedder_is_disabled() -> None:
    e = NullEmbedder()
    assert e.enabled is False
    assert e.dim == 0
    with pytest.raises(EmbeddingError):
        e.embed("anything")


# ---------------------------------------------------------------------------
# HttpEmbedder happy path (uses httpx.MockTransport)
# ---------------------------------------------------------------------------


def test_http_embedder_calls_embeddings_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify URL path, body shape, and response parsing."""
    import httpx
    import json as _json

    captured: dict[str, Any] = {}

    def fake_post(url: str, *, headers: dict, content: str, timeout: float) -> httpx.Response:
        captured["url"] = url
        captured["body"] = _json.loads(content)
        captured["headers"] = headers
        captured["timeout"] = timeout
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3] * 512}]})

    monkeypatch.setattr(httpx, "post", fake_post)

    cfg = EmbeddingConfig(
        base_url="https://api.example.com/v1",
        model="test-embed",
        api_key="sk-test",
    )
    emb = HttpEmbedder(cfg)
    vec = emb.embed("hello world")
    assert len(vec) == 1536
    assert emb.dim == 1536
    assert captured["url"] == "https://api.example.com/v1/embeddings"
    assert "Bearer sk-test" in captured["headers"].get("Authorization", "")
    assert captured["body"]["model"] == "test-embed"
    assert captured["body"]["input"] == "hello world"


def test_http_embedder_handles_missing_v1_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """base_url without trailing /v1 should still hit /v1/embeddings."""
    import httpx

    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    cfg = EmbeddingConfig(base_url="https://api.example.com", model="x", api_key="k")
    emb = HttpEmbedder(cfg)
    emb.embed("x")
    assert captured["url"] == "https://api.example.com/v1/embeddings"


def test_http_embedder_raises_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(500, text="oops"))
    cfg = EmbeddingConfig(base_url="https://api.example.com", model="x", api_key="k")
    emb = HttpEmbedder(cfg)
    with pytest.raises(EmbeddingError) as exc:
        emb.embed("x")
    assert "500" in str(exc.value)


def test_http_embedder_raises_on_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={"data": []}))
    cfg = EmbeddingConfig(base_url="https://api.example.com", model="x", api_key="k")
    emb = HttpEmbedder(cfg)
    with pytest.raises(EmbeddingError):
        emb.embed("x")


def test_http_embedder_raises_on_dimension_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a dim is established, a different-length vector is an error."""
    import httpx

    responses = iter(
        [
            httpx.Response(200, json={"data": [{"embedding": [0.0] * 4}]}),
            httpx.Response(200, json={"data": [{"embedding": [0.0] * 8}]}),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: next(responses))
    cfg = EmbeddingConfig(base_url="https://api.example.com", model="x", api_key="k")
    emb = HttpEmbedder(cfg)
    emb.embed("first")
    with pytest.raises(EmbeddingError) as exc:
        emb.embed("second")
    assert "dimension" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_embedding_config_returns_none_when_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point HOME at an empty dir so neither default config exists.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KB_MCP_EMBEDDING_CONFIG", raising=False)
    assert load_embedding_config() is None


def test_load_embedding_config_reads_hermes_config_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KB_MCP_EMBEDDING_CONFIG", raising=False)
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "auxiliary:\n"
        "  embedding:\n"
        "    base_url: https://api.example.com/v1\n"
        "    model: text-embed-3-small\n"
        "    api_key: sk-xyz\n",
        encoding="utf-8",
    )
    cfg = load_embedding_config()
    assert cfg is not None
    assert cfg.base_url == "https://api.example.com/v1"
    assert cfg.model == "text-embed-3-small"
    assert cfg.api_key == "sk-xyz"


def test_load_embedding_config_shared_overrides_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared override file wins over ~/.hermes/config.yaml."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KB_MCP_EMBEDDING_CONFIG", raising=False)
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "auxiliary:\n  embedding:\n    base_url: https://api.A.com\n    model: model-A\n",
        encoding="utf-8",
    )
    (tmp_path / ".hermes" / "shared").mkdir()
    (tmp_path / ".hermes" / "shared" / "kb_mcp_lite.yaml").write_text(
        "auxiliary:\n  embedding:\n    base_url: https://api.B.com\n    model: model-B\n",
        encoding="utf-8",
    )
    cfg = load_embedding_config()
    assert cfg is not None
    assert cfg.base_url == "https://api.B.com"
    assert cfg.model == "model-B"


def test_load_embedding_config_env_var_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "auxiliary:\n  embedding:\n    base_url: https://override.com\n    model: m",
        encoding="utf-8",
    )
    monkeypatch.setenv("KB_MCP_EMBEDDING_CONFIG", str(custom))
    monkeypatch.setenv("HOME", str(tmp_path))  # no .hermes dir
    cfg = load_embedding_config()
    assert cfg is not None
    assert cfg.base_url == "https://override.com"
    assert cfg.model == "m"
