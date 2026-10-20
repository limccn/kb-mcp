"""Reranker client for kb-mcp RAG pipeline.

Supports Cross-Encoder reranking via any compatible HTTP API:
- SiliconFlow (e.g. BAAI/bge-reranker-v2-m3)
- Cohere (e.g. rerank-v3.5)
- Jina AI (e.g. jina-reranker-v2-base-multilingual)
- Voyage AI
- Self-hosted vLLM / TEI (Text Embeddings Inference) / Ollama / Local reranker APIs

Design goals
------------
- **Provider-agnostic**: Talks to standard POST /v1/rerank or /rerank endpoints.
- **Graceful Degradation**: If no reranker is configured or HTTP request fails,
  gracefully degrades to NullReranker without breaking core search.
- **Configurable**: Loaded from `~/.config/kb-mcp/config.yaml`, env vars, or Hermes config.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("kb_mcp_lite.reranker")


class RerankError(Exception):
    """Raised when the reranker cannot score documents."""


@dataclass(frozen=True)
class RerankItem:
    """A scored document item returned from reranking."""

    index: int
    score: float
    document: str | None = None


@dataclass(frozen=True)
class RerankConfig:
    """Configuration for calling a reranking HTTP API."""

    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 30.0
    provider: str = "generic"

    @property
    def endpoint(self) -> str:
        """The full URL to POST to. Strips trailing slashes and normalizes path."""
        base = self.base_url.rstrip("/")
        if base.endswith("/rerank"):
            return base
        if (
            re.search(r"/v\d+(\.\d+)?$", base, re.IGNORECASE)
            or base.endswith("/api")
            or base.endswith("/v1")
        ):
            return f"{base}/rerank"
        return f"{base}/v1/rerank"


def _load_yaml(path: Path) -> dict:
    """Best-effort YAML loader without hard-requiring PyYAML."""
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("Failed to parse %s: %s", path, e)
        return {}


def _extract_rerank_block(d: dict) -> dict | None:
    """Find the rerank config inside a parsed YAML dict."""
    if isinstance(d.get("rerank"), dict):
        return d["rerank"]
    aux = d.get("auxiliary") or {}
    if isinstance(aux, dict) and isinstance(aux.get("rerank"), dict):
        return aux["rerank"]
    kb = d.get("kb_mcp_lite") or {}
    if isinstance(kb, dict) and isinstance(kb.get("rerank"), dict):
        return kb["rerank"]
    return None


def _expand_env(value: str) -> str:
    """Expand ${VAR} and $VAR references."""

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return os.environ.get(name, m.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", _sub, value)


def _load_dotenv() -> None:
    """Load ~/.hermes/profiles/default/.env, ~/.hermes/.env, .env, ~/.zshrc, ~/.bashrc into os.environ."""
    for path in (
        Path.home() / ".hermes" / "profiles" / "default" / ".env",
        Path.home() / ".hermes" / ".env",
        Path.cwd() / ".env",
        Path.home() / ".zshrc",
        Path.home() / ".bashrc",
    ):
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception as e:
            logger.debug("failed to load %s: %s", path, e)


def load_rerank_config() -> RerankConfig | None:
    """Load rerank config from environment variables or YAML config files."""
    _load_dotenv()
    # 1. Check explicit environment variables
    env_url = os.environ.get("KB_MCP_RERANK_URL") or os.environ.get("KB_MCP_RERANK_BASE_URL")
    env_model = os.environ.get("KB_MCP_RERANK_MODEL")
    env_key = (
        os.environ.get("KB_MCP_RERANK_API_KEY")
        or os.environ.get("SILICONFLOW_API_KEY")
        or os.environ.get("COHERE_API_KEY")
        or os.environ.get("JINA_API_KEY")
        or ""
    )
    env_provider = os.environ.get("KB_MCP_RERANK_PROVIDER", "generic")

    # If SiliconFlow key is provided without URL/model, default to SiliconFlow's BGE reranker
    if os.environ.get("SILICONFLOW_API_KEY") and not env_url:
        env_url = "https://api.siliconflow.cn/v1/rerank"
        if not env_model:
            env_model = "BAAI/bge-reranker-v2-m3"

    # If Cohere key is provided without URL/model, default to Cohere API
    if os.environ.get("COHERE_API_KEY") and not env_url:
        env_url = "https://api.cohere.com/v2/rerank"
        if not env_model:
            env_model = "rerank-v3.5"

    # If Jina key is provided without URL/model, default to Jina API
    if os.environ.get("JINA_API_KEY") and not env_url:
        env_url = "https://api.jina.ai/v1/rerank"
        if not env_model:
            env_model = "jina-reranker-v2-base-multilingual"

    if env_url and env_model:
        return RerankConfig(
            base_url=env_url,
            model=env_model,
            api_key=env_key,
            provider=env_provider,
        )

    # 2. Check config file candidates
    candidates = []
    override = os.environ.get("KB_MCP_RERANK_CONFIG") or os.environ.get("KB_MCP_CONFIG")
    if override:
        candidates.append(Path(override))

    candidates.extend(
        [
            Path.home() / ".config" / "kb-mcp" / "config.yaml",
            Path.home() / ".hermes" / "shared" / "kb_mcp_lite.yaml",
            Path.home() / ".hermes" / "config.yaml",
        ]
    )

    for path in candidates:
        if not path.exists():
            continue
        d = _load_yaml(path)
        block = _extract_rerank_block(d)
        if not block:
            continue

        raw_url = block.get("url") or block.get("base_url")
        raw_model = block.get("model")
        raw_key = block.get("api_key") or ""
        raw_timeout = block.get("timeout", 30.0)
        raw_provider = block.get("provider", "generic")

        if not raw_url or not raw_model:
            logger.warning("Rerank config at %s is missing 'url' or 'model'", path)
            continue

        url = _expand_env(str(raw_url))
        model = _expand_env(str(raw_model))
        key = _expand_env(str(raw_key))

        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = 30.0

        return RerankConfig(
            base_url=url,
            model=model,
            api_key=key,
            timeout=timeout,
            provider=raw_provider,
        )

    return None


class Reranker(Protocol):
    """Protocol for reranking search results."""

    @property
    def enabled(self) -> bool:
        """True if this reranker is active and calls a real model."""
        ...

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[RerankItem]:
        """Score and sort documents against query in descending order of relevance."""
        ...


class NullReranker:
    """No-op reranker used when reranking is not configured or disabled."""

    @property
    def enabled(self) -> bool:
        return False

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[RerankItem]:
        # Return natural ordering with neutral score
        limit = top_n if top_n is not None else len(documents)
        return [
            RerankItem(index=i, score=0.0, document=doc) for i, doc in enumerate(documents[:limit])
        ]


class HttpReranker:
    """Cross-Encoder reranking client using standard HTTP API."""

    def __init__(self, config: RerankConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return self._config.model

    def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[RerankItem]:
        if not documents:
            return []
        if not query:
            raise RerankError("query must not be empty for reranking")

        import httpx

        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        actual_top_n = min(len(documents), top_n) if top_n is not None else len(documents)

        body: dict[str, Any] = {
            "model": self._config.model,
            "query": query,
            "documents": documents,
            "top_n": actual_top_n,
        }

        url = self._config.endpoint
        try:
            resp = httpx.post(
                url,
                headers=headers,
                content=json.dumps(body),
                timeout=self._config.timeout,
            )
        except httpx.HTTPError as e:
            raise RerankError(f"HTTP error calling reranker at {url}: {e}") from e

        if resp.status_code != 200:
            raise RerankError(f"Reranker API returned HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except Exception as e:
            raise RerankError(f"Failed to parse JSON response from reranker: {e}") from e

        # Normalize results format
        # Typical formats:
        # 1. {"results": [{"index": 0, "relevance_score": 0.98}, ...]}
        # 2. {"data": [{"index": 0, "score": 0.98}, ...]}
        # 3. {"results": [{"document": "...", "index": 0, "score": 0.98}, ...]}
        raw_results = data.get("results") or data.get("data")
        if not isinstance(raw_results, list):
            raise RerankError(f"Unexpected reranker response structure: {data}")

        items: list[RerankItem] = []
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            idx = r.get("index")
            if idx is None or not isinstance(idx, int):
                continue
            score = r.get("relevance_score")
            if score is None:
                score = r.get("score", 0.0)
            try:
                fscore = float(score)
            except (TypeError, ValueError):
                fscore = 0.0

            doc_text = documents[idx] if 0 <= idx < len(documents) else None
            items.append(RerankItem(index=idx, score=fscore, document=doc_text))

        # Sort descending by score
        items.sort(key=lambda x: x.score, reverse=True)
        if top_n is not None:
            items = items[:top_n]
        return items


def make_reranker(config: RerankConfig | None = None) -> Reranker:
    """Factory creating HttpReranker if config exists, otherwise NullReranker."""
    cfg = config or load_rerank_config()
    if cfg is None:
        return NullReranker()
    try:
        return HttpReranker(cfg)
    except Exception as e:
        logger.warning("Failed to initialize HttpReranker: %s. Falling back to NullReranker", e)
        return NullReranker()


__all__ = [
    "RerankConfig",
    "RerankError",
    "RerankItem",
    "Reranker",
    "NullReranker",
    "HttpReranker",
    "load_rerank_config",
    "make_reranker",
]
