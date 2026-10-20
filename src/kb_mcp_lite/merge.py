"""Smart 3-way merger for Git conflict resolution in Markdown knowledge bases.

Resolves conflicts in Markdown Frontmatter (sets union for tags/aliases/links)
and provides automated resolution strategies for body conflicts.
"""

from __future__ import annotations

import re
from typing import Any
import yaml


def merge_frontmatter_dicts(
    ours: dict[str, Any],
    theirs: dict[str, Any],
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform a 3-way/2-way merge of two Frontmatter metadata dicts.

    Strategies:
    - tags: Set union of ours + theirs
    - aliases: Set union of ours + theirs
    - links: Deduplicated list union by (to, rel)
    - metadata: Deep merge keys, ours overwrites base, theirs overwrites base
    - title / type / other scalar: Prefer ours if changed from base, else theirs
    """
    base = base or {}
    merged: dict[str, Any] = dict(base)

    # 1. Merge scalar types and titles
    for k in ("type", "title", "source"):
        val_ours = ours.get(k)
        val_theirs = theirs.get(k)
        val_base = base.get(k)
        if val_ours != val_base and val_ours is not None:
            merged[k] = val_ours
        elif val_theirs is not None:
            merged[k] = val_theirs

    # 2. Merge tags (set union)
    tags_ours = set(ours.get("tags") or [])
    tags_theirs = set(theirs.get("tags") or [])
    tags_base = set(base.get("tags") or [])
    merged_tags = sorted(list((tags_ours | tags_theirs) - (tags_base - (tags_ours | tags_theirs))))
    merged["tags"] = merged_tags

    # 3. Merge aliases (set union)
    aliases_ours = set(ours.get("aliases") or [])
    aliases_theirs = set(theirs.get("aliases") or [])
    merged["aliases"] = sorted(list(aliases_ours | aliases_theirs))

    # 4. Merge links (deduplicated list of dicts)
    links_ours = ours.get("links") or []
    links_theirs = theirs.get("links") or []
    seen_links = set()
    merged_links = []
    for lnk in list(links_ours) + list(links_theirs):
        if isinstance(lnk, dict) and "to" in lnk:
            key = (lnk.get("to"), lnk.get("rel", "relates-to"))
            if key not in seen_links:
                seen_links.add(key)
                merged_links.append(lnk)
    if merged_links:
        merged["links"] = merged_links

    # 5. Merge metadata dict
    meta_ours = ours.get("metadata") or {}
    meta_theirs = theirs.get("metadata") or {}
    meta_base = base.get("metadata") or {}
    merged_meta = dict(meta_base)
    merged_meta.update(meta_theirs)
    merged_meta.update(meta_ours)
    if merged_meta:
        merged["metadata"] = merged_meta

    return merged


def resolve_git_conflict_text(conflict_text: str) -> tuple[bool, str]:
    """Attempt to resolve a git-conflicted markdown document automatically.

    Extracts <<<<<<< ours, =======, >>>>>>> theirs blocks.
    Returns (resolved: bool, result_text: str).
    """
    pattern = re.compile(r"<<<<<<<[^\n]*\n(.*?)\n=======\n(.*?)\n>>>>>>>[^\n]*", re.DOTALL)

    if not pattern.search(conflict_text):
        return False, conflict_text

    # Check if conflict is only in frontmatter or body
    # Simple strategy: If conflict block starts with '---' (frontmatter), attempt yaml merge
    def replace_conflict(match: re.Match) -> str:
        ours_part = match.group(1).strip()
        theirs_part = match.group(2).strip()

        # Try YAML parsing
        try:
            ours_yaml = yaml.safe_load(ours_part)
            theirs_yaml = yaml.safe_load(theirs_part)
            if isinstance(ours_yaml, dict) and isinstance(theirs_yaml, dict):
                merged = merge_frontmatter_dicts(ours_yaml, theirs_yaml)
                return yaml.dump(merged, sort_keys=False).strip()
        except Exception:
            pass

        # If body conflict, keep both with clear note if non-identical
        if ours_part == theirs_part:
            return ours_part
        return f"{ours_part}\n\n{theirs_part}"

    resolved_content = pattern.sub(replace_conflict, conflict_text)
    # Check if remaining conflict markers exist
    still_has_conflict = any(m in resolved_content for m in ("<<<<<<<", "=======", ">>>>>>>"))
    return (not still_has_conflict), resolved_content
