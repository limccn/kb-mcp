"""Unit tests for the new Git bi-directional sync and conflict defense features."""

from __future__ import annotations


from kb_mcp_lite.md_io import parse_frontmatter


def test_parse_frontmatter_defends_git_conflict() -> None:
    """Verify that file text containing git merge conflict markers is handled safely
    without raising exceptions, fallback to type='conflict' and keeping the text in body.
    """
    conflict_text = """<<<<<<< HEAD
---
type: project
title: My Project Local
tags: [local]
---
Local body content
=======
---
type: project
title: My Project Remote
tags: [remote]
---
Remote body content
>>>>>>> main
"""
    fm, body = parse_frontmatter(conflict_text)

    assert fm["type"] == "conflict"
    assert fm["title"] == "Git Sync Conflict"
    assert "conflict" in fm["tags"]
    # The body must contain the whole raw text with markers preserved
    assert "<<<<<<< HEAD" in body
    assert "=======" in body
    assert ">>>>>>> main" in body
