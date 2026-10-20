"""Document models and type registry.

This module is the canonical source for the document schema. Every other
module (``store``, ``md_io``, ``mcp_server``, ``cli``) imports from here.
If you change a field name or a validation rule, you change it here and
propagate — never duplicate.

Built-in types: ``project``, ``decision``, ``lesson``, ``glossary``,
``person``, ``faq``, ``api``, ``runbook``, ``release``. Users can add custom types via :class:`TypeRegistry`.

Conventions
-----------

- All IDs are **slugs** (``a-z0-9-`` plus ``/`` for grouping). Examples:
  ``proj/kb-mcp``, ``dec/use-sqlite-fts5``, ``lesson/dont-reuse-lastrowid``.
- All timestamps are **ISO-8601 UTC strings** in the SQLite layer
  (``datetime.utcnow().isoformat()``); pydantic models accept
  ``datetime`` and serialise to ISO.
- ``body`` is **Markdown**; the store does not parse it.
- ``tags`` are stored as a JSON array string in SQLite for portability;
  pydantic models expose ``list[str]``.
"""

from __future__ import annotations

import os

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Built-in document types
# ---------------------------------------------------------------------------


class DocumentType(str, Enum):
    """Built-in document types shipped with kb-mcp v0.1.0.

    Custom types are allowed (any non-empty string); this enum is the
    *known-good* set with sensible defaults.
    """

    PROJECT = "project"
    DECISION = "decision"
    LESSON = "lesson"
    GLOSSARY = "glossary"
    PERSON = "person"
    FAQ = "faq"
    API = "api"
    RUNBOOK = "runbook"
    RELEASE = "release"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KbMcpError(Exception):
    """Base for all kb-mcp exceptions."""


class NotFoundError(KbMcpError):
    """Raised when a document id does not exist."""

    def __init__(self, doc_id: str) -> None:
        super().__init__(f"document not found: {doc_id!r}")
        self.doc_id = doc_id


class DuplicateError(KbMcpError):
    """Raised when adding a document whose (type, title) already exists."""

    def __init__(self, doc_id: str, existing_id: str | None = None) -> None:
        msg = f"document already exists: {doc_id!r}"
        if existing_id and existing_id != doc_id:
            msg += f" (existing id: {existing_id!r})"
        super().__init__(msg)
        self.doc_id = doc_id
        self.existing_id = existing_id


class ValidationError(KbMcpError):
    """Raised when input fails pydantic validation or store invariants."""


class IntegrityError(KbMcpError):
    """Raised when DB integrity check fails (rare; usually bug)."""


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


_TYPE_PREFIX = {
    DocumentType.PROJECT: "proj",
    DocumentType.DECISION: "dec",
    DocumentType.LESSON: "lesson",
    DocumentType.GLOSSARY: "glossary",
    DocumentType.PERSON: "person",
    DocumentType.FAQ: "faq",
    DocumentType.API: "api",
    DocumentType.RUNBOOK: "runbook",
    DocumentType.RELEASE: "release",
}


