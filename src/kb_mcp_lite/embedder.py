"""Embedding client for kb-mcp v0.2.0 (Phase D).

Reads the same ``auxiliary.embedding`` block that Hermes' main config
already uses (``~/.hermes/config.yaml``), then talks to whatever
OpenAI-compatible ``/v1/embeddings`` endpoint the user has configured.

Design goals
------------

* **Generic, not MiniMax-locked.** Any provider that exposes an
  OpenAI-style ``POST {base_url}/v1/embeddings`` body
  ``{"input": str, "model": str}`` returning
  ``{"data": [{"embedding": [float, ...]}]}`` is supported. This
  includes OpenAI, MiniMax (when they expose embeddings), vLLM, Ollama
  with the OpenAI shim, BGE self-hosted, etc.
* **No background work, no auto-build.** Vectors are computed lazily on
  ``add`` / ``update`` (if the embedder is configured) or explicitly
  via ``kb embed`` (CLI). On a cold start with no embedder configured,
  semantic mode degrades to lexical.
* **Configurable override path.** ``KB_MCP_EMBEDDING_CONFIG`` env var
  points to a YAML file; falls back to
  ``~/.hermes/shared/kb_mcp_lite.yaml`` then ``~/.hermes/config.yaml``.

Failure modes
-------------

* Missing config -> :class:`NullEmbedder` (no-op, dimensions = 0)
* HTTP 4xx/5xx -> :class:`EmbeddingError`
* Empty ``data`` list -> :class:`EmbeddingError`
* Wrong-dim vector returned -> :class:`EmbeddingError`
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("kb_mcp_lite.embedder")


class EmbeddingError(Exception):
    """Raised when the embedder cannot produce a vector."""


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingConfig:
    """The minimum needed to call an OpenAI-compatible embeddings API.

    ``api_key`` may be empty if the server is local (Ollama, vLLM with
    no auth).
    """

    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 30.0

    @property
    def endpoint(self) -> str:
        """The full URL to POST to. Strips any trailing slash from base_url."""
        import re

        base = self.base_url.rstrip("/")
        # If the user has already specified /embeddings in the base_url, use it directly!
        if base.endswith("/embeddings"):
            return base
        # If the base URL ends with a version suffix (like /v1, /v3) or ends with /api, we just append /embeddings.
        if (
            re.search(r"/v\d+(\.\d+)?$", base, re.IGNORECASE)
            or base.endswith("/api")
            or base.endswith("/v1")
        ):
            return f"{base}/embeddings"
        return f"{base}/v1/embeddings"


def _load_yaml(path: Path) -> dict:
    """Best-effort YAML loader without requiring PyYAML.

    Returns an empty dict on parse failure (the caller treats empty dict
    as "no config"). Uses Python's stdlib only — we don't want a hard
    dep on PyYAML just for the kb-mcp CLI.
    """
    if not path.exists():
        return {}
    try:
        # Lazy import so the kb-mcp core can still ship without PyYAML.
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed; cannot parse %s", path)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to parse %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _extract_embedding_block(d: dict) -> dict | None:
    """Find the embedding config inside a parsed YAML.

    Looks in three places, in order:

    1. ``embedding.*``  — kb-mcp native config.
    2. ``auxiliary.embedding.*``  — Hermes' main config layout.
    3. ``kb_mcp_lite.embedding.*`` — flat top-level override.
    """
    if isinstance(d.get("embedding"), dict):
        return d["embedding"]
    aux = d.get("auxiliary") or {}
    if isinstance(aux, dict) and isinstance(aux.get("embedding"), dict):
        return aux["embedding"]
    kb = d.get("kb_mcp_lite") or {}
    if isinstance(kb, dict) and isinstance(kb.get("embedding"), dict):
        return kb["embedding"]
    return None


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` and ``$VAR`` references in ``value``.

    Matches Hermes' own config convention for credential indirection
    (keeps secrets out of config.yaml, in .env instead). Unknown
    variables are left as-is so misconfigurations are visible.
    """
    import re

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return os.environ.get(name, m.group(0))

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", _sub, value)


def _load_dotenv() -> None:
    """Load ``~/.hermes/profiles/default/.env`` into ``os.environ``.

    Hermes stores API keys here; the MCP server process inherits them
    via Hermes' own env injection, but when kb-mcp is invoked as a
    standalone CLI (``kb search ...``) the .env is not auto-loaded.
    This function bridges that gap for the standalone case.
    """
    for path in (
        Path.home() / ".hermes" / "profiles" / "default" / ".env",
        Path.home() / ".hermes" / ".env",
    ):
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    # Always overwrite: Hermes may have injected a
                    # redacted "***" placeholder into os.environ for
                    # security; the .env file has the real value.
                    os.environ[key] = val
        except Exception as e:
            logger.debug("failed to load %s: %s", path, e)
        # First file that exists wins (default profile takes priority).
        break


