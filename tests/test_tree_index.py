"""Tests for the tree-index generator (task 10-12-tree-graph-links, D2-D4).

Fixtures are temporary trees of hand-written Markdown that imitate a
converter output (frontmatter ids as ``convert_docs.py`` would assign
them). The generator is exercised in-process; the import side of the
contract (fm ``links`` -> store edges) lives in the e2e suite.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from kb_mcp_lite.md_io import parse_frontmatter
from kb_mcp_lite.schema import ValidationError
from kb_mcp_lite.tree_index import EnrichSkip, enrich_tree

#: kb's id charset (schema.py ``_check_id``).
ID_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")


def _write_md(
    root: Path,
    rel: str,
    *,
    doc_id: str | None = None,
    doc_type: str = "spec",
    title: str | None = None,
    metadata: dict | None = None,
    body: str = "body\n",
) -> Path:
    """Write one frontmatter Markdown file under *root*."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm: dict = {"type": doc_type, "title": title or rel}
    if doc_id is not None:
        fm["id"] = doc_id
    if metadata is not None:
        fm["metadata"] = metadata
    head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.write_text(f"---\n{head}---\n\n{body}", encoding="utf-8")
    return path


def _read_fm(path: Path) -> tuple[dict, str]:
    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return dict(fm), body


def _raw_body(path: Path) -> str:
    """The on-disk body, byte-exact (frontmatter parsing normalises it)."""
    text = path.read_text(encoding="utf-8")
    return text.split("\n---\n", 1)[1][1:]


def _sample_tree(root: Path) -> None:
    _write_md(root, "alpha.md", doc_id="alpha")
    _write_md(root, "beta.md", doc_id="beta")
    _write_md(root, "reports/summary.md", doc_id="reports/summary")
    _write_md(root, "reports/annual.md", doc_id="reports/annual")
    _write_md(root, "sub/ref/table.md", doc_id="sub/ref/table")


def _all_links(tree: Path) -> list[dict]:
    """Links of every file the generator itself wrote (marker-checked)."""
    links: list[dict] = []
    for path in sorted(tree.rglob("kb-index.md")):
        fm, _ = _read_fm(path)
        if fm.get("metadata", {}).get("generated") == "kb-ingest":
            links.extend(fm.get("links", []))
    return links