def slugify(title: str, max_length: int = 32) -> str:
    """Convert ``title`` to a URL-safe ASCII-first slug.

    Lowercase, replace runs of non-alphanumeric with ``-``, strip
    non-ASCII characters, and trim leading/trailing dashes. Non-ASCII
    characters that cannot be represented in ASCII (e.g. CJK characters)
    are dropped, which keeps IDs compatible with the
    ``^[a-z0-9][a-z0-9/_-]*$`` validation regex.
    Overly long ASCII prefixes are truncated to ``max_length`` characters
    at word boundaries.

    >>> slugify("Use SQLite FTS5!")
    'use-sqlite-fts5'
    >>> slugify("kb-mcp 项目")
    'kb-mcp-cb580c2c'

    **CJK / non-ASCII titles** (where the ASCII-only result is empty or
    degenerates to a number, e.g. "1. 概述" or "项目概述") previously
    produced a blank slug and collided on every CJK document of the same
    type. As of v0.2.2, when the ASCII-cleaned result is empty, the
    function falls back to ``cjk-<sha1[:8]>`` derived from the original
    title. This is **idempotent** (same title → same fallback slug) and
    **bounded** (always 12 chars + ``cjk-`` prefix).

    **Mixed CJK/non-ASCII + ASCII titles** (e.g. "kb-mcp 项目" vs "kb-mcp 部署")
    historically collapsed to the same ASCII part ("kb-mcp"), causing collisions.
    We detect if any CJK/non-ASCII alphanumeric character was dropped, and if so,
    append the SHA1 hash to ensure uniqueness while preserving the ASCII prefix.
    """
    import hashlib
    import re
    import unicodedata

    s = title.strip().lower()
    # Normalize (decomposes accented chars), then drop non-ASCII.
    s_ascii = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s_ascii = re.sub(r"[^a-z0-9]+", "-", s_ascii).strip("-")

    # Check if there are any CJK/non-ASCII alphanumeric characters that were dropped
    has_dropped_non_ascii = False
    for char in s:
        char_norm = unicodedata.normalize("NFKD", char)
        char_ascii = char_norm.encode("ascii", "ignore").decode("ascii")
        if char.isalnum() and not char_ascii:
            has_dropped_non_ascii = True
            break

    h = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]

    # Truncate overly long ASCII prefixes at word boundaries
    if len(s_ascii) > max_length:
        truncated = s_ascii[:max_length].rstrip("-")
        if "-" in truncated:
            last_dash = truncated.rfind("-")
            if last_dash > 0:
                truncated = truncated[:last_dash]
        s_ascii = truncated

    if s_ascii and not s_ascii.replace("-", "").isdigit():
        if has_dropped_non_ascii:
            return f"{s_ascii}-{h}"
        return s_ascii
    # Fallback: empty / pure-digit (likely truncated CJK) → stable hash.
    return f"cjk-{h}"


def make_id(doc_type: str, title: str) -> str:
    """Return a flat, slugified document ID without directory prefixes.

    Ensures 1:1 isomorphism with Git filenames ({id}.md) and avoids
    path traversal/directory mismatch issues during import/export.

    >>> make_id("project", "kb-mcp")
    'kb-mcp'
    >>> make_id("custom-type", "Hello World")
    'hello-world'
    """
    return slugify(title)


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


