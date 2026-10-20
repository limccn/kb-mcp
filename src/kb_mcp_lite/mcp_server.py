"""FastMCP server for kb-mcp.

Exposes tools, Resources, and Prompts over stdio transport:

**Tools (17):** kb_search, kb_get, kb_add, kb_link, kb_list, kb_update,
kb_delete, kb_unlink, kb_history, kb_restore, kb_diff, kb_restore_deleted,
kb_doctor, kb_similar, kb_duplicates, kb_diff_check, kb_query_relations

**Resources (13):** kb://doc/{id}, kb://links/{id},
kb://types, kb://stats, kb://graph/{id}[/{depth}],
kb://list[/{type}], kb://changes, kb://history/{id},
kb://search/{query}, kb://export/{id}, kb://help/{doc}

**Prompts (7):** new-doc(type), link-analysis(id), search-guide, import-docs,
doctor, maintenance, onboarding

Error codes:

| kb-mcp exception   | MCP code | Meaning          |
|--------------------|----------|------------------|
| ValidationError    | -32602   | Invalid params   |
| NotFoundError      | -32004   |                  |
| DuplicateError     | -32005   |                  |
| IntegrityError     | -32603   | Internal error   |
| Other              | -32603   | Internal error   |

Logging: structured JSON to stderr only; body content is never logged
(privacy NFR-O-2).
"""

# NOTE: We intentionally do NOT use `from __future__ import annotations` here.
# FastMCP 1.12's Tool.from_function() calls issubclass() on parameter
# annotations at runtime. PEP-563 (postponed annotations) would turn
# every annotation into a string, causing issubclass() to crash with
# "TypeError: issubclass() arg 1 must be a class".

import hashlib
import hmac
import json
import logging
import os
import sys
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from kb_mcp_lite.md_io import render_document
from kb_mcp_lite.schema import (
    Document,
    DuplicateError,
    IntegrityError,
    KbMcpError,
    NotFoundError,
    SearchHit,
    ValidationError,
    make_id,
)
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import VaultManager

# ---------------------------------------------------------------------------
# Pydantic input models (architecture.md § 4.4)
# ---------------------------------------------------------------------------


class KbSearchInput(BaseModel):
    query: str = Field(min_length=1)
    type: str | None = None
    tags: list[str] | None = None
    limit: int = Field(default=10, ge=1, le=100)
    mode: str = Field(default="hybrid", pattern="^(lexical|fuzzy|semantic|hybrid|rrf)$")
    rrf_k: int = Field(default=60, ge=1, le=200)
    rerank: bool = Field(default=False, description="Apply Cross-Encoder reranking")
    vault: str | None = Field(default=None, description="Target vault name or '*' for all vaults")


class KbGetInput(BaseModel):
    id: str = Field(min_length=1)
    section: str | None = Field(
        default=None, description="Optional heading / section to extract from body"
    )
    vault: str | None = Field(default=None, description="Optional target vault name")


class KbAddInput(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=1_000_000)
    tags: list[str] | None = None
    aliases: list[str] | None = None
    metadata: dict[str, Any] | None = None
    source: str | None = None
    id: str | None = Field(default=None, min_length=1, max_length=512)


class KbLinkInput(BaseModel):
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    rel: str = Field(default="relates-to", min_length=1, max_length=64)


class KbListInput(BaseModel):
    type: str | None = None
    tags: list[str] | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    include_deleted: bool = False
    vault: str | None = Field(default=None, description="Optional target vault name")


class KbUpdateInput(BaseModel):
    id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    body: str | None = Field(default=None, max_length=1_000_000)
    tags: list[str] | None = None
    aliases: list[str] | None = None
    metadata: dict[str, Any] | None = None
    source: str | None = None


class KbDeleteInput(BaseModel):
    id: str = Field(min_length=1)


class KbUnlinkInput(BaseModel):
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    rel: str | None = Field(default=None, min_length=1, max_length=64)


class KbSimilarInput(BaseModel):
    id: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=100)


class KbDuplicatesInput(BaseModel):
    threshold: float = Field(default=0.15, ge=0.0, le=2.0)
    limit: int = Field(default=50, ge=1, le=500)


class KbDiffCheckInput(BaseModel):
    cwd: str | None = Field(default=None, description="Optional repository working directory")
    vault: str | None = Field(default=None, description="Optional target vault name")


class KbGraphQueryInput(BaseModel):
    start_id: str = Field(min_length=1)
    target_id: str | None = Field(
        default=None, description="Optional target id to find shortest path to"
    )
    rel: str | None = Field(default=None, description="Filter by relation type")
    direction: str = Field(default="outbound", pattern="^(outbound|inbound|both)$")
    doc_type: str | None = Field(default=None, description="Filter reached documents by type")
    max_depth: int = Field(default=2, ge=1, le=5)
    vault: str | None = Field(default=None, description="Optional target vault name")


class KbImpactInput(BaseModel):
    doc_id: str = Field(min_length=1)
    max_depth: int = Field(default=3, ge=1, le=5)
    max_results: int = Field(default=50, ge=1, le=200)


class KbDecisionChainInput(BaseModel):
    decision_id: str = Field(min_length=1)


class KbRelSpecInput(BaseModel):
    rel_name: str | None = Field(default=None, description="Relation name; omit to list all")


class KbExpandInput(BaseModel):
    doc_id: str = Field(min_length=1)
    depth: int = Field(default=1, ge=1, le=3)
    max_neighbors: int = Field(default=10, ge=1, le=50)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _mcp_error(exc: Exception) -> tuple[int, str]:
    """Map kb-mcp exceptions to MCP JSON-RPC error codes.

    Returns (code, message).
    """
    if isinstance(exc, ValidationError):
        return -32602, str(exc)
    if isinstance(exc, NotFoundError):
        return -32004, str(exc)
    if isinstance(exc, DuplicateError):
        return -32005, str(exc)
    if isinstance(exc, IntegrityError):
        return -32603, str(exc)
    return -32603, f"internal error: {exc}"


# ---------------------------------------------------------------------------
# Structured JSON logging to stderr
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON to stderr."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging(level: str | None = None) -> logging.Logger:
    """Configure structured JSON logging to stderr.

    Body content is never logged (privacy).
    """
    log_level = (level or os.environ.get("KB_MCP_LOG_LEVEL", "WARNING")).upper()
    logger = logging.getLogger("kb_mcp_lite")
    logger.setLevel(getattr(logging, log_level, logging.WARNING))

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logger.handlers.clear()
    logger.addHandler(handler)

    return logger


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_DOC_TEMPLATES: dict[str, str] = {
    "project": (
        "# {title}\n\n"
        "## Purpose\n\n"
        "(What does this project do? Why does it exist?)\n\n"
        "## Stack\n\n"
        "- Language:\n- Framework:\n- Database:\n- Infrastructure:\n\n"
        "## Status\n\n"
        "(active / maintenance / archived)\n\n"
        "## Owners\n\n"
        "(who maintains this)\n\n"
        "## Links\n\n"
        "- Related docs: \n"
    ),
    "decision": (
        "# {title}\n\n"
        "## Context\n\n"
        "(What prompted this decision? What problem does it solve?)\n\n"
        "## Options Considered\n\n"
        "- Option A: \n- Option B: \n\n"
        "## Decision\n\n"
        "(Chosen option and why)\n\n"
        "## Consequences\n\n"
        "(What does this decision affect? Any follow-up work?)\n\n"
    ),
    "lesson": (
        "# {title}\n\n"
        "## What Happened\n\n"
        "(Description of the situation)\n\n"
        "## Root Cause\n\n"
        "(Why did it happen?)\n\n"
        "## Resolution\n\n"
        "(How was it fixed or mitigated?)\n\n"
        "## Prevention\n\n"
        "(How to avoid this in the future)\n\n"
    ),
    "glossary": (
        "# {title}\n\n"
        "## Definition\n\n"
        "(One-sentence definition of the term)\n\n"
        "## Details\n\n"
        "(Elaboration, examples, or context)\n\n"
        "## Related Terms\n\n"
        "- \n\n"
    ),
    "person": (
        "# {title}\n\n"
        "## Role\n\n"
        "(Title / responsibility)\n\n"
        "## Expertise\n\n"
        "- \n\n"
        "## Projects\n\n"
        "- \n\n"
    ),
    "faq": (
        "# {title}\n\n## Answer\n\n(Concise answer to the question)\n\n## References\n\n- \n\n"
    ),
    "api": (
        "# {title}\n\n"
        "## Endpoint\n\n"
        "(Path and HTTP method)\n\n"
        "## Request\n\n"
        "(Parameters, headers, and request body)\n\n"
        "## Response\n\n"
        "(Success and error response shapes)\n\n"
        "## Auth & Errors\n\n"
        "(Authentication, rate limits, and error codes)\n\n"
    ),
    "runbook": (
        "# {title}\n\n"
        "## Trigger\n\n"
        "(When to run this procedure)\n\n"
        "## Prerequisites\n\n"
        "- \n\n"
        "## Steps\n\n"
        "1. \n2. \n3. \n\n"
        "## Verification\n\n"
        "(How to confirm success)\n\n"
        "## Rollback\n\n"
        "(What to do if something fails)\n\n"
    ),
    "release": (
        "# {title}\n\n"
        "## Version & Date\n\n"
        "(Version number and release date)\n\n"
        "## Changes\n\n"
        "- \n\n"
        "## Impact\n\n"
        "(Breaking changes, migrations, affected users)\n\n"
        "## Rollback\n\n"
        "(How to revert if needed)\n\n"
    ),
}