class TestGeneration:
    def test_one_index_per_content_directory(self, tmp_path: Path) -> None:
        tree = tmp_path / "fx"
        _sample_tree(tree)
        result = enrich_tree(tree, root_title="fx")
        assert result.generated == 4  # "", reports, sub, sub/ref
        assert result.skipped == []
        assert result.edges == 8
        for rel in (
            "kb-index.md",
            "reports/kb-index.md",
            "sub/kb-index.md",
            "sub/ref/kb-index.md",
        ):
            assert (tree / rel).is_file(), rel

    def test_index_frontmatter_shape(self, tmp_path: Path) -> None:
        tree = tmp_path / "fx"
        _sample_tree(tree)
        enrich_tree(tree, root_title="My Tree")

        fm, _ = _read_fm(tree / "reports/kb-index.md")
        assert fm["id"] == "reports/kb-index"
        assert fm["type"] == "index"
        assert fm["title"] == "reports"
        assert fm["metadata"] == {"generated": "kb-ingest", "dir": "reports"}
        assert fm["links"] == [
            {"to": "reports/annual", "rel": "contains"},
            {"to": "reports/summary", "rel": "contains"},
        ]
        assert _raw_body(tree / "reports/kb-index.md") == (
            "# reports\n\n- reports/annual.md\n- reports/summary.md\n"
        )

        root_fm, _ = _read_fm(tree / "kb-index.md")
        assert root_fm["id"] == "kb-index"
        assert root_fm["type"] == "index"
        assert root_fm["title"] == "My Tree"
        assert root_fm["metadata"] == {"generated": "kb-ingest", "dir": ""}
        assert root_fm["links"] == [
            {"to": "alpha", "rel": "contains"},
            {"to": "beta", "rel": "contains"},
            {"to": "reports/kb-index", "rel": "contains"},
            {"to": "sub/kb-index", "rel": "contains"},
        ]
        assert _raw_body(tree / "kb-index.md") == (
            "# My Tree\n\n- alpha.md\n- beta.md\n"
            "- reports/kb-index.md\n- sub/kb-index.md\n"
        )

    def test_root_title_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        tree = tmp_path / "fx"
        _sample_tree(tree)
        enrich_tree(tree)
        fm, _ = _read_fm(tree / "kb-index.md")
        assert fm["title"] == "fx"

    def test_ancestor_closure_and_hidden_skips(self, tmp_path: Path) -> None:
        """``sub`` has no direct document; its subtree does -- still indexed."""
        tree = tmp_path / "fx"
        _write_md(tree, "sub/ref/table.md", doc_id="sub/ref/table")
        (tree / "plain.txt").write_text("not markdown\n", encoding="utf-8")
        (tree / "empty").mkdir()
        _write_md(tree, ".hidden.md", doc_id="hidden")
        _write_md(tree, ".hiddendir/inner.md", doc_id="hidden-inner")

        result = enrich_tree(tree)
        assert result.generated == 3  # "", sub, sub/ref
        assert result.edges == 3
        assert (tree / "sub/kb-index.md").is_file()
        assert not (tree / "empty/kb-index.md").exists()
        assert not (tree / ".hiddendir/kb-index.md").exists()
        fm, _ = _read_fm(tree / "sub/kb-index.md")
        assert fm["links"] == [{"to": "sub/ref/kb-index", "rel": "contains"}]

    def test_cjk_directory_ids(self, tmp_path: Path) -> None:
        """Japanese directory segments slug exactly like the converter's.

        Literals pin ``cjk-<sha1(segment)[:8]>``: sha1("資料")[:8] =
        7d1895fb, sha1("概要")[:8] = 7a52d716.
        """
        tree = tmp_path / "fx"
        _write_md(tree, "資料/概要.md", doc_id="cjk-7d1895fb/cjk-7a52d716")
        result = enrich_tree(tree)
        assert result.generated == 2
        assert (tree / "資料/kb-index.md").is_file()
        fm, _ = _read_fm(tree / "資料/kb-index.md")
        assert fm["id"] == "cjk-7d1895fb/kb-index"
        assert fm["metadata"] == {"generated": "kb-ingest", "dir": "資料"}
        assert fm["links"] == [{"to": "cjk-7d1895fb/cjk-7a52d716", "rel": "contains"}]

    def test_generated_ids_match_id_re(self, tmp_path: Path) -> None:
        tree = tmp_path / "fx"
        _sample_tree(tree)
        _write_md(tree, "資料/概要.md", doc_id="cjk-7d1895fb/cjk-7a52d716")
        enrich_tree(tree)
        for path in sorted(tree.rglob("kb-index.md")):
            fm, _ = _read_fm(path)
            assert ID_RE.match(fm["id"]), (path, fm["id"])
            for link in fm["links"]:
                assert ID_RE.match(link["to"]), (path, link)

    def test_body_lists_paths_not_ids(self, tmp_path: Path) -> None:
        """No backticks / Markdown links in generated bodies (D3: no references)."""
        tree = tmp_path / "fx"
        _sample_tree(tree)
        enrich_tree(tree, root_title="fx")
        for path in sorted(tree.rglob("kb-index.md")):
            _, body = _read_fm(path)
            assert "`" not in body
            assert "](" not in body
            lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
            for line in lines:
                assert re.fullmatch(r"- [^\s]+\.md", line), (path, line)

    def test_regenerate_is_byte_identical(self, tmp_path: Path) -> None:
        tree = tmp_path / "fx"
        _sample_tree(tree)
        first = enrich_tree(tree, root_title="fx")

        def digests() -> dict[str, str]:
            return {
                p.relative_to(tree).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(tree.rglob("*.md"))
            }

        before = digests()
        second = enrich_tree(tree, root_title="fx")
        assert second.generated == first.generated == 4
        assert second.skipped == []  # our own files are recognised, not collisions
        assert second.edges == first.edges == 8
        assert digests() == before