class Document(BaseModel):
    """A typed Markdown document in the knowledge base.

    Subclassing is supported for type-specific fields (see ``Project``,
    ``Decision``, etc. below). The base model is sufficient for every
    built-in type at v0.1; subclasses exist to document type-specific
    conventions and to give static type-checkers hints.

    Validation rules:

    - ``id`` matches ``^[a-z0-9][a-z0-9/_-]*$``
    - ``title`` is non-empty after stripping
    - ``type`` is non-empty (any string for custom types)
    - ``tags`` items match ``^[a-z0-9][a-z0-9_-]*$`` (lowercase, no spaces)
    """

    model_config = ConfigDict(frozen=False, str_strip_whitespace=True)

    id: str = Field(
        ..., description="Slug-style id; auto-generated from type+title if omitted on add"
    )
    type: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=512)
    body: str = Field(default="", max_length=1_000_000)  # 1 MB cap
    tags: list[str] = Field(default_factory=list, max_length=64)
    aliases: list[str] = Field(
        default_factory=list, description="Alternative IDs for this document"
    )
    source: str | None = Field(default=None, description="Origin file path if imported")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Structured type-specific attributes"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: datetime | None = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str | None) -> str | None:
        if v and os.path.isabs(v):
            raise ValueError(f"source must be a relative path, got absolute path {v!r}")
        return v

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        # Empty id is allowed at the model layer; the store layer is
        # responsible for filling it in via ``make_id(type, title)``.
        # This split lets callers build a Document with just type+title
        # and let the store assign the slug.
        if v == "":
            return v
        import re as _re

        if not _re.match(r"^[a-z0-9][a-z0-9/_-]*$", v):
            raise ValueError(f"id must match ^[a-z0-9][a-z0-9/_-]*$ (got {v!r})")
        return v

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str]) -> list[str]:
        import re

        for t in v:
            t_str = str(t).strip()
            if not t_str:
                raise ValueError("tag cannot be empty")
            # Allow ASCII lowercase alphanumeric, dash, underscore, and Unicode / CJK word characters
            if not re.match(r"^[\w\-]+$", t_str, re.UNICODE):
                raise ValueError(f"tag {t!r} must contain valid alphanumeric or unicode characters")
        return v

    @field_validator("created_at", "updated_at", "deleted_at", mode="before")
    @classmethod
    def _parse_dt(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            # Accept "...Z" suffix as UTC.
            s = v.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s)
            except ValueError as e:
                raise ValueError(f"invalid ISO-8601 datetime {v!r}: {e}") from e
        raise ValueError(f"unsupported datetime value: {v!r}")

    # ---- helpers --------------------------------------------------------

    def extract_sections(self) -> dict[str, str]:
        """Parse markdown body into a dictionary of {section_heading: section_content}.

        Headings (e.g., '# Heading', '## Subheading') are normalized by stripping '#'.
        The root content before the first heading is keyed as '' (empty string).
        """
        import re

        sections: dict[str, str] = {}
        current_heading = ""
        current_lines: list[str] = []

        for line in (self.body or "").splitlines():
            m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
            if m:
                if current_heading or current_lines:
                    sections[current_heading] = "\n".join(current_lines).strip()
                current_heading = m.group(2).strip()
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_heading or current_lines:
            sections[current_heading] = "\n".join(current_lines).strip()

        return sections

    def get_section(self, heading: str) -> str | None:
        """Get content of a specific heading (case-insensitive fuzzy match)."""
        sections = self.extract_sections()
        if not heading:
            return sections.get("", None)
        target = heading.strip().lower()
        for k, v in sections.items():
            if k.lower() == target or target in k.lower():
                return v
        return None

    def to_row(self) -> dict[str, Any]:
        """Serialise to a flat dict suitable for SQLite INSERT.

        ``tags`` and ``metadata`` become JSON strings. ``created_at`` / ``updated_at`` /
        ``deleted_at`` become ISO-8601 strings.
        """
        import json

        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "metadata": json.dumps(self.metadata, ensure_ascii=False) if self.metadata else "{}",
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Document:
        """Inverse of :meth:`to_row`."""
        import json
        from datetime import datetime

        data = dict(row)
        tags_raw = data.get("tags") or "[]"
        if isinstance(tags_raw, str):
            try:
                data["tags"] = json.loads(tags_raw)
            except Exception:
                data["tags"] = []
        meta_raw = data.get("metadata") or "{}"
        if isinstance(meta_raw, str):
            try:
                data["metadata"] = json.loads(meta_raw)
            except Exception:
                data["metadata"] = {}
        # Parse datetime fields from ISO strings
        for k in ("created_at", "updated_at", "deleted_at"):
            v = data.get(k)
            if isinstance(v, str):
                s = v.replace("Z", "+00:00")
                try:
                    data[k] = datetime.fromisoformat(s)
                except ValueError:
                    pass
        return cls.model_construct(**data)


# ---------------------------------------------------------------------------
# Link model
# ---------------------------------------------------------------------------


