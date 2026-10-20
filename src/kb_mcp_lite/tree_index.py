"""Per-directory index documents that turn a converted tree into a graph.

``kb ingest --vault`` runs :func:`enrich_tree` between conversion and
import (task ``10-12-tree-graph-links``, design D2-D4): every directory of
the converted tree that carries Markdown content gets a generated
``kb-index.md`` whose frontmatter ``links`` (rel ``contains``) point at
the directory's direct documents and at the index documents of its direct
subdirectories. ``md_io.import_dir`` then turns those frontmatter links
into store edges -- without this module a converted tree imports as a bag
of isolated documents.

Determinism is a contract: sorted ordering, no timestamps, and a re-run
over an already-enriched tree is byte-identical. The generated files
carry ``metadata.generated: kb-ingest`` in their frontmatter; that marker
is what lets a re-run overwrite its own output while refusing to touch
any file it did not write.

ID space: the index id is ``<dir>/kb-index`` (root: ``kb-index``), where
``<dir>`` is the converter's per-segment slug -- :func:`_slug_segment`
mirrors ``convert_docs.slug_id`` so the index ids line up with the id
prefixes the converted documents already carry (they must: frontmatter
links are checked against real ids). A collision with the converted tree
is a *named skip*, never an error and never silent.

Known boundary (design D5): import only ever adds edges, so deleting a
subtree from the source and re-importing leaves the old ``contains``
edges behind; this module does not clean up edges or stale documents.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from kb_mcp_lite.md_io import parse_frontmatter
from kb_mcp_lite.schema import ValidationError

__all__ = ["EnrichResult", "EnrichSkip", "enrich_tree"]


#: File name every generated index document has inside its directory.
_INDEX_NAME = "kb-index"

#: Relation every generated ``links`` entry carries (see relations.py).
_CONTAINS = "contains"

#: Frontmatter marker identifying files this module generated.
_GENERATED_BY = "kb-ingest"

#: kb's document-id charset (schema.py's ``_check_id`` validator, mirrored
#: by the converter; rewritten here for the same reason convert_docs.py
#: rewrites it -- the ingestion side must not import kb_mcp_lite).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")


@dataclass(frozen=True)
class EnrichSkip:
    """One directory whose index was not generated, and why.

    ``dir`` is the root-relative posix directory (``""`` = the tree root).
    """

    dir: str
    reason: str


@dataclass
class EnrichResult:
    """What :func:`enrich_tree` did: counts, named skips, edge count."""

    generated: int
    skipped: list[EnrichSkip]
    edges: int

    def summary(self) -> str:
        """One human line plus one named line per skip -- skips are never silent."""
        lines = [
            f"tree index: {self.generated} generated, "
            f"{len(self.skipped)} skipped, {self.edges} edges"
        ]
        for skip in self.skipped:
            lines.append(f"  skip {skip.dir or '.'}: {skip.reason}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _Entry:
    """One ``.md`` file found in the tree (the inventory row)."""

    rel: str  # root-relative posix path
    dir_rel: str  # parent directory, root-relative posix ("" = root)
    doc_id: str  # frontmatter id, else the import-time file id
    generated: bool  # carries this module's marker


def _slug_segment(segment: str) -> str:
    """One path segment -> id segment, mirroring ``convert_docs.slug_id``.

    ASCII is kept, everything else collapses to ``-``; a segment with no
    usable ASCII falls back to ``cjk-<sha1(original segment)[:8]>``. The
    rule is duplicated on purpose: the index id must equal what the
    converter computed for the same path, and the converter cannot be
    imported (ingestion never depends on kbmcp, and the frozen exe
    excludes it).
    """
    ascii_part = re.sub(r"[^a-z0-9]+", "-", segment.lower()).strip("-")[:40].strip("-")
    if ascii_part and _ID_RE.match(ascii_part):
        return ascii_part
    return "cjk-" + hashlib.sha1(segment.encode("utf-8")).hexdigest()[:8]


def _index_id(dir_rel: str) -> str:
    """The generated index's document id for a root-relative directory."""
    if not dir_rel:
        return _INDEX_NAME
    prefix = "/".join(_slug_segment(seg) for seg in dir_rel.split("/"))
    return f"{prefix}/{_INDEX_NAME}"


def _parent_dir(dir_rel: str) -> str:
    """The parent of a root-relative posix directory ("" stays "")."""
    parent = Path(dir_rel).parent.as_posix()
    return "" if parent == "." else parent


def _scan(base: Path) -> list[_Entry]:
    """Inventory every ``.md`` file under *base* (hidden entries skipped).

    Mirrors ``md_io.import_dir``'s walk so the generator and the importer
    agree on what the tree contains: sorted, no hidden files/directories,
    ``.md`` only. An unreadable or unparsable file counts as a plain
    document (never as generated output) with its path-derived id.
    """
    entries: list[_Entry] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            path = Path(root) / name
            rel = path.relative_to(base).as_posix()
            fm: dict = {}
            try:
                fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 -- unparsable file is a plain document
                pass
            doc_id = fm.get("id")
            if not (isinstance(doc_id, str) and doc_id):
                doc_id = rel[: -len(".md")]  # import_dir's file_id fallback
            meta = fm.get("metadata")
            generated = (
                fm.get("type") == "index"
                and isinstance(meta, dict)
                and meta.get("generated") == _GENERATED_BY
            )
            entries.append(
                _Entry(
                    rel=rel,
                    dir_rel=_parent_dir(rel),
                    doc_id=doc_id,
                    generated=generated,
                )
            )
    return entries


