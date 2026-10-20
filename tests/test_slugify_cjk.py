"""Tests for slugify() CJK fallback (kb-mcp-lite v0.2.2)."""

from kb_mcp_lite.schema import slugify, make_id


def test_slugify_ascii_unchanged():
    """Pure ASCII titles produce the same slug as v0.2.1 and earlier."""
    assert slugify("Use SQLite FTS5!") == "use-sqlite-fts5"
    assert slugify("kb-mcp") == "kb-mcp"
    assert slugify("Hello World") == "hello-world"


def test_slugify_mixed_appends_hash_suffix():
    """Mixed CJK+ASCII keeps the ASCII part but appends a hash suffix to avoid collisions."""
    s1 = slugify("kb-mcp 项目")
    s2 = slugify("kb-mcp 部署")
    assert s1.startswith("kb-mcp-") and len(s1) == len("kb-mcp-") + 8
    assert s2.startswith("kb-mcp-") and len(s2) == len("kb-mcp-") + 8
    assert s1 != s2, "different mixed CJK+ASCII titles must have different hashes"

    p1 = slugify("Props 属性")
    assert p1.startswith("props-") and len(p1) == len("props-") + 8


def test_slugify_cjk_fallback_is_stable_hash():
    """Pure-CJK titles now produce a stable cjk-<hash> slug instead of empty."""
    s1 = slugify("项目概述")
    s2 = slugify("项目概述")
    assert s1 == s2, "must be idempotent"
    assert s1.startswith("cjk-")
    assert len(s1) == 12, f"expected 12 chars (cjk- + 8 hex), got {len(s1)}: {s1!r}"


def test_slugify_digit_only_fallback():
    """Pure-digit slugs (likely truncated CJK) also trigger fallback."""
    s = slugify("1. 概述")
    assert s.startswith("cjk-") and len(s) == 12
    # And the bare number is rejected (would collide on every chapter).
    assert "1" != s


def test_slugify_different_cjk_different_hash():
    """Different CJK titles must produce different slugs."""
    a = slugify("项目概述")
    b = slugify("技术栈")
    assert a != b


def test_slugify_empty_falls_back():
    """Empty string also falls back (an empty slug would collide on every
    empty-title doc of the same type). The fallback is stable."""
    s = slugify("")
    assert s.startswith("cjk-") and len(s) == 12


def test_make_id_with_cjk_fallback():
    """make_id returns flat slug without slash prefix."""
    assert make_id("reference", "项目概述") == "cjk-cb580c2c"
    assert make_id("reference", "1. 概述") == "cjk-8247761e"


def test_make_id_ascii_unchanged():
    """ASCII titles produce flat id without prefix."""
    assert make_id("project", "kb-mcp") == "kb-mcp"
    assert make_id("reference", "use-sqlite-fts5") == "use-sqlite-fts5"


def test_slugify_long_title_truncated():
    """Overly long titles are truncated at word boundaries."""
    title = "项目公共组件实战指南 (ChartContainer / ExportModal / FileImportModal / InfiniteSelect / VirtualTable)"
    s = slugify(title)
    assert s == "chartcontainer-exportmodal-4f4a1fa1"
    assert len(s) <= 40

