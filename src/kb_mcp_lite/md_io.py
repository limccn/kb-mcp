"""Markdown I/O: parse frontmatter, render documents, bulk import/export.

This module is the bridge between the on-disk Markdown vault and the
:class:`kb_mcp_lite.store.Store` backend. It only imports from
:mod:`kb_mcp_lite.schema` and :mod:`kb_mcp_lite.store` (the Protocol); it never
touches a concrete backend, which keeps the CLI, MCP server, and
tests decoupled from SQLite.

Public API (defined in :ref:`docs/architecture.md § 4.3 <architecture>`):

.. code-block:: python

    def parse_frontmatter(text: str) -> tuple[Frontmatter, str]: ...
    def render_document(doc: Document, outlinks: list[Link] | None = None) -> str: ...
    def import_dir(store: Store, dir: Path, *, dry_run: bool = False) -> ImportReport: ...
    def export_dir(store: Store, dir: Path, *, force: bool = False, incremental: bool = False) -> int: ...

NFR-S-3 path-traversal guard: any resolved path that escapes the given
directory raises :class:`kb_mcp_lite.schema.ValidationError` with a clear
message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import frontmatter

from kb_mcp_lite.schema import (
    Document,
    DocumentType,
    ImportReport,
    Link,
    ValidationError,
    _TYPE_PREFIX,
    make_id,
    slugify,
)

if TYPE_CHECKING:
    # The Store Protocol lives in ``kb_mcp_lite.store`` (the Protocol
    # module). The Wave 0 directory layout puts both ``store.py`` (the
    # Protocol) and ``store/`` (the implementation package) at the same
    # level; Python's import machinery picks the package, shadowing the
    # Protocol module. We only need the Protocol for static type
    # checking — runtime code duck-types against the Protocol — so the
    # import is guarded with TYPE_CHECKING and annotations stay as
    # strings (PEP 563). See deviations_from_architecture.md.
    from kb_mcp_lite.store import Store  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Frontmatter TypedDict
# ---------------------------------------------------------------------------


class Frontmatter(TypedDict, total=False):
    """Recognised subset of YAML frontmatter keys.

    The TypedDict is ``total=False`` because every key is optional at
    parse time — a file may legitimately have none of them (e.g. a
    Markdown body with no frontmatter block). Unknown keys are passed
    through on the returned dict: :class:`TypedDict` describes only
    the *statically known* shape, not a runtime constraint.
    """

    type: str
    title: str
    tags: list[str]
    source: str
    created_at: str
    updated_at: str
    links: list[dict]


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[Frontmatter, str]:
    """Split YAML frontmatter from Markdown body.

    Args:
        text: Full file contents.

    Returns:
        A ``(frontmatter_dict, body)`` tuple. The frontmatter dict
        preserves every key found in the YAML block (unknown keys are
        passed through). The body is the Markdown content after the
        closing ``---`` line, or the entire input if no frontmatter
        block was present.

    Raises:
        ValueError: if the YAML frontmatter block is malformed. This
            propagates from :mod:`python-frontmatter`; callers in the
            import path catch and report it per-file.
    """
    # Defense: check for Git merge conflict markers first
    if any(marker in text for marker in ("<<<<<<<", "=======", ">>>>>>>")):
        # Build a mock frontmatter so the importer doesn't drop the document
        fm_fallback: Frontmatter = {
            "type": "conflict",
            "title": "Git Sync Conflict",
            "tags": ["conflict", "git-sync"],
        }
        # Keep the entire raw text containing conflict markers as the body
        return fm_fallback, text

    try:
        post = frontmatter.loads(text)
        fm: Frontmatter = dict(post.metadata)  # type: ignore[assignment]
        return fm, post.content
    except Exception as e:
        raise e


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_document(doc: Document, outlinks: list[Link] | None = None) -> str:
    """Render a :class:`Document` to its stable on-disk Markdown form.

    The output has a YAML frontmatter block (keys sorted alphabetically
    by :func:`frontmatter.dumps` for determinism) followed by a blank
    line and the body. Round-trippable via :func:`parse_frontmatter`
    plus :func:`import_dir`.

    When ``outlinks`` is provided, a ``links`` list is added to the
    frontmatter so that import round-trips preserve document
    relationships.

    The body is emitted **verbatim** — Markdown is never HTML-converted
    here. Callers wanting HTML should run their own renderer (e.g.
    ``markdown-it-py``) downstream.
    """
    fm: dict[str, Any] = {
        "type": doc.type,
        "title": doc.title,
        "tags": list(doc.tags),
    }
    if doc.aliases:
        fm["aliases"] = list(doc.aliases)
    if doc.source is not None:
        fm["source"] = doc.source
    if doc.created_at is not None:
        fm["created_at"] = doc.created_at.isoformat()
    if doc.updated_at is not None:
        fm["updated_at"] = doc.updated_at.isoformat()
    if doc.metadata:
        fm["metadata"] = dict(doc.metadata)
    if outlinks:
        fm["links"] = [
            {"to": link.to_id, "rel": link.rel}
            for link in sorted(outlinks, key=lambda lnk: (lnk.to_id, lnk.rel))
        ]

    post = frontmatter.Post(doc.body or "", **fm)
    return frontmatter.dumps(post)


# ---------------------------------------------------------------------------
# Document construction from parsed frontmatter
# ---------------------------------------------------------------------------


def _coerce_tags(value: Any) -> list[str]:
    """Best-effort coercion of the ``tags`` frontmatter value to ``List[str]``.

    Accepts a list (returned as-is with non-string entries stringified)
    or a single string (wrapped in a one-element list). Anything else
    yields an empty list.
    """
    if isinstance(value, list):
        return [str(t) for t in value]
    if isinstance(value, str):
        return [value] if value else []
    return []


def _coerce_links(value: Any) -> list[dict[str, str]]:
    """Coerce the frontmatter ``links`` value to a list of ``{to, rel}`` dicts.

    Accepts a list of dicts. Each dict must have a ``to`` key (str).
    ``rel`` is optional and defaults to ``"relates-to"``.

    Raises:
        ValidationError: if any entry is missing ``to`` or has a
            non-string ``to`` / ``rel``.
    """
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValidationError(f"links[{i}]: expected a dict, got {type(entry).__name__}")
        to = entry.get("to")
        if not to or not isinstance(to, str):
            raise ValidationError(f"links[{i}]: missing or invalid 'to' (expected str, got {to!r})")
        rel = entry.get("rel", "relates-to")
        if not isinstance(rel, str) or not rel.strip():
            raise ValidationError(
                f"links[{i}]: invalid 'rel' (expected non-empty str, got {rel!r})"
            )
        result.append({"to": to, "rel": rel.strip()})
    return result


def _parse_iso_dt(value: Any) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware datetime.

    Accepts a ``datetime`` (returned unchanged) or a string. Bare
    ``Z`` suffixes and naive datetimes are normalised to UTC so the
    Document model sees a consistent shape.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValidationError(f"invalid ISO-8601 datetime {value!r}: {e}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def doc_from_frontmatter(
    fm: Frontmatter,
    body: str,
    source: str | None = None,
    file_id: str | None = None,
) -> Document:
    """Build a :class:`Document` from parsed frontmatter + body.

    Public helper so callers (e.g. tests) can reuse the conversion
    logic. Required fields: ``type`` and ``title``. Optional fields:
    ``tags``, ``created_at``, ``updated_at``. The ``source`` argument
    overrides whatever is in the frontmatter (callers from
    :func:`import_dir` always pass the file's relative path).

    ``file_id`` is the import-time ID derived from the filename
    (relative path minus ``.md`` extension). When provided, it is
    used as the document ID instead of ``make_id(type, title)``.
    Frontmatter may still override by explicitly setting ``id``.

    Raises:
        ValidationError: if required fields are missing or have an
            invalid type / datetime format.
    """
    type_ = fm.get("type")
    title = fm.get("title")
    if not type_:
        raise ValidationError("frontmatter missing required field 'type'")
    if not isinstance(type_, str) or not type_.strip():
        raise ValidationError(f"frontmatter 'type' must be a non-empty string (got {type_!r})")
    if not title:
        raise ValidationError("frontmatter missing required field 'title'")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError(f"frontmatter 'title' must be a non-empty string (got {title!r})")

    aliases_raw = fm.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
    elif isinstance(aliases_raw, list):
        aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]
    else:
        aliases = []

    now = datetime.now(timezone.utc)
    created_at_raw = fm.get("created_at")
    updated_at_raw = fm.get("updated_at")

    # ID priority: 1) explicit frontmatter id, 2) file_id (from filename),
    # 3) auto-generated from type + title.
    explicit_id = fm.get("id")
    if isinstance(explicit_id, str) and explicit_id:
        doc_id = explicit_id
    elif file_id:
        doc_id = file_id
    else:
        doc_id = make_id(type_, title)

    # Metadata extraction: extract explicit 'metadata' key or any extra typed keys
    raw_meta = fm.get("metadata")
    metadata: dict[str, Any] = {}
    if isinstance(raw_meta, dict):
        metadata.update(raw_meta)

    # Catch any extra top-level fields not in core Document attributes
    known_keys = {
        "id",
        "type",
        "title",
        "tags",
        "aliases",
        "source",
        "created_at",
        "updated_at",
        "deleted_at",
        "links",
        "metadata",
    }
    for k, v in fm.items():
        if k not in known_keys and k not in metadata:
            metadata[k] = v

    return Document(
        id=doc_id,
        type=type_,
        title=title,
        body=body or "",
        tags=_coerce_tags(fm.get("tags", [])),
        aliases=aliases,
        source=source,
        metadata=metadata,
        created_at=_parse_iso_dt(created_at_raw) if created_at_raw else now,
        updated_at=_parse_iso_dt(updated_at_raw) if updated_at_raw else now,
    )


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------


def _ensure_within(base: Path, candidate: Path, *, what: str) -> Path:
    """Resolve ``candidate`` against ``base`` and verify it stays inside.

    Returns the resolved :class:`Path`. Raises
    :class:`ValidationError` if the resolved location escapes
    ``base`` (NFR-S-3).
    """
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValidationError(
            f"{what} escapes base directory: {candidate!r} "
            f"resolves to {resolved!r}, outside {base!r}"
        )
    return resolved


# ---------------------------------------------------------------------------
# import_dir
# ---------------------------------------------------------------------------


def import_dir(store: Store, dir: Path, *, dry_run: bool = False) -> ImportReport:
    """Walk ``dir`` recursively, parse ``.md`` files, insert/update by source.

    Hidden files, hidden directories, and non-``.md`` files are
    skipped. Each parsed :class:`Document` is given a ``source``
    attribute set to the file's path relative to ``dir``. The store's
    :meth:`Store.import_many` handles source-based idempotent
    re-import (existing documents with the same ``source`` are
    updated, not duplicated).

    NFR-S-3: any file whose resolved path escapes ``dir`` is rejected
    with :class:`ValidationError` recorded in the returned report.

    Args:
        store: A backend implementing the :class:`Store` Protocol.
        dir: Root directory of the vault to import.
        dry_run: If True, parse and validate every file but do not
            write to ``store``. The returned report's
            ``inserted``/``updated`` counts are zero; ``errors``
            contains parse-time failures.

    Returns:
        An :class:`ImportReport` aggregating per-file outcomes. Parse
        errors are listed in ``errors``; successfully parsed files
        contribute to ``inserted``/``updated`` via the store.
    """
    base = dir.resolve()
    if not base.is_dir():
        raise ValidationError(f"not a directory: {dir!r}")

    docs: list[Document] = []
    errors: list[str] = []
    # Each entry: (from_id, to_id, rel, source_path) — source_path for error messages.
    pending_links: list[tuple[str, str, str, str]] = []

    for root, dirs, files in os.walk(base):
        # Skip hidden directories in-place so walk doesn't descend into them.
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            if not name.endswith(".md"):
                continue
            path = Path(root) / name
            try:
                # NFR-S-3: resolve (canonicalises symlinks) and verify the
                # file remains inside ``base``.
                resolved = path.resolve()
                try:
                    resolved.relative_to(base)
                except ValueError:
                    errors.append(
                        f"{path}: path traversal blocked "
                        f"(resolves to {resolved!r}, outside {base!r})"
                    )
                    continue

                text = path.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                rel_source = str(path.relative_to(base))
                # ID = relative path minus ".md" extension (e.g. "proj/kb-mcp").
                file_id = Path(rel_source).with_suffix("").as_posix()
                doc = doc_from_frontmatter(fm, body, source=rel_source, file_id=file_id)
                docs.append(doc)

                # Collect links from frontmatter.
                raw_links = fm.get("links")
                if raw_links:
                    try:
                        parsed = _coerce_links(raw_links)
                        for link in parsed:
                            pending_links.append((doc.id, link["to"], link["rel"], rel_source))
                    except ValidationError as e:
                        errors.append(f"{path}: {e}")

            except ValidationError as e:
                errors.append(f"{path}: {e}")
            except Exception as e:  # noqa: BLE001 — surface any parse error per-file
                errors.append(f"{path}: {type(e).__name__}: {e}")

    if dry_run or not docs:
        return ImportReport(
            inserted=0,
            updated=0,
            skipped=len(docs) if dry_run else 0,
            errors=list(errors),
        )

    # Delegate to the store. import_many implements source-based dedup
    # ("update by source path" per architecture § 4.3).
    report = store.import_many(docs)

    # Build old-ID → new-ID mapping for imported docs. Links exported by
    # older versions used prefix/slug (e.g. "dec/beta"); after the switch to
    # filename/flat-based IDs those old targets are stale. Map them to the new ID
    # so cross-doc links survive re-import.
    old_id_map: dict[str, str] = {}
    for doc in docs:
        try:
            prefix = _TYPE_PREFIX[DocumentType(doc.type)]
        except (ValueError, KeyError):
            prefix = doc.type
        legacy_id = f"{prefix}/{slugify(doc.title)}"
        if legacy_id != doc.id:
            old_id_map[legacy_id] = doc.id
        old_id = make_id(doc.type, doc.title)
        if old_id != doc.id:
            old_id_map[old_id] = doc.id

    # Create links after all documents are in the store.
    link_errors: list[str] = []
    for from_id, to_id, rel, source_path in pending_links:
        resolved_to = old_id_map.get(to_id, to_id)
        try:
            store.link(from_id, resolved_to, rel=rel)
        except Exception as e:  # noqa: BLE001 — surface per-link error
            link_errors.append(f"{source_path}: link {from_id} -> {resolved_to} ({rel}): {e}")

    return ImportReport(
        inserted=report.inserted,
        updated=report.updated,
        skipped=report.skipped,
        errors=list(errors) + list(report.errors) + link_errors,
    )


# ---------------------------------------------------------------------------
# export_dir
# ---------------------------------------------------------------------------


def _export_candidate(base: Path, doc: Document) -> Path:
    """Return the file ``doc`` exports to (or should be looked up at).

    Uses the recorded ``source`` when set — it is the authoritative path
    written by the last export — and falls back to ``<slug>.md`` (last id
    segment) for never-exported documents. Looking up by slug alone is
    wrong whenever two documents share a slug: the second one exports to
    ``<slug>-2.md``, so its sibling's file would be compared instead.
    """
    if doc.source:
        return base / doc.source
    slug = doc.id.rsplit("/", 1)[-1] or "untitled"
    return base / f"{slug}.md"


def export_dir(store: Store, dir: Path, *, force: bool = False, incremental: bool = False) -> int:
    """Write one ``.md`` per document under ``dir``.

    Filename is the last segment of the document's id (``proj/kb-mcp``
    becomes ``kb-mcp.md``). When multiple documents map to the same
    slug within a single export, a numeric suffix (``-2``, ``-3``, …)
    is appended. Pre-existing files are never overwritten unless
    ``force=True``.

    After a successful write, the document's ``source`` field is
    updated in the store to the new file's path (relative to
    ``dir``). This keeps the import-export round trip idempotent:
    re-importing the exported directory matches documents by source.

    NFR-S-3: every ``doc.source`` is checked up front; any whose
    resolved path escapes ``dir`` raises :class:`ValidationError`
    before any files are written.

    Args:
        store: A backend implementing the :class:`Store` Protocol.
        dir: Destination directory. Created (with parents) if missing.
        force: Overwrite existing files. Default False.

    Returns:
        Count of files written.
    """
    base = dir.resolve()
    base.mkdir(parents=True, exist_ok=True)

    all_docs = store.export_all(include_deleted=True)
    live_docs = [d for d in all_docs if d.deleted_at is None]
    docs = sorted(live_docs, key=lambda d: d.id)
    deleted_docs = [d for d in all_docs if d.deleted_at is not None]
    # 增量导出过滤：只导出更新时间晚于对应文件修改时间的文档
    if incremental:
        filtered = []
        for doc in docs:
            candidate = _export_candidate(base, doc)
            if candidate.exists():
                mtime = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
                if doc.updated_at <= mtime:
                    continue
            filtered.append(doc)
        docs = filtered

    # NFR-S-3: validate every doc's source up front. Bail before any
    # writes if any source escapes the destination.
    for doc in docs:
        if doc.source:
            if os.path.isabs(doc.source):
                raise ValidationError(
                    f"doc {doc.id!r} has absolute source path {doc.source!r} (refusing to export)"
                )
            try:
                (base / doc.source).resolve().relative_to(base)
            except ValueError:
                raise ValidationError(
                    f"doc {doc.id!r} source {doc.source!r} escapes destination {base!r}"
                )

    for doc in deleted_docs:
        if doc.source:
            if os.path.isabs(doc.source):
                raise ValidationError(
                    f"deleted doc {doc.id!r} has absolute source path {doc.source!r} (refusing to export)"
                )
            try:
                (base / doc.source).resolve().relative_to(base)
            except ValueError:
                raise ValidationError(
                    f"deleted doc {doc.id!r} source {doc.source!r} escapes destination {base!r}"
                )

    # Paths claimed by live documents. A soft-deleted doc can share a
    # slug with a live sibling — both map to the same file — so a file
    # claimed by a live doc must never be removed here.
    live_claims = {_export_candidate(base, d) for d in live_docs}

    # Delete soft-deleted documents' files from the filesystem
    for doc in deleted_docs:
        if doc.source:
            candidate = _export_candidate(base, doc)
            if candidate not in live_claims and candidate.is_file():
                candidate.unlink()

    # Seed claimed filenames from live docs skipped by the incremental
    # filter, so collision suffixes stay stable across runs: a doc that
    # previously exported as ``<slug>-2.md`` must not grab ``<slug>.md``
    # when its sibling is not part of this run.
    exported_ids = {d.id for d in docs}
    used_slugs: set[str] = {
        _export_candidate(base, d).name for d in live_docs if d.id not in exported_ids
    }
    written = 0

    for doc in docs:
        # Slug = last id segment. Fall back to "untitled" for the empty case.
        slug = doc.id.rsplit("/", 1)[-1] or "untitled"

        # In-run collision: append -2, -3, ... until unique among
        # the filenames we've already claimed in this export.
        candidate = base / f"{slug}.md"
        n = 2
        while candidate.name in used_slugs:
            candidate = base / f"{slug}-{n}.md"
            n += 1
            if n > 10_000:
                raise ValidationError(f"too many filename collisions for slug {slug!r}")
        used_slugs.add(candidate.name)

        # Refuse to clobber a pre-existing file unless force=True.
        if candidate.exists() and not force:
            raise ValidationError(
                f"refusing to overwrite existing file {candidate} (use force=True to overwrite)"
            )

        # Ensure parent directory exists (important for nested paths
        # like "sub/nested.md" when filename is used as document ID).
        candidate.parent.mkdir(parents=True, exist_ok=True)

        candidate.write_text(
            render_document(doc, outlinks=store.outlinks(doc.id)),
            encoding="utf-8",
        )
        written += 1

        # Update doc.source in the store so a subsequent import from
        # ``base`` matches by source. Uses update_source(): a plain
        # update() would bump ``updated_at`` after the file was written,
        # leaving the doc newer than its export and defeating incremental
        # export. Best-effort: if the backend rejects the update (e.g.
        # validation rule), the export still succeeded; just log it as a
        # per-doc warning.
        try:
            rel = candidate.relative_to(base)
            store.update_source(doc.id, str(rel))
        except ValidationError as e:
            # Append as a non-fatal note; the on-disk file is fine.
            # We surface this through a custom side-channel rather
            # than changing the return type.
            import warnings

            warnings.warn(f"could not update source for {doc.id!r}: {e}")

    return written


# ---------------------------------------------------------------------------
# pending_export
# ---------------------------------------------------------------------------


@dataclass
class PendingExport:
    """Store-vs-export diff produced by :func:`pending_export`.

    Each field holds document ids:

    - ``added``: live document whose export file is missing under ``dir``.
    - ``modified``: exported file content differs from a fresh render.
    - ``deleted``: soft-deleted document whose ``source`` file still exists
      under ``dir`` (the next export would remove it).
    """

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


def pending_export(store: Store, dir: Path) -> PendingExport:
    """Compare the store against the exported Markdown directory.

    Answers "would an export change anything right now?" without writing
    any files. Detection is content-based — a document counts as modified
    only when its exported file differs from a fresh
    :func:`render_document` render — so the result is insensitive to mtime
    and clock effects (import bumps ``updated_at``; export writes back
    ``source`` after the file lands on disk).

    Files are located via :func:`_export_candidate` (recorded ``source``
    first, ``<slug>.md`` fallback), mirroring :func:`export_dir`. A
    soft-deleted document counts as pending deletion only when its file
    exists and is not claimed by a live sibling sharing the same slug.
    """
    base = dir.resolve()
    pending = PendingExport()
    all_docs = store.export_all(include_deleted=True)
    live_claims = {_export_candidate(base, d) for d in all_docs if d.deleted_at is None}
    for doc in all_docs:
        if doc.deleted_at is not None:
            if doc.source:
                candidate = _export_candidate(base, doc)
                if candidate not in live_claims and candidate.is_file():
                    pending.deleted.append(doc.id)
            continue
        candidate = _export_candidate(base, doc)
        if not candidate.is_file():
            pending.added.append(doc.id)
            continue
        rendered = render_document(doc, outlinks=store.outlinks(doc.id))
        if not _same_export_content(candidate.read_text(encoding="utf-8"), rendered):
            pending.modified.append(doc.id)
    return pending


#: Frontmatter keys ``export_dir`` mutates in the store *after* the file
#: is written (``source`` write-back, and the ``updated_at`` bump that
#: comes with it). The on-disk file always lags one write-back behind,
#: so comparing them would flag every exported doc as modified.
_VOLATILE_FRONTMATTER_KEYS = frozenset({"source", "updated_at"})


def _same_export_content(on_disk: str, rendered: str) -> bool:
    """Compare exported Markdown, ignoring volatile frontmatter keys.

    Both sides are parsed and compared as logical content (frontmatter
    minus :data:`_VOLATILE_FRONTMATTER_KEYS`, plus the body); falls back
    to a byte comparison if either side fails to parse.
    """
    try:
        fm_disk, body_disk = parse_frontmatter(on_disk)
        fm_rendered, body_rendered = parse_frontmatter(rendered)
    except Exception:  # noqa: BLE001 — unparseable file counts as byte-diff
        return on_disk == rendered
    for key in _VOLATILE_FRONTMATTER_KEYS:
        fm_disk.pop(key, None)  # type: ignore[misc]
        fm_rendered.pop(key, None)  # type: ignore[misc]
    return fm_disk == fm_rendered and body_disk == body_rendered


__all__ = [
    "Frontmatter",
    "PendingExport",
    "parse_frontmatter",
    "render_document",
    "doc_from_frontmatter",
    "import_dir",
    "export_dir",
    "pending_export",
]