def enrich_tree(tree: Path, *, root_title: str | None = None) -> EnrichResult:
    """Generate ``kb-index.md`` documents for every directory with content.

    The directories of interest are the ancestor closure of the tree's
    (non-generated) documents: a directory whose subtree holds at least
    one document gets an index, even if all of its own documents live in
    subdirectories -- the child slots are what keep the graph connected.

    Each generated index links, with rel ``contains``, the directory's
    direct documents plus one slot per direct subdirectory whose index id
    exists in the final id set (normal case: the child's own generated
    index). Links carry the real ids; the body lists the items by
    root-relative ``.md`` path only -- no bare ids, no Markdown links --
    so import's body auto-``references`` cannot pick anything up and the
    vault's edges are exactly the ``contains`` set.

    Returns an :class:`EnrichResult`; a directory whose target file/id is
    already occupied by something this module did not generate is recorded
    as a named :class:`EnrichSkip` and left untouched (not an error).
    A tree that is not a directory raises :class:`ValidationError`.
    """
    base = Path(tree).resolve()
    if not base.is_dir():
        raise ValidationError(f"not a directory: {tree!r}")
    title = root_title if root_title is not None else base.name

    entries = _scan(base)
    by_rel = {e.rel: e for e in entries}
    documents = [e for e in entries if not e.generated]
    document_ids = {e.doc_id for e in documents}

    dirs_of_interest: set[str] = set()
    for entry in documents:
        d = entry.dir_rel
        while True:
            dirs_of_interest.add(d)
            if not d:
                break
            d = _parent_dir(d)

    # Phase 1: skip decisions, in sorted directory order. Order matters
    # only for the pathological case of two directories slugging to the
    # same id: the first one sorted claims it.
    generated_dirs: list[str] = []
    claimed: dict[str, str] = {}  # index id -> dir that claimed it
    skipped: list[EnrichSkip] = []
    for dir_rel in sorted(dirs_of_interest):
        target_rel = f"{dir_rel}/{_INDEX_NAME}.md" if dir_rel else f"{_INDEX_NAME}.md"
        target_id = _index_id(dir_rel)
        occupant = by_rel.get(target_rel)
        ours = occupant is not None and occupant.generated and occupant.doc_id == target_id
        if (base / target_rel).exists() and not ours:
            skipped.append(
                EnrichSkip(
                    dir_rel,
                    f"'{target_rel}' already exists and was not generated by kb-ingest",
                )
            )
            continue
        if target_id in document_ids or target_id in claimed:
            if target_id in document_ids:
                other_rel = next(e.rel for e in documents if e.doc_id == target_id)
            else:
                other_dir = claimed[target_id]
                other_rel = (
                    f"{other_dir}/{_INDEX_NAME}.md" if other_dir else f"{_INDEX_NAME}.md"
                )
            skipped.append(
                EnrichSkip(dir_rel, f"id '{target_id}' is already used by '{other_rel}'")
            )
            continue
        generated_dirs.append(dir_rel)
        claimed[target_id] = dir_rel

    # Phase 2: render and write. ``final_ids`` freezes what exists once
    # this run is done; a child slot is linked iff its id is in it.
    final_ids = document_ids | set(claimed)
    edges = 0
    for dir_rel in generated_dirs:
        target_rel = f"{dir_rel}/{_INDEX_NAME}.md" if dir_rel else f"{_INDEX_NAME}.md"
        items: list[tuple[str, str]] = [
            (e.rel, e.doc_id) for e in documents if e.dir_rel == dir_rel
        ]
        for other_dir in dirs_of_interest:
            if other_dir and _parent_dir(other_dir) == dir_rel:
                slot_id = _index_id(other_dir)
                if slot_id in final_ids:
                    items.append((f"{other_dir}/{_INDEX_NAME}.md", slot_id))
        items.sort(key=lambda item: item[0])

        dir_title = title if dir_rel == "" else dir_rel
        fm = {
            "id": _index_id(dir_rel),
            "type": "index",
            "title": dir_title,
            "metadata": {"generated": _GENERATED_BY, "dir": dir_rel},
            "links": [{"to": item_id, "rel": _CONTAINS} for _, item_id in items],
        }
        head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
        body_lines = [f"# {dir_title}", ""] + [f"- {rel}" for rel, _ in items]
        body = "\n".join(body_lines) + "\n"

        target = base / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"---\n{head}---\n\n{body}", encoding="utf-8")
        edges += len(items)

    return EnrichResult(generated=len(generated_dirs), skipped=skipped, edges=edges)