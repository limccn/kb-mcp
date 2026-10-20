"""Body-level reference parser for content-level backlinks (v0.8 特性 #4).

Extracts document references from Markdown body content and creates
``references`` links automatically during add/update.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kb_mcp_lite.store.sqlite import SqliteStore

logger = logging.getLogger(__name__)

# Patterns — order matters: markdown links before code refs
_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\(([a-z0-9][a-z0-9/_-]*)\)")
_CODE_REF_PATTERN = re.compile(r"`([a-z0-9][a-z0-9/_-]*)`")


def extract_body_references(
    body: str,
    known_ids: set[str],
    *,
    markdown_links: bool = True,
    code_refs: bool = True,
) -> list[tuple[str, str]]:
    """Extract document references from *body* that match entries in *known_ids*.

    Returns a list of ``(reference_id, syntax_type)`` tuples where
    *syntax_type* is ``"markdown"`` or ``"code"``.

    Conservative strategy:
    - Only matches ``^[a-z0-9][a-z0-9/_-]*$`` candidate IDs.
    - Only includes candidates present in *known_ids*.
    - Skips fenced code blocks (`` ``` ... ``` ``).
    - Inline code `` `id` `` is captured as code refs; markdown links take priority.
    """
    if not body or not known_ids:
        return []

    # 1. Strip fenced code blocks
    body_no_fenced = re.sub(r"```.*?```", "", body, flags=re.DOTALL)

    # 2. Collect markdown link targets first (to exclude from code-ref pass)
    found: list[tuple[str, str]] = []
    link_targets: set[str] = set()

    if markdown_links:
        for m in _LINK_PATTERN.finditer(body_no_fenced):
            target = m.group(2)
            if target in known_ids and target not in link_targets:
                link_targets.add(target)
                found.append((target, "markdown"))

    # 3. Collect code-ref candidates — inline code `` `id` `` references
    if code_refs:
        for m in _CODE_REF_PATTERN.finditer(body_no_fenced):
            target = m.group(1)
            if target in known_ids and target not in link_targets:
                link_targets.add(target)
                found.append((target, "code"))

    return found


def sync_body_references(
    store: SqliteStore,  # noqa: F821 — forward ref
    doc_id: str,
    body: str,
    *,
    rel: str = "references",
) -> int:
    """Parse *body* for references and sync them as links in the store.

    Returns the number of links created (net new).
    """
    # Lazy import to avoid circular dependency at module level

    # Load all known doc IDs (excluding deleted)
    rows = store._conn.execute("SELECT id FROM documents WHERE deleted_at IS NULL").fetchall()
    known_ids = {r["id"] for r in rows}

    refs = extract_body_references(body, known_ids)
    if not refs:
        return 0

    # Get existing outgoing links with this rel type
    existing = store.outgoing_links(doc_id)
    existing_refs = {lnk.to_id for lnk in existing if lnk.rel == rel}

    new_count = 0
    for target_id, _syntax in refs:
        if target_id not in existing_refs:
            try:
                store.link(doc_id, target_id, rel=rel)
                new_count += 1
            except Exception:
                logger.debug(
                    "auto-link: cannot link %s → %s (target may not exist)",
                    doc_id,
                    target_id,
                )

    # Remove stale references (body no longer mentions them)
    ref_targets = {t for t, _ in refs}
    for target_id in existing_refs - ref_targets:
        try:
            store.unlink(doc_id, target_id, rel=rel)
            new_count -= 1  # net count
        except Exception:
            pass

    return new_count