_DEFAULT_DOC_TEMPLATE = _DOC_TEMPLATES["decision"]
_HELP_DOCS = ("architecture", "cli-reference", "quickstart")


# ---------------------------------------------------------------------------
# Store factory
# ---------------------------------------------------------------------------


def _create_store(vault: str | None = None) -> SqliteStore:
    """Return a :class:`SqliteStore` for the given (or current) vault.

    The DB path is resolved via :class:`VaultManager`.
    """
    mgr = VaultManager()
    db_path = mgr.resolve_path(vault)
    return SqliteStore(db_path)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------


# ``mcp`` 1.x rewrites ``{x}`` into ``[^/]+`` in ``ResourceTemplate.matches``: a
# template parameter can never span a slash, and whatever it captures is never
# percent-decoded. kb ids are ``type/slug``, so the shape ``kb_add`` generates by
# default matches no template at all, and the encoded form that does match
# arrives in the handler with its ``%2F`` intact. Both halves are handled here:
# normalise the URI before matching, and unquote once inside each handler.
#
# Path segments each template's parameters occupy, keyed by the URI authority
# (``kb://<authority>/...``). ``kb://graph`` has two overlapping templates and
# is disambiguated separately.
_KB_PARAM_SEGMENTS = {
    "doc": 1,
    "links": 1,
    "history": 1,
    "export": 1,
    "search": 1,
    "help": 1,
    "list": 1,
}


def _merge_segments(segments: list[str]) -> str:
    """Collapse path segments into one template parameter, percent-encoded."""
    return quote("/".join(unquote(s) for s in segments), safe="")


def _is_graph_depth(segment: str) -> bool:
    """Is ``segment`` a usable ``kb://graph/{id}/{depth}`` depth (1..8)?"""
    try:
        return 1 <= int(unquote(segment)) <= 8
    except ValueError:
        return False


def _normalize_resource_uri(uri: str) -> str:
    """Rewrite a ``kb://`` URI so the SDK's resource templates can match it.

    ``kb://doc/dec/e2e-harness`` (the id shape ``kb_add`` generates) carries more
    path segments than ``kb://doc/{id}`` has parameters, so it matches no
    template. The surplus tail is re-joined into the last parameter and
    percent-encoded — the only form the matcher accepts; each handler then
    decodes it once. A URI whose segment count already equals the template
    arity is returned unchanged, because re-encoding it would double-encode an
    already-encoded id.

    ``kb://graph`` is ambiguous (``{id}`` and ``{id}/{depth}`` share a prefix),
    so the tail decides: a parseable depth uses the depth template, anything
    else is part of the id and the default depth applies.

    A URI that needs re-joining is rebuilt as ``kb://<authority>/<merged>``; a
    ``?query`` or ``#fragment`` on it is dropped (no kb template uses one).
    """
    split = urlsplit(uri)
    if split.scheme != "kb" or not split.netloc:
        return uri

    segments = split.path.split("/")[1:] if split.path.startswith("/") else []

    if split.netloc == "graph":
        if len(segments) >= 2 and _is_graph_depth(segments[-1]):
            return f"kb://graph/{_merge_segments(segments[:-1])}/{segments[-1]}"
        if len(segments) >= 2:
            return f"kb://graph/{_merge_segments(segments)}"
        return uri

    expected = _KB_PARAM_SEGMENTS.get(split.netloc)
    if expected is None or len(segments) <= expected:
        return uri
    return f"kb://{split.netloc}/{_merge_segments(segments)}"