def load_embedding_config() -> EmbeddingConfig | None:
    """Resolve the embedding config in priority order.

    1. ``KB_MCP_EMBEDDING_CONFIG`` env var (path to a YAML file).
    2. ``KB_MCP_CONFIG`` env var → ``~/.config/kb-mcp/config.yaml``
       (kb-mcp native config file — ``embedding:`` key).
    3. ``~/.hermes/shared/kb_mcp_lite.yaml`` (shared override).
    4. ``~/.hermes/config.yaml`` (Hermes compatibility).

    ``api_key`` values of the form ``${VAR}`` are expanded against
    environment variables so secrets can live in ``.env`` rather than
    config.yaml.
    """
    # Load Hermes' .env if present, so ${VAR} references resolve.
    _load_dotenv()
    candidates: list[Path] = []
    env = os.environ.get("KB_MCP_EMBEDDING_CONFIG")
    if env:
        candidates.append(Path(env))
    # kb-mcp native config (~/.config/kb-mcp/config.yaml)
    from kb_mcp_lite.config import config_path as _kb_config_path

    candidates.append(_kb_config_path())
    candidates.append(Path.home() / ".hermes" / "shared" / "kb_mcp_lite.yaml")
    candidates.append(Path.home() / ".hermes" / "config.yaml")

    for path in candidates:
        if not path.exists():
            continue
        d = _load_yaml(path)
        block = _extract_embedding_block(d)
        if not block:
            continue
        base_url = _expand_env((block.get("base_url") or block.get("url") or "").strip())
        model = (block.get("model") or "").strip()
        if not base_url or not model:
            logger.debug("%s: embedding block present but missing base_url/model", path)
            continue
        return EmbeddingConfig(
            base_url=base_url,
            model=model,
            api_key=_expand_env((block.get("api_key") or "").strip()),
            timeout=float(block.get("timeout") or 30.0),
        )
    return None


# ---------------------------------------------------------------------------
# Embedder protocol + implementations
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    """Anything that turns a string into a fixed-size float vector."""

    @property
    def dim(self) -> int:
        """Vector dimension. ``0`` means embedder is disabled."""
        ...

    @property
    def enabled(self) -> bool:
        """True if this embedder produces real vectors."""
        ...

    def embed(self, text: str) -> list[float]:
        """Return a list of ``dim`` floats for ``text``.

        Raises:
            EmbeddingError: on HTTP / API / dimension errors.
        """
        ...


class NullEmbedder:
    """A no-op embedder. ``dim=0`` signals "semantic mode unavailable"."""

    dim: int = 0

    @property
    def enabled(self) -> bool:
        return False

    def embed(self, text: str) -> list[float]:
        raise EmbeddingError("NullEmbedder cannot produce vectors")


class HttpEmbedder:
    """OpenAI-compatible ``/v1/embeddings`` client.

    The :class:`httpx` import is lazy so that kb-mcp without HTTP deps
    (e.g. a unit-test sandbox) can still construct a :class:`NullEmbedder`.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        # Probe dim on first call; we don't hard-code it so the same
        # client works for any model (384 / 768 / 1024 / 1536 / 3072 / ...).
        self._dim: int = 0

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def enabled(self) -> bool:
        return True

    def embed(self, text: str) -> list[float]:
        if not text:
            raise EmbeddingError("cannot embed empty text")
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        body = {
            "input": text,
            "model": self._config.model,
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
            raise EmbeddingError(f"HTTP error calling {url}: {e}") from e

        if resp.status_code != 200:
            raise EmbeddingError(f"embeddings API returned {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            vector = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise EmbeddingError(f"unexpected response shape: {e}") from e

        if not isinstance(vector, list) or not all(isinstance(x, (int, float)) for x in vector):
            raise EmbeddingError("embedding is not a list of numbers")

        if self._dim == 0:
            self._dim = len(vector)
        elif len(vector) != self._dim:
            raise EmbeddingError(f"dimension mismatch: expected {self._dim}, got {len(vector)}")
        return [float(x) for x in vector]


def make_embedder() -> Embedder:
    """Build the best available embedder for this process.

    Priority:
    1. ``HttpEmbedder`` if embedding config is found and HTTP deps are
       present.
    2. :class:`NullEmbedder` otherwise (semantic search will degrade
       to lexical; the rest of the KB is unaffected).
    """
    cfg = load_embedding_config()
    if cfg is None:
        return NullEmbedder()
    try:
        return HttpEmbedder(cfg)
    except ImportError:
        logger.warning(
            "httpx is required for HttpEmbedder; install kb-mcp[vec] or "
            "`pip install httpx` to enable semantic search"
        )
        return NullEmbedder()


__all__ = [
    "EmbeddingConfig",
    "EmbeddingError",
    "Embedder",
    "NullEmbedder",
    "HttpEmbedder",
    "load_embedding_config",
    "make_embedder",
]