class TestCollisions:
    def test_converted_index_doc_coexists(self, tmp_path: Path) -> None:
        """Source ``index.docx`` converts to ``index.md`` -- no clash with kb-index."""
        tree = tmp_path / "fx"
        _write_md(tree, "reports/index.md", doc_id="reports/index")
        _write_md(tree, "reports/summary.md", doc_id="reports/summary")
        result = enrich_tree(tree, root_title="fx")
        assert result.skipped == []
        fm, _ = _read_fm(tree / "reports/kb-index.md")
        assert fm["links"] == [
            {"to": "reports/index", "rel": "contains"},
            {"to": "reports/summary", "rel": "contains"},
        ]

    def test_occupied_path_skip_is_named(self, tmp_path: Path) -> None:
        """A converted ``kb-index.md`` occupies the slot: named skip, no write."""
        tree = tmp_path / "fx"
        _write_md(tree, "alpha.md", doc_id="alpha")
        _write_md(tree, "sub/note.md", doc_id="sub/note")
        _write_md(tree, "sub/kb-index.md", doc_id="sub/kb-index")  # converted
        _write_md(tree, "sub/ref/table.md", doc_id="sub/ref/table")

        result = enrich_tree(tree, root_title="fx")
        assert result.generated == 2  # "", sub/ref
        assert result.edges == 3
        assert result.skipped == [
            EnrichSkip(
                "sub",
                "'sub/kb-index.md' already exists and was not generated by kb-ingest",
            )
        ]
        lines = result.summary().splitlines()
        assert lines[0] == "tree index: 2 generated, 1 skipped, 3 edges"
        assert lines[1] == (
            "  skip sub: 'sub/kb-index.md' already exists and was not generated by kb-ingest"
        )

        # The occupier was not overwritten and is not an index.
        fm, _ = _read_fm(tree / "sub/kb-index.md")
        assert fm["type"] == "spec"
        assert fm["id"] == "sub/kb-index"

        # The parent still links the slot id (it exists -> no dangling link)...
        root_fm, _ = _read_fm(tree / "kb-index.md")
        assert {"to": "sub/kb-index", "rel": "contains"} in root_fm["links"]
        # ...but no generated index links sub/ref/kb-index (its only slot
        # would have been inside the skipped sub index).
        assert all(link["to"] != "sub/ref/kb-index" for link in _all_links(tree))

    def test_occupied_id_without_path_skip_is_named(self, tmp_path: Path) -> None:
        """An id taken by a document elsewhere also skips -- and the slot links."""
        tree = tmp_path / "fx"
        _write_md(tree, "alpha.md", doc_id="alpha")
        _write_md(tree, "stolen/keep.md", doc_id="sub/kb-index")
        _write_md(tree, "sub/note.md", doc_id="sub/note")

        result = enrich_tree(tree, root_title="fx")
        assert result.skipped == [
            EnrichSkip("sub", "id 'sub/kb-index' is already used by 'stolen/keep.md'")
        ]
        assert not (tree / "sub/kb-index.md").exists()
        root_fm, _ = _read_fm(tree / "kb-index.md")
        assert {"to": "sub/kb-index", "rel": "contains"} in root_fm["links"]


class TestDegenerateTrees:
    def test_empty_tree_is_a_no_op(self, tmp_path: Path) -> None:
        tree = tmp_path / "fx"
        tree.mkdir()
        result = enrich_tree(tree, root_title="fx")
        assert (result.generated, result.skipped, result.edges) == (0, [], 0)
        assert list(tree.rglob("*.md")) == []

    def test_all_skipped_tree(self, tmp_path: Path) -> None:
        tree = tmp_path / "fx"
        _write_md(tree, "kb-index.md", doc_id="kb-index")  # converted kb-index.txt
        result = enrich_tree(tree, root_title="fx")
        assert result.generated == 0
        assert result.edges == 0
        assert result.skipped == [
            EnrichSkip(
                "",
                "'kb-index.md' already exists and was not generated by kb-ingest",
            )
        ]

    def test_not_a_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            enrich_tree(tmp_path / "missing")