def _make_server(vault: str | None = None) -> Any:
    """Build and return a FastMCP instance with kb tools registered.

    Args:
        vault: Optional vault name. Defaults to the current active vault.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("mcp package not installed; run: pip install mcp") from e

    class _KbFastMCP(FastMCP):
        """FastMCP that normalises ``kb://`` URIs before template matching.

        ``FastMCP._setup_handlers`` registers this very method with the
        low-level server, so overriding it intercepts every ``resources/read``
        before ``ResourceTemplate.matches`` sees the URI.
        """

        async def read_resource(self, uri: Any) -> Any:
            return await super().read_resource(_normalize_resource_uri(str(uri)))

    mcp = _KbFastMCP("kb-mcp")
    store = _create_store(vault)
    logger = _setup_logging()

    # ---- kb_search --------------------------------------------------------

    @mcp.tool()
    def kb_search(
        query: str,
        type: str | None = None,  # noqa: A002
        tags: list[str] | None = None,
        limit: int = 10,
        mode: str = "hybrid",
        rrf_k: int = 60,
        rerank: bool = False,
        vault: str | None = None,
    ) -> Any:
        """Full-text search the knowledge base across current or specified vault(s).

        Args:
            query: Search query (non-empty).
            type: Restrict to a document type (optional).
            tags: Restrict to documents carrying all listed tags (AND, optional).
            limit: Max results 1..100 (default 10).
            mode: Scoring mode — 'lexical' (exact BM25), 'fuzzy'
                (trigram BM25, tolerates typos), 'hybrid' (default,
                reciprocal-rank fusion of lexical+fuzzy+semantic),
                'rrf' (same as hybrid), or 'semantic' (vectors).
            rrf_k: RRF constant (default 60). Lower = more weight on
                top ranks. Only used in hybrid/rrf mode.
            rerank: Apply Cross-Encoder reranking to rescale and re-order top results.
            vault: Target vault name, or '*' to search across all registered vaults.

        Returns:
            List of hit dicts: {id, title, type, snippet, score, vault}.
        """
        try:
            inp = KbSearchInput(
                query=query,
                type=type,
                tags=tags,
                limit=limit,
                mode=mode,
                rrf_k=rrf_k,
                rerank=rerank,
                vault=vault,
            )
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info(
            "kb_search query=%r type=%r tags=%r limit=%d mode=%r rrf_k=%d rerank=%s vault=%r",
            inp.query,
            inp.type,
            inp.tags,
            inp.limit,
            inp.mode,
            inp.rrf_k,
            inp.rerank,
            inp.vault,
        )
        try:
            mgr = VaultManager()
            if inp.vault == "*":
                # Search across all registered vaults
                all_hits: list[dict[str, Any]] = []
                for vinfo in mgr.list_vaults():
                    v_store = SqliteStore(mgr.resolve_path(vinfo.name))
                    try:
                        v_hits = v_store.search(
                            query=inp.query,
                            type=inp.type,
                            tags=inp.tags,
                            limit=inp.limit,
                            mode=inp.mode,
                            rrf_k=inp.rrf_k,
                            rerank=inp.rerank,
                        )
                        for h in v_hits:
                            all_hits.append(
                                {
                                    "id": h.doc.id,
                                    "title": h.doc.title,
                                    "type": h.doc.type,
                                    "snippet": h.snippet,
                                    "score": h.score,
                                    "vault": vinfo.name,
                                }
                            )
                    finally:
                        v_store.close()
                is_lexical_or_fuzzy = inp.mode in ("lexical", "fuzzy") and not inp.rerank
                all_hits.sort(key=lambda x: x["score"], reverse=(not is_lexical_or_fuzzy))
                return {
                    "hits": all_hits[: inp.limit],
                    "count": min(len(all_hits), inp.limit),
                }

            target_store = _create_store(inp.vault) if inp.vault else store
            try:
                hits: list[SearchHit] = target_store.search(
                    query=inp.query,
                    type=inp.type,
                    tags=inp.tags,
                    limit=inp.limit,
                    mode=inp.mode,
                    rrf_k=inp.rrf_k,
                    rerank=inp.rerank,
                )
                v_name = inp.vault or mgr.get_current()
                return {
                    "hits": [
                        {
                            "id": h.doc.id,
                            "title": h.doc.title,
                            "type": h.doc.type,
                            "snippet": h.snippet,
                            "score": h.score,
                            "vault": v_name,
                        }
                        for h in hits
                    ],
                    "count": len(hits),
                }
            finally:
                if inp.vault:
                    target_store.close()
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_search failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_get -----------------------------------------------------------

    @mcp.tool()
    def kb_get(
        id: str,
        section: str | None = None,
        vault: str | None = None,
    ) -> Any:
        """Fetch a document by id, with optional section-level extraction and vault selection.

        Args:
            id: Document id (slug, e.g. "proj/kb-mcp").
            section: Optional heading name to extract just that specific section
                (reduces token consumption for large docs).
            vault: Optional vault name to fetch from.

        Returns:
            Full document dict (all fields) or sliced section result.
        """
        try:
            inp = KbGetInput(id=id, section=section, vault=vault)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_get id=%r section=%r vault=%r", inp.id, inp.section, inp.vault)
        try:
            target_store = _create_store(inp.vault) if inp.vault else store
            try:
                doc = target_store.get(inp.id)
                dumped = doc.model_dump(mode="json")
                if inp.section:
                    section_content = doc.get_section(inp.section)
                    if section_content is None:
                        available = list(doc.extract_sections().keys())
                        dumped["section_found"] = False
                        dumped["available_sections"] = [s for s in available if s]
                        dumped["section_error"] = f"Section {inp.section!r} not found."
                    else:
                        dumped["section_found"] = True
                        dumped["section_name"] = inp.section
                        dumped["body"] = section_content
                return dumped
            finally:
                if inp.vault:
                    target_store.close()
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_get failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_add -----------------------------------------------------------

    @mcp.tool()
    def kb_add(
        type: str,  # matches MCP schema in architecture.md § 4.4
        title: str,
        body: str = "",
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str | None = None,
        id: str | None = None,
    ) -> Any:
        """Create a new document.

        BEST PRACTICE FOR AI AGENTS:
        After creating a document, if there are relevant related documents (e.g. a related project, decision, or topic), call the 'kb_link' tool to link them together to maintain graph connectivity.

        Args:
            type: Document type (e.g. "project", "decision").
            title: Document title (non-empty).
            body: Markdown body (default "").
            tags: List of tag strings (optional).
            aliases: Alternative IDs for this document (optional).
            metadata: Structured type-specific attributes dict (optional).
            source: Origin file path (optional, enables idempotent re-import).
            id: Explicit document id (e.g. "reference/foo/bar"). When omitted,
                the server auto-generates one from ``type`` and ``title``. Pass
                this when the caller has a stable hierarchical id (multi-doc
                imports, micro-app docs splits, etc.).

        Returns:
            {id: new_document_id, status: "created", hint: "Remember to link this document using kb_link."}.
        """
        try:
            inp = KbAddInput(
                type=type,
                title=title,
                body=body,
                tags=tags,
                aliases=aliases,
                metadata=metadata,
                source=source,
                id=id,
            )
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info(
            "kb_add type=%r title=%r tags=%r aliases=%r metadata=%r source=%r id=%r",
            inp.type,
            inp.title,
            inp.tags,
            inp.aliases,
            inp.metadata,
            inp.source,
            inp.id,
        )
        try:
            doc_id = inp.id or make_id(inp.type, inp.title)
            doc = Document(
                id=doc_id,
                type=inp.type,
                title=inp.title,
                body=inp.body,
                tags=inp.tags or [],
                aliases=inp.aliases or [],
                metadata=inp.metadata or {},
                source=inp.source,
            )
            stored_id = store.add(doc)
            return {
                "id": stored_id,
                "status": "created",
                "agent_instruction": f"Please immediately call 'kb_link' to associate this new document '{stored_id}' with the project it belongs to (e.g. 'proj/xxx') or other related documents.",
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_add failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_link ----------------------------------------------------------

    @mcp.tool()
    def kb_link(
        from_id: str,
        to_id: str,
        rel: str = "relates-to",
    ) -> Any:
        """Create a typed edge between two documents.

        Args:
            from_id: Source document id.
            to_id: Target document id.
            rel: Relation type (default "relates-to").

        Returns:
            {ok: True, from_id, to_id, rel}.
        """
        try:
            inp = KbLinkInput(from_id=from_id, to_id=to_id, rel=rel)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_link from=%r to=%r rel=%r", inp.from_id, inp.to_id, inp.rel)
        try:
            link = store.link(inp.from_id, inp.to_id, rel=inp.rel)
            return {
                "ok": True,
                "from_id": link.from_id,
                "to_id": link.to_id,
                "rel": link.rel,
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_link failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_list ----------------------------------------------------------

    @mcp.tool()
    def kb_list(
        type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
        vault: str | None = None,
    ) -> Any:
        """List documents, sorted by ``updated_at`` DESC.

        Args:
            type: Restrict to a document type (optional).
            tags: Restrict to documents carrying all listed tags (AND, optional).
            limit: Max results 1..1000 (default 100).
            offset: Skip this many results before returning (pagination).
            include_deleted: Include soft-deleted documents (default false).
            vault: Optional vault name to list from.

        Returns:
            List of document summaries: {id, title, type, tags, updated_at}.
        """
        try:
            inp = KbListInput(
                type=type,
                tags=tags,
                limit=limit,
                offset=offset,
                include_deleted=include_deleted,
                vault=vault,
            )
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info(
            "kb_list type=%r tags=%r limit=%d offset=%d include_deleted=%s vault=%r",
            inp.type,
            inp.tags,
            inp.limit,
            inp.offset,
            inp.include_deleted,
            inp.vault,
        )
        try:
            target_store = _create_store(inp.vault) if inp.vault else store
            try:
                docs = target_store.list(
                    type=inp.type,
                    tags=inp.tags,
                    limit=inp.limit,
                    offset=inp.offset,
                    include_deleted=inp.include_deleted,
                )
                return {
                    "documents": [
                        {
                            "id": d.id,
                            "type": d.type,
                            "title": d.title,
                            "tags": d.tags,
                            "updated_at": d.updated_at.isoformat(),
                        }
                        for d in docs
                    ],
                    "count": len(docs),
                }
            finally:
                if inp.vault:
                    target_store.close()
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_list failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_update --------------------------------------------------------

    @mcp.tool()
    def kb_update(
        id: str,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> Any:
        """Patch fields on an existing document.

        Only ``title``, ``body``, ``tags``, ``aliases``, ``metadata``, ``source`` may be
        changed. ``id``, ``type``, ``created_at`` are immutable.

        Args:
            id: Document id to update.
            title: New title (optional).
            body: New Markdown body (optional).
            tags: New tag list (optional; empty list clears tags).
            aliases: New alias list (optional; empty list clears aliases).
            metadata: Structured type-specific attributes dict (optional; replaces
                the whole metadata dict — pass {} to clear).
            source: New source path (optional).

        Returns:
            {ok: True, id, updated_at}.
        """
        try:
            inp = KbUpdateInput(
                id=id,
                title=title,
                body=body,
                tags=tags,
                aliases=aliases,
                metadata=metadata,
                source=source,
            )
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        fields: dict[str, object] = {}
        if inp.title is not None:
            fields["title"] = inp.title
        if inp.body is not None:
            fields["body"] = inp.body
        if inp.tags is not None:
            fields["tags"] = inp.tags
        if inp.aliases is not None:
            fields["aliases"] = inp.aliases
        if inp.metadata is not None:
            fields["metadata"] = inp.metadata
        if inp.source is not None:
            fields["source"] = inp.source

        if not fields:
            code, msg = _mcp_error(ValidationError("update requires at least one field"))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_update id=%r fields=%s", inp.id, sorted(fields.keys()))
        try:
            doc = store.update(inp.id, **fields)
            return {"ok": True, "id": doc.id, "updated_at": doc.updated_at.isoformat()}
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_update failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_delete --------------------------------------------------------

    @mcp.tool()
    def kb_delete(id: str) -> Any:
        """Soft-delete a document by id.

        Idempotent: deleting an already-deleted document is a no-op.
        Use ``kb doctor`` and ``kb prune`` (CLI) for hard deletion.

        Args:
            id: Document id to delete.

        Returns:
            {ok: True, id}.
        """
        try:
            inp = KbDeleteInput(id=id)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_delete id=%r", inp.id)
        try:
            store.delete(inp.id)
            return {"ok": True, "id": inp.id}
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_delete failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_unlink --------------------------------------------------------

    @mcp.tool()
    def kb_unlink(
        from_id: str,
        to_id: str,
        rel: str | None = None,
    ) -> Any:
        """Remove typed edges between two documents.

        If ``rel`` is None, all edges between ``from_id`` and ``to_id`` are
        removed. Returns the count of edges removed.

        Args:
            from_id: Source document id.
            to_id: Target document id.
            rel: Relation type (default: remove all relations).

        Returns:
            {ok: True, removed: N, from_id, to_id, rel}.
        """
        try:
            inp = KbUnlinkInput(from_id=from_id, to_id=to_id, rel=rel)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_unlink from=%r to=%r rel=%r", inp.from_id, inp.to_id, inp.rel)
        try:
            n = store.unlink(inp.from_id, inp.to_id, rel=inp.rel)
            return {
                "ok": True,
                "removed": n,
                "from_id": inp.from_id,
                "to_id": inp.to_id,
                "rel": inp.rel,
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_unlink failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_history -------------------------------------------------------

    class KbHistoryInput(BaseModel):
        id: str = Field(min_length=1)
        limit: int = Field(default=50, ge=1, le=500)

    @mcp.tool()
    def kb_history(id: str, limit: int = 50) -> Any:
        """View the version history of a document.

        Args:
            id: Document id.
            limit: Max versions to return (default 50, max 500).

        Returns:
            List of version entries: {version_id, action, created_at, ...}.
        """
        try:
            inp = KbHistoryInput(id=id, limit=limit)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_history id=%r limit=%d", inp.id, inp.limit)
        try:
            history = store.document_history(inp.id, limit=inp.limit)
            return {"history": history, "count": len(history)}
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_history failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_restore -------------------------------------------------------

    class KbRestoreInput(BaseModel):
        id: str = Field(min_length=1)
        version: int | None = None

    @mcp.tool()
    def kb_restore(id: str, version: int | None = None) -> Any:
        """Restore a document to a previous version.

        Args:
            id: Document id.
            version: Version id to restore to (default: most recent).

        Returns:
            {ok: True, id, version, restored_at}.
        """
        try:
            inp = KbRestoreInput(id=id, version=version)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_restore id=%r version=%r", inp.id, inp.version)
        try:
            doc = store.restore(inp.id, version_id=inp.version)
            return {
                "ok": True,
                "id": doc.id,
                "version": inp.version,
                "restored_at": doc.updated_at.isoformat(),
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_restore failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_diff ----------------------------------------------------------

    class KbDiffInput(BaseModel):
        id: str = Field(min_length=1)
        version_a: int
        version_b: int

    @mcp.tool()
    def kb_diff(id: str, version_a: int, version_b: int) -> Any:
        """Compare two document versions and return field-level differences.

        Args:
            id: Document id.
            version_a: First version id.
            version_b: Second version id.

        Returns:
            {added, removed, changed} describing the diff from A to B.
        """
        try:
            inp = KbDiffInput(id=id, version_a=version_a, version_b=version_b)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_diff id=%r a=%d b=%d", inp.id, inp.version_a, inp.version_b)
        try:
            result = store.diff(inp.id, inp.version_a, inp.version_b)
            return result
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_diff failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_restore_deleted -----------------------------------------------

    @mcp.tool()
    def kb_restore_deleted(id: str) -> Any:
        """Restore a soft-deleted document.

        Args:
            id: Document id.

        Returns:
            {ok: True, id, restored_at}.
        """
        try:
            inp = KbRestoreInput(id=id)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_restore_deleted id=%r", inp.id)
        try:
            doc = store.restore_deleted(inp.id)
            return {
                "ok": True,
                "id": doc.id,
                "restored_at": doc.updated_at.isoformat(),
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_restore_deleted failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_doctor --------------------------------------------------------

    @mcp.tool()
    def kb_doctor() -> Any:
        """Run knowledge base health checks.

        Returns:
            A DoctorReport dict with ``ok`` and ``checks``.
        """
        logger.info("kb_doctor")
        try:
            report = store.doctor()
            return report.model_dump(mode="json")
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_doctor failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_similar -------------------------------------------------------

    @mcp.tool()
    def kb_similar(id: str, limit: int = 10) -> Any:
        """Find documents most similar to a document by embedding distance.

        Args:
            id: Document id.
            limit: Max results 1..100 (default 10).

        Returns:
            {id, results: [{id, title, type, distance}], count}.
        """
        try:
            inp = KbSimilarInput(id=id, limit=limit)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_similar id=%r limit=%d", inp.id, inp.limit)
        try:
            similar = store.similar_docs(inp.id, limit=inp.limit)
            return {
                "id": inp.id,
                "results": [
                    {
                        "id": doc.id,
                        "title": doc.title,
                        "type": doc.type,
                        "distance": distance,
                    }
                    for doc, distance in similar
                ],
                "count": len(similar),
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_similar failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_duplicates ----------------------------------------------------

    @mcp.tool()
    def kb_duplicates(threshold: float = 0.15, limit: int = 50) -> Any:
        """Scan active documents for near-duplicate pairs.

        Args:
            threshold: Cosine-distance cutoff (default 0.15; lower is more
                similar).
            limit: Max pairs 1..500 (default 50).

        Returns:
            {pairs: [{id_a, id_b, distance}], count}.
        """
        try:
            inp = KbDuplicatesInput(threshold=threshold, limit=limit)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_duplicates threshold=%r limit=%d", inp.threshold, inp.limit)
        try:
            pairs = store.find_duplicates(threshold=inp.threshold, limit=inp.limit)
            return {
                "pairs": [
                    {"id_a": id_a, "id_b": id_b, "distance": distance}
                    for id_a, id_b, distance in pairs
                ],
                "count": len(pairs),
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_duplicates failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_embed_status / kb_embed_retry ---------------------------------

    @mcp.tool()
    def kb_embed_status() -> Any:
        """Show the embedding queue status (counts + oldest pending/failed).

        Since v0.8 embeddings are produced asynchronously by a background
        worker draining an ``embedding_queue``; this tool reports how the
        queue looks right now.

        Returns:
            {embedder_enabled, dim, indexed_documents,
             queue: {pending, in_progress, done, failed},
             total_enqueued, oldest_pending, oldest_failed}.
        """
        logger.info("kb_embed_status")
        try:
            return store.embedding_status()
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_embed_status failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    @mcp.tool()
    def kb_embed_retry(doc_id: str | None = None) -> Any:
        """Re-queue embedding jobs that are not currently pending.

        Args:
            doc_id: Re-run this single document (also works from
                ``done`` / ``in_progress``). When omitted, every
                ``failed`` job is reset to pending.

        Returns:
            {ok: True, retried: int, doc_id: str | None}.

        NOTE: this only re-queues the row back to ``pending`` — it does
        not embed anything itself. The row is drained asynchronously by
        the store's background worker (``auto_start_worker=True``, the
        default here), so ``kb_embed_status`` may briefly show it as
        ``pending`` before the worker picks it up. If no worker is ever
        running (a store opened with ``auto_start_worker=False``, e.g. in
        tests), the row stays ``pending``; drain it synchronously from
        code via ``SqliteStore.process_embedding_queue``.
        """
        logger.info("kb_embed_retry doc_id=%r", doc_id)
        try:
            moved = store.retry_embedding(doc_id=doc_id)
            return {"ok": True, "retried": moved, "doc_id": doc_id}
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_embed_retry failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_diff_check ----------------------------------------------------

    @mcp.tool()
    def kb_diff_check(cwd: str | None = None, vault: str | None = None) -> Any:
        """Analyze git repository diff and proactively recommend mandatory ADRs, lessons, and constraints.

        Args:
            cwd: Working directory of the git repo (default: current directory).
            vault: Optional vault name to query constraints from.

        Returns:
            {has_recommendations, decisions, lessons, apis, prompt_context}.
        """
        from kb_mcp_lite.context_guard import ContextGuard

        try:
            inp = KbDiffCheckInput(cwd=cwd, vault=vault)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info("kb_diff_check cwd=%r vault=%r", inp.cwd, inp.vault)
        try:
            target_store = _create_store(inp.vault) if inp.vault else store
            try:
                guard = ContextGuard(store=target_store)
                return guard.evaluate_diff(cwd=inp.cwd)
            finally:
                if inp.vault:
                    target_store.close()
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_diff_check failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_query_relations -----------------------------------------------

    @mcp.tool()
    def kb_query_relations(
        start_id: str,
        target_id: str | None = None,
        rel: str | None = None,
        direction: str = "outbound",
        doc_type: str | None = None,
        max_depth: int = 2,
        vault: str | None = None,
    ) -> Any:
        """Query multi-hop relations or find paths between documents in the knowledge graph.

        Args:
            start_id: Starting document id.
            target_id: If provided, finds shortest directed path from start_id to target_id.
            rel: Filter edges by relationship name (e.g. 'depends-on', 'governs').
            direction: Traversal direction — 'outbound' (default), 'inbound', or 'both'.
            doc_type: Restrict reached nodes to a specific document type.
            max_depth: Maximum hops to traverse (1..5, default 2).
            vault: Optional vault name.

        Returns:
            {path: [...]} when target_id is set, or {results: [{id, type, title, rel, hop, via}], count: N}.
        """
        from kb_mcp_lite.graph_query import GraphQueryEngine

        try:
            inp = KbGraphQueryInput(
                start_id=start_id,
                target_id=target_id,
                rel=rel,
                direction=direction,
                doc_type=doc_type,
                max_depth=max_depth,
                vault=vault,
            )
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")

        logger.info(
            "kb_query_relations start=%r target=%r rel=%r dir=%r type=%r depth=%d",
            inp.start_id,
            inp.target_id,
            inp.rel,
            inp.direction,
            inp.doc_type,
            inp.max_depth,
        )
        try:
            target_store = _create_store(inp.vault) if inp.vault else store
            try:
                engine = GraphQueryEngine(target_store)
                if inp.target_id:
                    path = engine.find_path(inp.start_id, inp.target_id, max_depth=inp.max_depth)
                    return {
                        "start_id": inp.start_id,
                        "target_id": inp.target_id,
                        "found": path is not None,
                        "path": path or [],
                    }

                results = engine.query_relations(
                    start_id=inp.start_id,
                    rel=inp.rel,
                    direction=inp.direction,
                    doc_type=inp.doc_type,
                    max_depth=inp.max_depth,
                )
                return {
                    "start_id": inp.start_id,
                    "results": results,
                    "count": len(results),
                }
            finally:
                if inp.vault:
                    target_store.close()
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_query_relations failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_impact (v0.8 特性 #6) ------------------------------------------

    @mcp.tool()
    def kb_impact(
        doc_id: str,
        max_depth: int = 3,
        max_results: int = 50,
    ) -> Any:
        """Impact analysis: find all documents influenced by the given document.

        Traverses influence edges (governs, depends-on, supersedes, blocks)
        outward from doc_id up to max_depth hops.

        Args:
            doc_id: Root document id to analyze impact from.
            max_depth: Maximum traversal hops (1-5, default 3).
            max_results: Maximum results to return (1-200, default 50).

        Returns:
            List of impact nodes: {id, title, type, distance, via, rel, path}.
        """
        try:
            inp = KbImpactInput(doc_id=doc_id, max_depth=max_depth, max_results=max_results)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")
        logger.info("kb_impact doc_id=%r max_depth=%d", inp.doc_id, inp.max_depth)
        try:
            from kb_mcp_lite.relations import ImpactAnalyzer

            analyzer = ImpactAnalyzer(store)
            nodes = analyzer.analyze(
                root_id=inp.doc_id,
                max_depth=inp.max_depth,
                max_results=inp.max_results,
            )
            return {
                "root_id": inp.doc_id,
                "results": [
                    {
                        "id": n.doc.id,
                        "title": n.doc.title,
                        "type": n.doc.type,
                        "distance": n.distance,
                        "via": n.via,
                        "rel": n.rel,
                        "path": n.path,
                    }
                    for n in nodes
                ],
                "count": len(nodes),
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_impact failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_decision_chain (v0.8 特性 #6) ----------------------------------

    @mcp.tool()
    def kb_decision_chain(
        decision_id: str,
    ) -> Any:
        """Trace the supersession chain for a decision document.

        Follows supersedes / superseded-by edges to build the decision
        evolution chain (e.g. v0.1-dec → v0.2-dec → v0.3-dec).

        Args:
            decision_id: The decision document id to trace from.

        Returns:
            {decision_id, chain: [doc_id, ...], count}.
        """
        try:
            inp = KbDecisionChainInput(decision_id=decision_id)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")
        logger.info("kb_decision_chain decision_id=%r", inp.decision_id)
        try:
            from kb_mcp_lite.relations import supersession_chain

            chain = supersession_chain(store, inp.decision_id)
            return {
                "decision_id": inp.decision_id,
                "chain": chain,
                "count": len(chain),
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_decision_chain failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_rel_spec (v0.8 特性 #6) ----------------------------------------

    @mcp.tool()
    def kb_rel_spec(
        rel_name: str | None = None,
    ) -> Any:
        """List standard relation types or show details for a specific relation.

        Args:
            rel_name: Optional relation name to inspect. Omit to list all.

        Returns:
            If rel_name given: the RelationSpec dict.
            If omitted: list of all standard relations.
        """
        try:
            inp = KbRelSpecInput(rel_name=rel_name)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")
        logger.info("kb_rel_spec rel_name=%r", inp.rel_name)
        try:
            from kb_mcp_lite.relations import STANDARD_RELATIONS, get_relation_spec

            if inp.rel_name:
                spec = get_relation_spec(inp.rel_name)
                if spec is None:
                    return {"error": f"unknown relation: {inp.rel_name!r}"}
                return {
                    "name": spec.name,
                    "forward_label": spec.forward_label,
                    "backward_label": spec.backward_label,
                    "default_direction": spec.default_direction,
                    "traversal_cost": spec.traversal_cost,
                    "is_influence": spec.is_influence,
                    "is_supersession": spec.is_supersession,
                    "description": spec.description,
                }
            else:
                return {
                    "relations": [
                        {
                            "name": s.name,
                            "forward_label": s.forward_label,
                            "is_influence": s.is_influence,
                            "is_supersession": s.is_supersession,
                            "description": s.description,
                        }
                        for s in STANDARD_RELATIONS.values()
                    ],
                    "count": len(STANDARD_RELATIONS),
                }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_rel_spec failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_expand (v0.8 特性 #5) ------------------------------------------

    @mcp.tool()
    def kb_expand(
        doc_id: str,
        depth: int = 1,
        max_neighbors: int = 10,
    ) -> Any:
        """Graph expansion: return 1-hop neighbors of a document.

        Args:
            doc_id: Document id to expand from.
            depth: Hop depth (v0.8 fixed to 1).
            max_neighbors: Max neighbors to return (default 10).

        Returns:
            {doc_id, neighbors: [{id, title, type, rel, direction}], count}.
        """
        try:
            inp = KbExpandInput(doc_id=doc_id, depth=depth, max_neighbors=max_neighbors)
        except PydanticValidationError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")
        logger.info("kb_expand doc_id=%r depth=%d", inp.doc_id, inp.depth)
        try:
            # Verify doc exists
            store.get(inp.doc_id)

            # Outbound
            out_links = store.outgoing_links(inp.doc_id)
            # Inbound
            in_links = store.incoming_links(inp.doc_id)

            neighbors = []
            seen = set()
            for lnk in out_links:
                if lnk.to_id not in seen:
                    seen.add(lnk.to_id)
                    try:
                        doc = store.get(lnk.to_id)
                        neighbors.append(
                            {
                                "id": doc.id,
                                "title": doc.title,
                                "type": doc.type,
                                "rel": lnk.rel,
                                "direction": "outbound",
                            }
                        )
                    except NotFoundError:
                        pass
            for lnk in in_links:
                if lnk.from_id not in seen:
                    seen.add(lnk.from_id)
                    try:
                        doc = store.get(lnk.from_id)
                        neighbors.append(
                            {
                                "id": doc.id,
                                "title": doc.title,
                                "type": doc.type,
                                "rel": lnk.rel,
                                "direction": "inbound",
                            }
                        )
                    except NotFoundError:
                        pass

            return {
                "doc_id": inp.doc_id,
                "neighbors": neighbors[: inp.max_neighbors],
                "count": min(len(neighbors), inp.max_neighbors),
            }
        except (ValidationError, NotFoundError, DuplicateError, IntegrityError) as e:
            code, msg = _mcp_error(e)
            logger.exception("kb_expand failed: %s", msg)
            raise RuntimeError(f"MCP error {code}: {msg}")

    # ---- kb_schedule_list (v0.8 特性 #7) -----------------------------------

    @mcp.tool()
    def kb_schedule_list() -> Any:
        """List all registered scheduled tasks.

        Returns:
            List of task dicts: {name, description}.
        """
        logger.info("kb_schedule_list")
        try:
            from kb_mcp_lite.scheduler import TASK_REGISTRY

            return {
                "tasks": [
                    {"name": name, "description": cls.description}
                    for name, cls in TASK_REGISTRY.items()
                ],
                "count": len(TASK_REGISTRY),
            }
        except Exception as e:
            raise RuntimeError(f"MCP error: {e}")

    # ---- kb_schedule_status (v0.8 特性 #7) ---------------------------------

    @mcp.tool()
    def kb_schedule_status() -> Any:
        """Show scheduler status and next run times.

        Returns:
            {running, jobs: [{id, next_run}], history_count}.
        """
        logger.info("kb_schedule_status")
        try:
            from kb_mcp_lite.scheduler import TaskScheduler
            from kb_mcp_lite.config import load_config

            config = load_config()
            scheduler = TaskScheduler(store, config)
            return scheduler.get_status()
        except Exception as e:
            raise RuntimeError(f"MCP error: {e}")

    # ---- kb_schedule_run (v0.8 特性 #7) ------------------------------------

    @mcp.tool()
    def kb_schedule_run(
        task_name: str,
    ) -> Any:
        """Manually trigger a scheduled task.

        Args:
            task_name: Name of the task to run (e.g. 'auto-commit').

        Returns:
            {task_name, status, duration_ms, error?}.
        """
        logger.info("kb_schedule_run task_name=%r", task_name)
        try:
            from kb_mcp_lite.scheduler import TaskScheduler
            from kb_mcp_lite.config import load_config

            config = load_config()
            scheduler = TaskScheduler(store, config)
            run = scheduler.run_task_now(task_name)
            return {
                "task_name": run.task_name,
                "status": run.status,
                "duration_ms": run.duration_ms,
                "error": run.error,
            }
        except ValueError as e:
            code, msg = _mcp_error(ValidationError(str(e)))
            raise RuntimeError(f"MCP error {code}: {msg}")
        except Exception as e:
            raise RuntimeError(f"MCP error: {e}")

    # ---- kb_schedule_history (v0.8 特性 #7) ---------------------------------

    @mcp.tool()
    def kb_schedule_history(
        limit: int = 20,
    ) -> Any:
        """Show task execution history.

        Args:
            limit: Max records to return (default 20).

        Returns:
            List of history records: {task_name, started_at, finished_at, status, duration_ms, error, triggered_by}.
        """
        logger.info("kb_schedule_history limit=%d", limit)
        try:
            rows = store._conn.execute(
                """
                SELECT task_name, started_at, finished_at, status, duration_ms, error, triggered_by
                FROM schedule_history
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "history": [dict(r) for r in rows],
                "count": len(rows),
            }
        except Exception as e:
            raise RuntimeError(f"MCP error: {e}")

    # ---- Resources -------------------------------------------------------

    @mcp.resource(
        "kb://doc/{id}",
        name="doc",
        description="Full document by id (JSON)",
        mime_type="application/json",
    )
    def kb_resource_doc(id: str) -> str:
        """Return the full document as JSON.

        Args:
            id: Document ID.

        Returns:
            JSON string of the full document.
        """
        id = unquote(id)
        logger.info("resource kb://doc/%s", id)
        try:
            doc = store.get(id)
            return json.dumps(doc.model_dump(mode="json"), ensure_ascii=False)
        except NotFoundError:
            return json.dumps({"error": "not_found", "id": id})
        except (ValidationError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    @mcp.resource(
        "kb://links/{id}",
        name="links",
        description="Backlinks and outlinks for a document (JSON)",
        mime_type="application/json",
    )
    def kb_resource_links(id: str) -> str:
        """Return the links (inbound + outbound) for a document.

        Args:
            id: Document ID.

        Returns:
            JSON object with backlinks and outlinks arrays.
        """
        id = unquote(id)
        logger.info("resource kb://links/%s", id)
        try:
            backlinks = store.backlinks(id)
            outlinks = store.outlinks(id)
            return json.dumps(
                {
                    "doc_id": id,
                    "backlinks": [{"from_id": lnk.from_id, "rel": lnk.rel} for lnk in backlinks],
                    "outlinks": [{"to_id": lnk.to_id, "rel": lnk.rel} for lnk in outlinks],
                },
                ensure_ascii=False,
            )
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    @mcp.resource(
        "kb://types",
        name="types",
        description="Registered document types with their JSON field schemas",
        mime_type="application/json",
    )
    def kb_resource_types() -> str:
        """Return the list of registered document types and their pydantic field
        schemas.

        Returns:
            JSON object: ``{types: [{name, description, fields: [...]}], count: N}``.
        """
        logger.info("resource kb://types")
        try:
            from kb_mcp_lite.schema import default_registry

            types_info = []
            for name in default_registry.known_types():
                model = default_registry.model_for(name)
                fields = []
                for fname, finfo in model.model_fields.items():
                    fields.append(
                        {
                            "name": fname,
                            "type": str(finfo.annotation),
                            "required": finfo.is_required(),
                            "default": repr(finfo.default) if finfo.default is not None else None,
                        }
                    )
                types_info.append(
                    {
                        "name": name,
                        "description": (model.__doc__ or "").strip(),
                        "fields": fields,
                    }
                )
            return json.dumps(
                {"types": types_info, "count": len(types_info)},
                ensure_ascii=False,
            )
        except (ValidationError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    @mcp.resource(
        "kb://stats",
        name="stats",
        description="Knowledge base statistics (document counts, links, recent changes)",
        mime_type="application/json",
    )
    def kb_resource_stats() -> str:
        """Return knowledge base statistics.

        Returns:
            JSON string of stats dict.
        """
        logger.info("resource kb://stats")
        try:
            stats = store.stats()
            return json.dumps(stats, ensure_ascii=False)
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    @mcp.resource(
        "kb://graph/{id}",
        name="graph",
        description="Subgraph centred on a document (depth 2); JSON with nodes and edges",
        mime_type="application/json",
    )
    def kb_resource_graph(id: str) -> str:
        """Return the subgraph (depth 2) centred on a document.

        Args:
            id: Document ID.

        Returns:
            JSON string with node ids and edges.
        """
        id = unquote(id)
        logger.info("resource kb://graph/%s", id)
        try:
            sub = store.subgraph(id, depth=2)
            return json.dumps(sub, ensure_ascii=False)
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    @mcp.resource(
        "kb://graph/{id}/{depth}",
        name="graph-depth",
        description="Subgraph centred on a document at a given depth; JSON with nodes and edges",
        mime_type="application/json",
    )
    def kb_resource_graph_depth(id: str, depth: str) -> str:
        """Return the subgraph at a custom depth centred on a document.

        Args:
            id: Document ID.
            depth: Traversal depth (1, 2, 3, …).

        Returns:
            JSON string with node ids and edges.
        """
        id = unquote(id)
        # _is_graph_depth() unquotes before admitting a tail as a depth, so a
        # percent-encoded digit (e.g. %33) arrives here and must be decoded too.
        depth = unquote(depth)
        logger.info("resource kb://graph/%s depth=%s", id, depth)
        try:
            n = int(depth)
            if n < 1 or n > 8:
                return json.dumps({"error": f"depth must be 1..8 (got {depth})"})
            sub = store.subgraph(id, depth=n)
            return json.dumps(sub, ensure_ascii=False)
        except ValueError:
            return json.dumps({"error": f"invalid depth {depth!r}"})
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    # ---- Resource: kb://list ------------------------------------------------

    @mcp.resource(
        "kb://list",
        name="list",
        description="List all documents, sorted by updated_at DESC (JSON)",
        mime_type="application/json",
    )
    def kb_resource_list() -> str:
        """Return a summary list of all active documents."""
        logger.info("resource kb://list")
        try:
            docs = store.list(limit=1000)
            return json.dumps(
                {
                    "documents": [
                        {
                            "id": d.id,
                            "type": d.type,
                            "title": d.title,
                            "tags": d.tags,
                            "updated_at": d.updated_at.isoformat(),
                        }
                        for d in docs
                    ],
                    "count": len(docs),
                },
                ensure_ascii=False,
            )
        except (ValidationError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    @mcp.resource(
        "kb://list/{type}",
        name="list-type",
        description="List documents of a specific type (JSON)",
        mime_type="application/json",
    )
    def kb_resource_list_type(type: str) -> str:
        """Return a summary list of documents filtered by type."""
        type = unquote(type)
        logger.info("resource kb://list type=%r", type)
        try:
            docs = store.list(type=type, limit=1000)
            return json.dumps(
                {
                    "type": type,
                    "documents": [
                        {
                            "id": d.id,
                            "type": d.type,
                            "title": d.title,
                            "tags": d.tags,
                            "updated_at": d.updated_at.isoformat(),
                        }
                        for d in docs
                    ],
                    "count": len(docs),
                },
                ensure_ascii=False,
            )
        except (ValidationError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    # ---- Resource: kb://changes --------------------------------------------

    @mcp.resource(
        "kb://changes",
        name="changes",
        description="Recent changes to the knowledge base (audit log, JSON)",
        mime_type="application/json",
    )
    def kb_resource_changes() -> str:
        """Return the most recent audit log entries."""
        logger.info("resource kb://changes")
        try:
            log = store.audit_log(limit=50)
            return json.dumps(
                {"changes": log, "count": len(log)},
                ensure_ascii=False,
            )
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    # ---- Resource: kb://history --------------------------------------------

    @mcp.resource(
        "kb://history/{id}",
        name="history",
        description="Version history for a document (JSON)",
        mime_type="application/json",
    )
    def kb_resource_history(id: str) -> str:
        """Return the version history for a document."""
        id = unquote(id)
        logger.info("resource kb://history id=%r", id)
        try:
            history = store.document_history(id)
            return json.dumps(
                {"id": id, "history": history, "count": len(history)},
                ensure_ascii=False,
            )
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    # ---- Resource: kb://search ---------------------------------------------

    @mcp.resource(
        "kb://search/{query}",
        name="search",
        description="Search results for a query (JSON, hybrid mode)",
        mime_type="application/json",
    )
    def kb_resource_search(query: str) -> str:
        """Return search results for a query using hybrid mode."""
        query = unquote(query)
        logger.info("resource kb://search query=%r", query)
        try:
            hits = store.search(query, limit=20, mode="hybrid")
            return json.dumps(
                {
                    "query": query,
                    "hits": [
                        {
                            "id": h.doc.id,
                            "title": h.doc.title,
                            "type": h.doc.type,
                            "snippet": h.snippet,
                            "score": h.score,
                        }
                        for h in hits
                    ],
                    "count": len(hits),
                },
                ensure_ascii=False,
            )
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return json.dumps({"error": str(e)})

    # ---- Resource: kb://export ---------------------------------------------

    @mcp.resource(
        "kb://export/{id}",
        name="export",
        description="Full document body as Markdown",
        mime_type="text/markdown",
    )
    def kb_resource_export(id: str) -> str:
        """Return the document rendered as round-trippable Markdown."""
        id = unquote(id)
        logger.info("resource kb://export id=%r", id)
        try:
            doc = store.get(id)
            return render_document(doc, outlinks=store.outlinks(id))
        except (ValidationError, NotFoundError, IntegrityError) as e:
            return f"# Error\n\nCould not export document {id!r}: {e}"

    # ---- Resource: kb://help -----------------------------------------------

    @mcp.resource(
        "kb://help/{doc}",
        name="help",
        description="Built-in documentation (e.g. quickstart, architecture) — Markdown",
        mime_type="text/markdown",
    )
    def kb_resource_help(doc: str) -> str:
        """Return a built-in help document packaged with the server."""
        doc = unquote(doc)
        logger.info("resource kb://help doc=%r", doc)
        try:
            from importlib import resources

            if doc not in _HELP_DOCS:
                available = ", ".join(_HELP_DOCS)
                return (
                    f"# Not Found\n\nHelp document `{doc}` not found."
                    f"\n\nAvailable documents: {available}"
                )
            help_dir = resources.files("kb_mcp_lite").joinpath("help")
            help_file = help_dir.joinpath(f"{doc}.md")
            return help_file.read_text(encoding="utf-8")
        except OSError as e:
            return f"# Error\n\nCould not read help document: {e}"

    # ---- Prompts ---------------------------------------------------------

    @mcp.prompt(
        name="new-doc",
        title="New Document",
        description="Create a new document with a type-specific Markdown template.",
    )
    def kb_prompt_new_doc(type: str = "decision") -> str:
        """Return a Markdown skeleton for a given document type.

        Args:
            type: Built-in type — project, decision, lesson, glossary, person,
                faq, api, runbook, release.

        Returns:
            A fill-in-the-blank Markdown template.
        """
        skeleton = _DOC_TEMPLATES.get(type, _DEFAULT_DOC_TEMPLATE)
        return (
            f"You are adding a **{type}** document to the knowledge base.\n\n"
            "Fill in this template:\n\n"
            f"{skeleton}\n\n"
            "After filling, call kb_add with type, title, body, and optional tags."
        )

    @mcp.prompt(
        name="link-analysis",
        title="Link Analysis",
        description="Analyse a document's link graph and suggest missing connections.",
    )
    def kb_prompt_link_analysis(id: str) -> str:
        """Analyse the link graph around a document and suggest missing links.

        Args:
            id: Document id to analyse (e.g. "proj/kb-mcp").

        Returns:
            A multi-step analysis workflow the agent can execute.
        """
        return (
            f"Analyse the link graph for document **{id}**.\n\n"
            "1. **Read the document** — call kb_get to fetch the full body.\n"
            "2. **Check backlinks** — use the kb://links/ resource to see "
            "which documents link to this one.\n"
            "3. **Check outlinks** — same resource; does this document "
            "reference other documents by id?\n"
            "4. **Search for related docs** — use kb_search with key terms "
            "from the body to find documents that should be linked but aren't.\n"
            "5. **Suggest new links** — call kb_link(from_id, to_id, "
            "rel='relates-to') for each missing connection.\n\n"
            "Consider especially:\n"
            "- Decisions that mention this context but aren't linked to it\n"
            "- Lessons learned that reference the same component\n"
            "- FAQ entries whose answer involves this document\n"
            "- Person documents listing this under their projects\n\n"
            "When done, summarise how many links were added."
        )

    # ---- Prompt: search-guide ----------------------------------------------

    @mcp.prompt(
        name="search-guide",
        title="Search Guide",
        description="Guide on how to search the knowledge base effectively.",
    )
    def kb_prompt_search_guide() -> str:
        """Return a guide on effective search strategies."""
        return (
            "## Knowledge Base Search Guide\n\n"
            "The knowledge base supports multiple search modes:\n\n"
            "### Search Modes\n"
            "- **`lexical`**: Exact token BM25 (AND-of-tokens). Best for precise queries.\n"
            "- **`fuzzy`**: Trigram BM25 — tolerates typos and partial words.\n"
            "- **`hybrid`** (default): Reciprocal-rank fusion of lexical + fuzzy + semantic.\n"
            "- **`rrf`**: Same as hybrid, with configurable RRF constant.\n"
            "- **`semantic`**: Vector similarity (requires an embedder).\n\n"
            "### Tips\n"
            '- Use **`kb_search`** tool with `mode="hybrid"` for best results.\n'
            '- Filter by `type` (e.g. `"decision"`, `"project"`) to narrow down.\n'
            "- Filter by `tags` to focus on a specific domain.\n"
            "- For browsing, use **`kb_list`** tool or the `kb://list/` resource.\n"
            "- For reading a single document, use the `kb://doc/{id}` resource.\n"
            "- To walk the link graph, use `kb://graph/{id}/{depth}`.\n"
            "- To understand available document types, read `kb://types`.\n\n"
            "### When to use what\n"
            "- **I know the exact id** → `kb://doc/...` resource or `kb_get` tool\n"
            '- **I know keywords** → `kb_search` with `mode="hybrid"`\n'
            "- **I want to explore** → `kb_list` or `kb://list/` resource\n"
            "- **I want related docs** → `kb://graph/...` resource\n"
        )

    # ---- Prompt: import-docs -----------------------------------------------

    @mcp.prompt(
        name="import-docs",
        title="Import Documents",
        description="Import documents from Markdown files into the knowledge base.",
    )
    def kb_prompt_import_docs() -> str:
        """Return a guide for importing documents."""
        return (
            "## Importing Documents\n\n"
            "You can import Markdown documents with YAML frontmatter into the knowledge base.\n\n"
            "### File Format\n"
            "Each `.md` file should have YAML frontmatter:\n\n"
            "```markdown\n"
            "---\n"
            "type: decision\n"
            "title: Use SQLite for storage\n"
            "tags: [database, sqlite]\n"
            "---\n"
            "Body content here...\n"
            "```\n\n"
            "### Using the CLI\n"
            "Batch import is a CLI-only lifecycle operation and is NOT available through "
            "this MCP server. Run `kb import <directory>` to batch-import all `.md` files, "
            "or `kb import <directory> --dry-run` to preview without writing.\n\n"
            "### Using kb_add\n"
            "For a single document, use the `kb_add` tool:\n"
            "```\n"
            'kb_add(type="decision", title="...", body="...", tags=["..."])\n'
            "```\n\n"
            "### Idempotent Re-import\n"
            "If a file has a `source` field matching an existing document's source, "
            "the import updates rather than duplicates.\n\n"
            "### What to import\n"
            "- Architecture Decision Records (ADRs)\n"
            "- Post-mortems and lessons learned\n"
            "- Project READMEs and onboarding docs\n"
            "- Glossary terms and definitions\n"
            "- FAQ entries\n"
        )

    # ---- Prompt: doctor ----------------------------------------------------

    @mcp.prompt(
        name="doctor",
        title="Health Check",
        description="Run a health check on the knowledge base to detect issues.",
    )
    def kb_prompt_doctor() -> str:
        """Return a guide for running knowledge base health checks."""
        return (
            "## Knowledge Base Health Check\n\n"
            "Run a health check to detect issues like:\n\n"
            "### Checks performed\n"
            "1. **Integrity check** — SQLite PRAGMA integrity_check\n"
            "2. **FTS sync** — FTS5 row count matches active documents\n"
            "3. **Orphan links** — No links pointing to non-existent documents\n"
            "4. **Valid type/title** — All documents have non-empty type and title\n\n"
            "### How to run\n"
            "1. Call the `kb_doctor` tool to run the health checks.\n"
            "2. Read `kb://types` to verify document types are registered.\n"
            "3. Read `kb://stats` to see document counts and trends.\n"
            "4. Read `kb://changes` to review recent modifications.\n\n"
            "### Common issues and fixes\n"
            "- **Orphan links**: A document was deleted but links pointing to it remain.\n"
            "  Use `kb_unlink` to clean them up.\n"
            "- **FTS mismatch**: Rebuild via the CLI (`kb reindex`); this operation is "
            "not available through MCP.\n"
            "- **Soft-deleted clutter**: Prune via the CLI (`kb prune`); this operation "
            "is not available through MCP.\n"
        )

    # ---- Prompt: maintenance -----------------------------------------------

    @mcp.prompt(
        name="maintenance",
        title="Maintenance",
        description="Guide for maintaining the knowledge base: prune, reindex, duplicate detection.",
    )
    def kb_prompt_maintenance() -> str:
        """Return a guide for knowledge base maintenance operations."""
        return (
            "## Knowledge Base Maintenance\n\n"
            "### Start with health\n"
            "Call `kb_doctor` first. If it reports orphan links or FTS drift, fix the "
            "MCP-reachable issues here and use the CLI for lifecycle operations.\n\n"
            "### Find duplicates\n"
            "Call `kb_duplicates` to scan for near-duplicate pairs. For a specific "
            "document, call `kb_similar` to see which documents are most related.\n\n"
            "### Merge duplicate documents\n"
            "1. Call `kb_get` on both documents to compare content.\n"
            "2. Merge content into the canonical document via `kb_update`.\n"
            "3. Re-link edges with `kb_link` to point references at the canonical id.\n"
            "4. Soft-delete the duplicate with `kb_delete`.\n\n"
            "### CLI-only lifecycle operations\n"
            "- `kb prune` hard-deletes old soft-deleted documents.\n"
            "- `kb reindex` rebuilds the FTS index.\n"
            "- `kb import` / `kb export` move Markdown files to and from the database.\n"
            "- `kb vault` manages vaults and Git sync.\n"
            "These operations are not exposed through this MCP server; do not claim to "
            "run them here.\n\n"
            "### Backup\n"
            "Use `kb export <directory>` from the CLI to create a Markdown backup "
            "suitable for version control integration.\n"
        )

    # ---- Prompt: onboarding ------------------------------------------------

    @mcp.prompt(
        name="onboarding",
        title="Knowledge Base Onboarding",
        description="Get an overview of what this knowledge base contains and how to use it.",
    )
    def kb_prompt_onboarding() -> str:
        """Return an onboarding overview for a new AI session."""
        return (
            "## Knowledge Base Onboarding\n\n"
            "This MCP server manages a **structured knowledge base** — "
            "a collection of Markdown documents with typed relationships.\n\n"
            "### Document Types\n"
            "- **project** — A project, repo, or initiative\n"
            "- **decision** — Architecture Decision Record (ADR)\n"
            "- **lesson** — Post-mortem / lessons learned\n"
            "- **glossary** — Term definition\n"
            "- **person** — A person the agent should recognise\n"
            "- **faq** — Frequently asked question\n"
            "- **api** — API endpoint documentation\n"
            "- **runbook** — Operational procedure or SOP\n"
            "- **release** — Release log and rollback notes\n\n"
            "### Quick Start\n"
            "1. Read `kb://stats` for an overview of what's stored.\n"
            "2. Read `kb://types` to see the full field schemas.\n"
            "3. List documents: `kb://list/` or `kb://list/{type}`.\n"
            "4. Search: `kb://search/{query}` resource or `kb_search` tool.\n"
            "5. Read a document: `kb://doc/{id}` resource or `kb_get` tool.\n"
            "6. Explore relationships: `kb://graph/{id}/{depth}`.\n"
            "7. Check recent changes: `kb://changes` resource.\n\n"
            "### Key Capabilities\n"
            "- **Full-text search** with lexical, fuzzy, semantic, and hybrid modes\n"
            "- **Linked graph** of typed relationships between documents\n"
            "- **Version history** — every change is tracked and revertible\n"
            "- **Similarity** — find related documents by semantic similarity\n"
            "- **Multi-vault** — isolate knowledge bases for different contexts\n"
            "- **Health & duplicates** — `kb_doctor` and `kb_duplicates` help audit "
            "the knowledge base\n\n"
            "### Need help?\n"
            "- Run the `search-guide` prompt for search tips.\n"
            "- Run the `doctor` prompt to check knowledge base health.\n"
            "- Run `kb://help/quickstart` for the quickstart guide.\n"
            "- Run `kb://help/architecture` for the architecture docs.\n"
        )

    return mcp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _require_http_token() -> str:
    """Return the bearer token for HTTP transport, refusing to start without one.

    The token comes from ``KB_MCP_HTTP_TOKEN`` only — never from an argument, so
    it stays out of shell history and process listings. The error text names the
    variable but never the value.
    """
    token = os.environ.get("KB_MCP_HTTP_TOKEN", "")
    if not token.strip():
        raise KbMcpError(
            "KB_MCP_HTTP_TOKEN is not set (or empty): 'kb serve --transport http' requires a "
            "non-empty bearer token. Set the environment variable to a secret value, e.g. "
            "PowerShell: $env:KB_MCP_HTTP_TOKEN = '<secret>'. Do not pass it as a command-line "
            "argument."
        )
    return token


def _make_bearer_guard(app: Any, token: str) -> Any:
    """Wrap an ASGI app so every HTTP method requires ``Authorization: Bearer <token>``.

    FastMCP's own ``token_verifier`` needs OAuth ``AuthSettings`` (with a mandatory
    ``issuer_url``), which does not describe a pre-shared static token. This guard sits
    outside the transport instead, so POST, GET (the SSE stream) and DELETE are covered
    identically — "auth on POST only" is structurally impossible here.

    Comparison hashes both sides first: fixed length, so ``hmac.compare_digest`` is a
    true constant-time comparison, and an empty presented value can never match an
    empty configured one (which would compare equal as plain strings). Neither the
    token nor the presented header is logged or echoed back.
    """
    expected = hashlib.sha256(token.encode("utf-8")).digest()

    async def guard(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        presented = b""
        for key, value in scope.get("headers") or ():
            if key == b"authorization" and value[:7].lower() == b"bearer ":
                presented = value[7:]
                break
        if not hmac.compare_digest(hashlib.sha256(presented).digest(), expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await app(scope, receive, send)

    return guard


def _allow_dns_rebinding_host(settings: Any, host: str) -> None:
    """Add ``host`` to the DNS-rebinding allowlist FastMCP built for localhost.

    FastMCP derives that allowlist from the host passed to its constructor, which is
    always the localhost default here. Without this, an explicit ``--host`` would bind
    the socket but reject every request whose Host header is not a localhost name —
    protection stays on, it just learns the new host.
    """
    security = getattr(settings, "transport_security", None)
    if security is None or not security.enable_dns_rebinding_protection:
        return
    # IPv6 字面量在 Host/Origin 里带方括号（RFC 3986），而 SDK 的匹配是
    # ``host.startswith(base_host + ":")``——不加方括号的条目永远匹配不上。
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    for attr, entry in (("allowed_hosts", f"{host}:*"), ("allowed_origins", f"http://{host}:*")):
        values = list(getattr(security, attr) or [])
        if entry not in values:
            values.append(entry)
            setattr(security, attr, values)


def run(
    vault: str | None = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the MCP server.

    Args:
        vault: Optional vault name. Defaults to the current active vault.
        transport: ``"stdio"`` (default) or ``"http"`` (streamable HTTP on ``/mcp``).
        host: Interface to bind for HTTP transport. Defaults to loopback only.
        port: Port to bind for HTTP transport.

    Called by ``kb serve`` CLI command.

    stdio uses :meth:`FastMCP.run` (sync, anyio-backed) rather than the async
    ``run_stdio_async`` + ``asyncio.run`` pair — the latter raises
    ``ValueError: I/O operation on closed file`` when stdin sees EOF
    before the asyncio loop is fully scheduled. ``anyio.run`` handles
    stdin/stdout lifecycle more gracefully under subprocess stdio.

    HTTP requires ``KB_MCP_HTTP_TOKEN`` and refuses to start without it; the token
    check runs before the store is opened or the port is bound.
    """
    if transport not in ("stdio", "http"):
        raise KbMcpError(f"unknown transport {transport!r}: expected 'stdio' or 'http'")
    if transport == "stdio":
        _make_server(vault).run(transport="stdio")
        return

    token = _require_http_token()
    mcp = _make_server(vault)
    mcp.settings.host = host
    mcp.settings.port = port
    _allow_dns_rebinding_host(mcp.settings, host)

    app = _make_bearer_guard(mcp.streamable_http_app(), token)
    logging.getLogger("kb_mcp_lite").info(
        "serving MCP over streamable HTTP on http://%s:%d%s (bearer token required)",
        host,
        port,
        mcp.settings.streamable_http_path,
    )

    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("KB_MCP_LOG_LEVEL", "info").lower(),
    )


__all__ = [
    "run",
    "KbSearchInput",
    "KbGetInput",
    "KbAddInput",
    "KbLinkInput",
    "KbListInput",
    "KbUpdateInput",
    "KbDeleteInput",
    "KbUnlinkInput",
    "KbSimilarInput",
    "KbDuplicatesInput",
]


# Allow ``python -m kb_mcp_lite.mcp_server`` to start the server directly
# (avoids Click's stdin/stdout interaction).
if __name__ == "__main__":
    run()