class Link(BaseModel):
    """A typed edge between two documents.

    Edges are idempotent: re-inserting the same ``(from_id, to_id, rel)``
    triple is a no-op (same primary key in SQLite).
    """

    model_config = ConfigDict(frozen=True)

    from_id: str
    to_id: str
    rel: str = Field(default="relates-to", min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Search result
# ---------------------------------------------------------------------------


class SearchHit(BaseModel):
    """A single search result with a snippet.

    ``score`` is the raw BM25 score returned by SQLite FTS5 (lower = more
    relevant; not normalised). The CLI and MCP server may normalise it
    before exposing to callers; this model preserves the raw value.
    """

    doc: Document
    snippet: str = Field(default="", description="Body excerpt with markers around matched terms")
    score: float = 0.0
    related: list[RelatedDoc] = Field(default_factory=list, description="1-hop graph neighbors")


# ---------------------------------------------------------------------------
# Related document (graph expansion)
# ---------------------------------------------------------------------------


class RelatedDoc(BaseModel):
    """A document related to a search hit via a graph edge.

    Attached to :class:`SearchHit` when ``expand_graph=True``.
    """

    doc: Document
    rel: str = Field(default="relates-to", description="Relation type from the parent hit")
    direction: Literal["outbound", "inbound"] = Field(
        default="outbound",
        description="Whether the parent hit points outward to this doc, or vice versa",
    )
    hop: int = Field(default=1, description="Hop distance from the parent hit (v0.8 fixed to 1)")
    score: float = Field(default=0.0, description="Decayed score from the parent hit")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class ImportReport(BaseModel):
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class DoctorCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class DoctorReport(BaseModel):
    ok: bool
    checks: list[DoctorCheck] = Field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return "kb doctor: OK"
        bad = [c for c in self.checks if not c.ok]
        lines = ["kb doctor: FAIL", f"  {len(bad)} check(s) failed:"]
        for c in bad:
            lines.append(f"  - {c.name}: {c.detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Type registry (extensibility point)
# ---------------------------------------------------------------------------


class TypeRegistry:
    """Registry of known document types and their default behaviours.

    The built-in types are pre-registered. Users can register custom
    types to:

    - constrain which fields are allowed on a document,
    - override slug generation,
    - attach a Markdown template.

    For v0.1.0 the registry stores **metadata only** — every document is
    still backed by the same SQLite schema. Field-level constraints are
    enforced at the pydantic layer via subclasses (see ``Project``,
    ``Decision``, …); the registry is the lookup table for which subclass
    to use.
    """

    def __init__(self) -> None:
        self._types: dict[str, type[Document]] = {
            "project": Project,
            "decision": Decision,
            "lesson": Lesson,
            "glossary": Glossary,
            "person": Person,
            "faq": Faq,
            "api": Api,
            "runbook": Runbook,
            "release": Release,
        }

    def register(self, type_name: str, model: type[Document]) -> None:
        if not type_name or not type_name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"type_name must be alphanumeric/dash/underscore (got {type_name!r})")
        if type_name in self._types:
            raise ValueError(f"type {type_name!r} already registered")
        self._types[type_name] = model

    def model_for(self, type_name: str) -> type[Document]:
        return self._types.get(type_name, Document)

    def known_types(self) -> Iterable[str]:
        return sorted(self._types.keys())

    def validate(self, type_name: str) -> None:
        if not type_name:
            raise ValidationError("type must be non-empty")
        # Any non-empty type string is allowed; this is a no-op for now
        # but is the hook for future stricter enforcement.


# ---------------------------------------------------------------------------
# Per-type subclasses
# ---------------------------------------------------------------------------


class Project(Document):
    """A project / repo / initiative. Body typically summarises purpose,
    stack, status, owners, repository and deployment URLs. Tag convention: ``<lang>``, ``<framework>``,
    ``<domain>``."""

    type: Literal["project"] = "project"


class Decision(Document):
    """An Architecture Decision Record (ADR). Body should follow the
    MADR-style structure: context → decision → consequences."""

    type: Literal["decision"] = "decision"


class Lesson(Document):
    """A post-mortem or lessons-learned entry. Body should describe the
    incident, the root cause, and the prevention rule."""

    type: Literal["lesson"] = "lesson"


class Glossary(Document):
    """A term definition. Body is the definition; canonical form."""

    type: Literal["glossary"] = "glossary"


class Person(Document):
    """A person the agent should recognise. Body is bio + context.
    Tag convention: ``<role>``, ``<team>``."""

    type: Literal["person"] = "person"


class Faq(Document):
    """A frequently asked question. Title is the question; body is the
    answer."""

    type: Literal["faq"] = "faq"


class Api(Document):
    """An API endpoint documentation. Body typically includes endpoint path,
    HTTP method, request/response examples, authentication requirements.
    Tag convention: ``<service>``, ``<http-method>``, ``<version>``."""

    type: Literal["api"] = "api"


class Runbook(Document):
    """An operational runbook / SOP. Body typically includes step-by-step
    instructions for routine operations, incident response, deployment procedures.
    Tag convention: ``<operation>``, ``<environment>``, ``<service>``."""

    type: Literal["runbook"] = "runbook"


class Release(Document):
    """A release / changelog entry. Body typically includes version number,
    release date, change list, impact, rollback steps.
    Tag convention: ``<version>``, ``<environment>``, ``<service>``."""

    type: Literal["release"] = "release"


# Module-level singleton for convenience
default_registry = TypeRegistry()


__all__ = [
    # types
    "DocumentType",
    "Document",
    "Link",
    "SearchHit",
    "ImportReport",
    "DoctorCheck",
    "DoctorReport",
    # subclasses
    "Project",
    "Decision",
    "Lesson",
    "Glossary",
    "Person",
    "Faq",
    "Api",
    "Runbook",
    "Release",
    # registry
    "TypeRegistry",
    "default_registry",
    # helpers
    "slugify",
    "make_id",
    # exceptions
    "KbMcpError",
    "NotFoundError",
    "DuplicateError",
    "ValidationError",
    "IntegrityError",
]
