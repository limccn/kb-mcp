"""Tests for kb_mcp_lite.md_io (Wave 1B).

Every test uses the ``tmp_path`` fixture — no mocks, no fakes. We
exercise the public API end-to-end against a real :class:`SqliteStore`
(tmp DB file).

The architecture spec (docs/architecture.md § 4.3) requires:

- ``parse_frontmatter(text) -> (Frontmatter, str)``
- ``render_document(doc) -> str``
- ``import_dir(store, dir, *, dry_run=False) -> ImportReport``
- ``export_dir(store, dir, *, force=False) -> int``

Plus NFR-S-3 (path-traversal guard) for both ``import_dir`` and
``export_dir``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from collections.abc import Iterator

import pytest

from kb_mcp_lite.md_io import (
    Frontmatter,
    doc_from_frontmatter,
    export_dir,
    import_dir,
    parse_frontmatter,
    pending_export,
    render_document,
)
from pydantic import ValidationError as PydanticValidationError

from kb_mcp_lite.schema import (
    Document,
    ImportReport,
    ValidationError,
    make_id,
)
from kb_mcp_lite.store.sqlite import SqliteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    """A fresh SqliteStore backed by a tmp_path DB file."""
    s = SqliteStore(tmp_path / "kb.db")
    try:
        yield s
    finally:
        s.close()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parse_frontmatter — frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Architecture § 4.3: returns (Frontmatter, str). Unknown keys preserved."""

    def test_empty_frontmatter_body_only(self) -> None:
        """A body with no YAML block yields an empty dict + the body.

        Note: ``python-frontmatter`` strips a single trailing newline
        from the body, which matches the behaviour of ``str.strip()``
        applied once.
        """
        fm, body = parse_frontmatter("Just a body, no frontmatter.\n")
        assert fm == {}
        assert body == "Just a body, no frontmatter."

    def test_single_key(self) -> None:
        """A one-key YAML block parses correctly."""
        text = "---\ntitle: Hello\n---\nThe body."
        fm, body = parse_frontmatter(text)
        assert fm == {"title": "Hello"}
        assert body == "The body."

    def test_multiline_value(self) -> None:
        """Multiline YAML values (literal block) are preserved."""
        text = (
            "---\n"
            "title: Multiline\n"
            "description: |\n"
            "  Line one.\n"
            "  Line two.\n"
            "  Line three.\n"
            "---\n"
            "Body here.\n"
        )
        fm, body = parse_frontmatter(text)
        assert fm["title"] == "Multiline"
        assert "Line one." in fm["description"]
        assert "Line two." in fm["description"]
        assert "Line three." in fm["description"]
        assert body.strip() == "Body here."

    def test_unknown_keys_preserved(self) -> None:
        """Unknown frontmatter keys survive the parse intact."""
        text = "---\ntitle: Known\ncustom_key: some-value\nanother: 42\ntags: [a, b]\n---\nBody.\n"
        fm, body = parse_frontmatter(text)
        # Known keys are present.
        assert fm["title"] == "Known"
        assert fm["tags"] == ["a", "b"]
        # Unknown keys preserved verbatim.
        assert fm["custom_key"] == "some-value"
        assert fm["another"] == 42
        # Trailing newline stripped by python-frontmatter.
        assert body == "Body."

    def test_returns_typeddict(self) -> None:
        """The return is a plain dict matching the Frontmatter TypedDict."""
        fm, _ = parse_frontmatter("---\ntitle: X\n---\nbody")
        # TypedDict is structural; verify the type and shape.
        assert isinstance(fm, dict)
        # TypedDict is a dict at runtime; assert keys are addressable.
        title: str = fm["title"]
        assert title == "X"

    def test_empty_input(self) -> None:
        """An empty string yields empty frontmatter + empty body."""
        fm, body = parse_frontmatter("")
        assert fm == {}
        assert body == ""

    def test_no_frontmatter_only_body(self) -> None:
        """Plain Markdown with no ``---`` block is returned as body only."""
        text = "# Heading\n\nA paragraph."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text


# ---------------------------------------------------------------------------
# render_document — stable Markdown output
# ---------------------------------------------------------------------------


class TestRenderDocument:
    """render_document produces round-trippable stable Markdown."""

    def test_round_trip_through_parse(self) -> None:
        """A rendered document re-parses to the same logical fields."""
        doc = Document(
            id="proj/kb-mcp",
            type="project",
            title="kb-mcp Project",
            body="# Heading\n\nA body with *markdown*.",
            tags=["python", "mcp"],
            source="kb-mcp.md",
            created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        text = render_document(doc)

        fm, body = parse_frontmatter(text)
        assert fm["type"] == "project"
        assert fm["title"] == "kb-mcp Project"
        assert fm["tags"] == ["python", "mcp"]
        assert fm["source"] == "kb-mcp.md"
        assert fm["created_at"] == "2024-01-01T12:00:00+00:00"
        assert fm["updated_at"] == "2024-06-01T12:00:00+00:00"
        assert body == "# Heading\n\nA body with *markdown*."

    def test_body_verbatim_no_html_conversion(self) -> None:
        """Body is NOT HTML-converted — Markdown stays Markdown."""
        body = (
            "# Heading\n\n"
            "A paragraph with **bold**, *italic*, `code`, and a [link](http://example.com).\n\n"
            "- item one\n- item two\n"
        )
        doc = Document(
            id="lesson/test",
            type="lesson",
            title="Test",
            body=body,
        )
        text = render_document(doc)
        # Body is exactly as supplied — no <h1>, <strong>, <em>, etc.
        assert "# Heading" in text
        assert "**bold**" in text
        assert "*italic*" in text
        assert "`code`" in text
        assert "[link](http://example.com)" in text
        assert "<h1>" not in text
        assert "<strong>" not in text
        assert "<em>" not in text
        assert "<a href" not in text

    def test_aliases_round_trip_through_frontmatter(self) -> None:
        """Aliases are preserved in rendered Markdown frontmatter."""
        doc = Document(
            id="proj/kb-mcp",
            type="project",
            title="kb-mcp",
            body="Body",
            aliases=["kb", "kb-mcp-lite"],
        )
        text = render_document(doc)
        fm, body = parse_frontmatter(text)
        assert fm["aliases"] == ["kb", "kb-mcp-lite"]
        assert body == "Body"

    def test_stable_key_ordering(self) -> None:
        """Rendering is deterministic — same input, same output."""
        doc = Document(
            id="faq/x",
            type="faq",
            title="Q",
            body="A",
            tags=["t"],
            source="q.md",
        )
        first = render_document(doc)
        second = render_document(doc)
        assert first == second

    def test_minimal_document_no_optional_fields(self) -> None:
        """A document with only required fields renders cleanly."""
        doc = Document(id="proj/min", type="project", title="Min", body="B")
        text = render_document(doc)
        assert text.startswith("---\n")
        assert "type: project" in text
        assert "title: Min" in text
        # source absent when None.
        assert "source:" not in text

    def test_body_empty_string_still_renders(self) -> None:
        """An empty body still produces a valid frontmatter block."""
        doc = Document(id="faq/empty", type="faq", title="Empty", body="")
        text = render_document(doc)
        fm, body = parse_frontmatter(text)
        assert fm["title"] == "Empty"
        assert body == ""


# ---------------------------------------------------------------------------
# doc_from_frontmatter — Document construction
# ---------------------------------------------------------------------------


class TestDocFromFrontmatter:
    """doc_from_frontmatter builds a Document from parsed frontmatter."""

    def test_basic(self) -> None:
        """Required fields produce a Document with a generated id."""
        fm: Frontmatter = {"type": "project", "title": "Hello World"}
        doc = doc_from_frontmatter(fm, "body text")
        assert doc.id == make_id("project", "Hello World")
        assert doc.type == "project"
        assert doc.title == "Hello World"
        assert doc.body == "body text"

    def test_missing_type_raises(self) -> None:
        """Missing 'type' raises ValidationError."""
        with pytest.raises(ValidationError, match="type"):
            doc_from_frontmatter({"title": "X"}, "body")

    def test_missing_title_raises(self) -> None:
        """Missing 'title' raises ValidationError."""
        with pytest.raises(ValidationError, match="title"):
            doc_from_frontmatter({"type": "project"}, "body")

    def test_tags_default_empty(self) -> None:
        """No tags in frontmatter means empty list."""
        doc = doc_from_frontmatter({"type": "project", "title": "X"}, "")
        assert doc.tags == []

    def test_tags_preserved(self) -> None:
        """Tags list is forwarded."""
        doc = doc_from_frontmatter({"type": "project", "title": "X", "tags": ["a", "b"]}, "")
        assert doc.tags == ["a", "b"]

    def test_source_argument_overrides_frontmatter(self) -> None:
        """The explicit ``source`` argument overrides any frontmatter source."""
        fm: Frontmatter = {
            "type": "project",
            "title": "X",
            "source": "from/frontmatter.md",
        }
        doc = doc_from_frontmatter(fm, "", source="from/caller.md")
        assert doc.source == "from/caller.md"


# ---------------------------------------------------------------------------
# import_dir — vault ingestion
# ---------------------------------------------------------------------------


class TestImportDir:
    """import_dir walks a directory, parses .md, inserts/updates by source."""

    def test_round_trip_import_export_import(self, tmp_path: Path, store: SqliteStore) -> None:
        """import_dir → export_dir → import_dir must be a no-op (no diff)."""
        vault = tmp_path / "src"
        vault.mkdir()

        _write(
            vault / "alpha.md",
            "---\ntype: project\ntitle: Alpha\n---\n# Alpha\n\nBody.\n",
        )
        _write(
            vault / "beta.md",
            "---\ntype: decision\ntitle: Use SQLite\ntags: [storage]\n---\n"
            "## Decision\n\nSQLite all the things.\n",
        )
        _write(
            vault / "sub" / "nested.md",
            "---\ntype: lesson\ntitle: Watch Your Paths\n---\nLesson body with *emphasis*.\n",
        )

        # Step 1: import vault.
        r1 = import_dir(store, vault)
        assert r1.inserted == 3
        assert r1.errors == []

        docs_before = sorted(store.export_all(), key=lambda d: d.id)
        assert len(docs_before) == 3
        snapshot_before = [
            (d.id, d.title, d.type, d.body, d.tags, tuple(sorted(d.tags))) for d in docs_before
        ]

        # Step 2: export to a separate directory.
        export_path = tmp_path / "exported"
        written = export_dir(store, export_path)
        assert written == 3
        # All exported files exist.
        written_files = sorted(p.name for p in export_path.glob("*.md"))
        # Each id's last segment must appear (one-to-one here).
        assert len(written_files) == 3

        # Step 3: re-import the exported directory. Must be a no-op
        # (idempotent): 0 inserts, 3 updates.
        r2 = import_dir(store, export_path)
        assert r2.inserted == 0, f"expected no new inserts, got {r2}"
        assert r2.updated == 3, f"expected 3 updates, got {r2}"

        docs_after = sorted(store.export_all(), key=lambda d: d.id)
        snapshot_after = [
            (d.id, d.title, d.type, d.body, d.tags, tuple(sorted(d.tags))) for d in docs_after
        ]

        # Same logical content (id, title, type, body, tags).
        assert snapshot_before == snapshot_after, (
            f"round-trip changed data:\n  before: {snapshot_before}\n  after:  {snapshot_after}"
        )

    def test_skip_hidden_files(self, tmp_path: Path, store: SqliteStore) -> None:
        """Files starting with ``.`` are skipped."""
        vault = tmp_path / "v"
        vault.mkdir()
        _write(vault / "visible.md", "---\ntype: project\ntitle: V\n---\nBody\n")
        _write(vault / ".hidden.md", "---\ntype: project\ntitle: H\n---\nX\n")
        r = import_dir(store, vault)
        assert r.inserted == 1
        assert store.list()[0].title == "V"

    def test_skip_hidden_directories(self, tmp_path: Path, store: SqliteStore) -> None:
        """Hidden directories are not descended into."""
        vault = tmp_path / "v"
        vault.mkdir()
        _write(vault / "ok.md", "---\ntype: project\ntitle: OK\n---\nA\n")
        hidden = vault / ".secret"
        hidden.mkdir()
        _write(hidden / "leak.md", "---\ntype: project\ntitle: Leak\n---\nX\n")
        r = import_dir(store, vault)
        assert r.inserted == 1
        assert all(d.title == "OK" for d in store.export_all())

    def test_skip_non_md_files(self, tmp_path: Path, store: SqliteStore) -> None:
        """Files without ``.md`` extension are skipped."""
        vault = tmp_path / "v"
        vault.mkdir()
        _write(vault / "real.md", "---\ntype: project\ntitle: Real\n---\nB\n")
        _write(vault / "note.txt", "text file, should be ignored")
        _write(vault / "noext", "no extension, also ignored")
        r = import_dir(store, vault)
        assert r.inserted == 1

    def test_idempotent_reimport_updates_by_source(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """Re-importing a directory updates existing docs (no duplicates)."""
        vault = tmp_path / "v"
        vault.mkdir()
        path = _write(
            vault / "x.md",
            "---\ntype: project\ntitle: X\n---\nv1\n",
        )
        r1 = import_dir(store, vault)
        assert r1.inserted == 1
        assert len(store.export_all()) == 1

        # Modify file content and re-import.
        path.write_text(
            "---\ntype: project\ntitle: X\n---\nv2 updated\n",
            encoding="utf-8",
        )
        r2 = import_dir(store, vault)
        assert r2.inserted == 0
        assert r2.updated == 1
        assert len(store.export_all()) == 1
        assert store.export_all()[0].body == "v2 updated"

    def test_path_traversal_blocked_via_symlink(self, tmp_path: Path, store: SqliteStore) -> None:
        """A symlink escaping the import dir is rejected (NFR-S-3)."""
        # Create an outside file (its contents must NOT appear in the store).
        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\ntype: project\ntitle: Outside\n---\nOUT\n",
            encoding="utf-8",
        )

        # Vault dir; inside it, a symlink to the outside file.
        vault = tmp_path / "vault"
        vault.mkdir()
        link = vault / "leak.md"
        link.symlink_to(outside)

        r = import_dir(store, vault)
        # The traversal must show up in errors.
        assert any("leak.md" in e and "traversal" in e for e in r.errors), (
            f"expected path-traversal error for leak.md, got errors={r.errors}"
        )
        # Nothing must have been imported.
        assert store.export_all() == []

    def test_dry_run_does_not_write(self, tmp_path: Path, store: SqliteStore) -> None:
        """dry_run=True parses but does not touch the store."""
        vault = tmp_path / "v"
        vault.mkdir()
        _write(
            vault / "x.md",
            "---\ntype: project\ntitle: X\n---\nBody\n",
        )
        r = import_dir(store, vault, dry_run=True)
        assert r.inserted == 0
        assert r.updated == 0
        assert store.export_all() == []

    def test_missing_required_frontmatter_reports_error(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """A file missing required 'type' or 'title' is reported, not inserted."""
        vault = tmp_path / "v"
        vault.mkdir()
        _write(vault / "no-type.md", "---\ntitle: X\n---\nB\n")
        _write(vault / "no-title.md", "---\ntype: project\n---\nB\n")
        _write(vault / "good.md", "---\ntype: project\ntitle: G\n---\nB\n")
        r = import_dir(store, vault)
        # Only the good doc is inserted; the others are errors.
        assert r.inserted == 1
        assert len(r.errors) == 2
        assert any("no-type.md" in e for e in r.errors)
        assert any("no-title.md" in e for e in r.errors)
        assert len(store.export_all()) == 1

    def test_empty_directory(self, tmp_path: Path, store: SqliteStore) -> None:
        """An empty directory imports nothing and reports nothing."""
        vault = tmp_path / "v"
        vault.mkdir()
        r = import_dir(store, vault)
        assert r.inserted == 0
        assert r.errors == []

    def test_returns_import_report_instance(self, tmp_path: Path, store: SqliteStore) -> None:
        """The return value is an ImportReport (not a tuple, dict, etc)."""
        vault = tmp_path / "v"
        vault.mkdir()
        r = import_dir(store, vault)
        assert isinstance(r, ImportReport)

    def test_round_trip_preserves_links(self, tmp_path: Path, store: SqliteStore) -> None:
        """Export → re-import preserves cross-document links.

        Regression: when filename-as-ID changed the import ID strategy,
        links in frontmatter referenced stale make_id targets and were
        silently dropped on re-import.
        """
        vault = tmp_path / "src"
        vault.mkdir()

        _write(
            vault / "alpha.md",
            "---\ntype: project\ntitle: Alpha\n---\n# Alpha\n\nBody.\n",
        )
        _write(
            vault / "beta.md",
            "---\ntype: decision\ntitle: Beta\n---\n# Beta\n\nBody.\n",
        )

        # First import → both docs get filename-based IDs.
        r1 = import_dir(store, vault)
        assert r1.inserted == 2
        assert r1.errors == []

        # Create a link between them.
        store.link("alpha", "beta", rel="references")

        # Export to a separate directory.
        export_path = tmp_path / "exported"
        export_dir(store, export_path, force=True)

        # The exported file should have links in frontmatter.
        exported = (export_path / "alpha.md").read_text(encoding="utf-8")
        assert "links:" in exported
        assert "beta" in exported

        # Re-import from the export directory — must preserve links.
        r2 = import_dir(store, export_path)
        assert r2.errors == [], f"re-import errors: {r2.errors}"

        # Verify the link still exists after re-import.
        outlinks = store.outlinks("alpha")
        assert len(outlinks) == 1
        assert outlinks[0].to_id == "beta"
        assert outlinks[0].rel == "references"

        # Also verify the inbound link is preserved.
        backlinks = store.backlinks("beta")
        assert len(backlinks) == 1
        assert backlinks[0].from_id == "alpha"

    def test_import_resolves_old_style_link_targets(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """Importing files with old-style make_id link targets (e.g.
        "proj/alpha") resolves them to new filename-based IDs.

        Regression: after the import ID strategy changed, links exported
        by an older version referenced targets like "proj/alpha" but the
        document now has ID "alpha". The import must map the old target
        to the new ID.
        """
        vault = tmp_path / "src"
        vault.mkdir()

        # These files simulate an old export: the frontmatter links
        # reference make_id-style targets.
        _write(
            vault / "alpha.md",
            "---\n"
            "type: project\n"
            "title: Alpha\n"
            "links:\n"
            "  - to: dec/beta\n"  # old make_id target
            "    rel: references\n"
            "---\n"
            "# Alpha\n\nBody.\n",
        )
        _write(
            vault / "beta.md",
            "---\ntype: decision\ntitle: Beta\n---\n# Beta\n\nBody.\n",
        )

        r = import_dir(store, vault)
        assert r.errors == [], f"import errors: {r.errors}"

        # The link from alpha should resolve to "beta" (filename-based ID)
        # instead of "dec/beta" (old make_id target).
        outlinks = store.outlinks("alpha")
        assert len(outlinks) == 1
        assert outlinks[0].to_id == "beta"
        assert outlinks[0].rel == "references"

        # Sanity: "dec/beta" does not exist.
        from kb_mcp_lite.schema import NotFoundError

        with pytest.raises(NotFoundError):
            store.get("dec/beta")


# ---------------------------------------------------------------------------
# export_dir — vault emission
# ---------------------------------------------------------------------------


class TestExportDir:
    """export_dir writes one .md per document, named <slug>.md."""

    def test_basic_export(self, tmp_path: Path, store: SqliteStore) -> None:
        """Two docs in the store produce two files on disk."""
        store.add(Document(id="proj/a", type="project", title="Alpha", body="A body"))
        store.add(Document(id="proj/b", type="project", title="Beta", body="B body"))
        out = tmp_path / "out"
        n = export_dir(store, out)
        assert n == 2
        files = sorted(p.name for p in out.glob("*.md"))
        assert files == ["a.md", "b.md"]
        # Contents are round-trippable.
        a_text = (out / "a.md").read_text(encoding="utf-8")
        fm, body = parse_frontmatter(a_text)
        assert fm["title"] == "Alpha"
        assert body == "A body"

    def test_numeric_suffix_on_collision(self, tmp_path: Path, store: SqliteStore) -> None:
        """Two docs with the same slug get -2, -3 suffixes."""
        store.add(Document(id="proj/x", type="project", title="X", body="first"))
        store.add(Document(id="dec/x", type="decision", title="X", body="second"))
        out = tmp_path / "out"
        n = export_dir(store, out)
        assert n == 2
        names = sorted(p.name for p in out.glob("*.md"))
        assert names == ["x-2.md", "x.md"]

    def test_refuses_overwrite_without_force(self, tmp_path: Path, store: SqliteStore) -> None:
        """Pre-existing files are not clobbered unless force=True."""
        store.add(Document(id="proj/x", type="project", title="X", body="x"))
        out = tmp_path / "out"
        out.mkdir()
        (out / "x.md").write_text("UNRELATED", encoding="utf-8")
        with pytest.raises(ValidationError, match="refusing to overwrite"):
            export_dir(store, out)
        # Original file untouched.
        assert (out / "x.md").read_text(encoding="utf-8") == "UNRELATED"

    def test_force_overwrites(self, tmp_path: Path, store: SqliteStore) -> None:
        """force=True overwrites existing files."""
        store.add(Document(id="proj/x", type="project", title="X", body="X body"))
        out = tmp_path / "out"
        out.mkdir()
        (out / "x.md").write_text("OLD", encoding="utf-8")
        n = export_dir(store, out, force=True)
        assert n == 1
        assert "X body" in (out / "x.md").read_text(encoding="utf-8")

    def test_creates_destination(self, tmp_path: Path, store: SqliteStore) -> None:
        """Missing destination directory is created (with parents)."""
        store.add(Document(id="proj/x", type="project", title="X", body="X"))
        out = tmp_path / "deep" / "nested" / "out"
        assert not out.exists()
        n = export_dir(store, out)
        assert n == 1
        assert out.is_dir()
        assert (out / "x.md").exists()

    def test_deletes_soft_deleted_documents(self, tmp_path: Path, store: SqliteStore) -> None:
        """A soft-deleted doc has its corresponding file deleted on disk."""
        store.add(Document(id="proj/x", type="project", title="X", body="X body"))
        out = tmp_path / "out"
        n = export_dir(store, out)
        assert n == 1
        x_file = out / "x.md"
        assert x_file.exists()

        # Now soft-delete it
        store.delete("proj/x")

        # Export again (must delete x.md)
        n = export_dir(store, out, force=True)
        assert n == 0
        assert not x_file.exists()

    def test_path_traversal_via_source_rejected(self, tmp_path: Path, store: SqliteStore) -> None:
        """NFR-S-3: a doc.source that escapes the export dir is rejected."""
        store.add(
            Document(
                id="proj/evil",
                type="project",
                title="Evil",
                body="b",
                source="../../etc/passwd",
            )
        )
        out = tmp_path / "out"
        with pytest.raises(ValidationError, match="source"):
            export_dir(store, out)

    def test_absolute_source_rejected(self, tmp_path: Path, store: SqliteStore) -> None:
        """Absolute doc.source paths are rejected outright (model-level)."""
        with pytest.raises(PydanticValidationError, match="source must be a relative path"):
            Document(
                id="proj/abs",
                type="project",
                title="Abs",
                body="b",
                source="/etc/passwd",
            )

    def test_absolute_source_rejected_export(self, tmp_path: Path, store: SqliteStore) -> None:
        """Defense-in-depth: export_dir rejects absolute source even if data bypassed model validation."""
        # Insert directly into SQLite to bypass the model-level validator.
        now = datetime.now(timezone.utc).isoformat()
        store._conn.execute(
            "INSERT INTO documents (id, type, title, body, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj/abs", "project", "Abs", "b", "/etc/passwd", now, now),
        )
        store._conn.commit()
        out = tmp_path / "out"
        with pytest.raises(ValidationError, match="absolute"):
            export_dir(store, out)

    def test_empty_store_writes_nothing(self, tmp_path: Path, store: SqliteStore) -> None:
        """An empty store writes zero files."""
        out = tmp_path / "out"
        n = export_dir(store, out)
        assert n == 0
        assert list(out.glob("*.md")) == []

    def test_body_verbatim(self, tmp_path: Path, store: SqliteStore) -> None:
        """Exported body is the original Markdown, not HTML."""
        md_body = "# H\n\n- a\n- b\n\n```\ncode\n```\n"
        store.add(Document(id="lesson/x", type="lesson", title="X", body=md_body))
        out = tmp_path / "out"
        export_dir(store, out)
        text = (out / "x.md").read_text(encoding="utf-8")
        # Markdown stays Markdown; no HTML tags anywhere.
        assert "# H" in text
        assert "```\ncode\n```" in text
        assert "<h1>" not in text
        assert "<ul>" not in text

    def test_incremental_skips_unchanged_docs(self, tmp_path: Path, store: SqliteStore) -> None:
        """A second incremental export right after a full one writes nothing.

        Regression: the source write-back must not bump ``updated_at``,
        otherwise every doc is newer than its file and incremental export
        degenerates into a full re-export.
        """
        store.add(Document(id="proj/a", type="project", title="Alpha", body="A body"))
        store.add(Document(id="proj/b", type="project", title="Beta", body="B body"))
        out = tmp_path / "out"
        assert export_dir(store, out, force=True, incremental=True) == 2
        assert export_dir(store, out, force=True, incremental=True) == 0

    def test_incremental_exports_only_changed_docs(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """After editing one doc, incremental export rewrites only that file."""
        store.add(Document(id="proj/a", type="project", title="Alpha", body="A body"))
        store.add(Document(id="proj/b", type="project", title="Beta", body="B body"))
        out = tmp_path / "out"
        assert export_dir(store, out, force=True, incremental=True) == 2

        store.update("proj/a", body="A body v2")

        assert export_dir(store, out, force=True, incremental=True) == 1
        assert "A body v2" in (out / "a.md").read_text(encoding="utf-8")
        # And the next run is clean again.
        assert export_dir(store, out, force=True, incremental=True) == 0

    def test_source_write_back_does_not_bump_updated_at(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """export_dir records source without changing ``updated_at``."""
        store.add(Document(id="proj/a", type="project", title="Alpha", body="A body"))
        before = store.get("proj/a").updated_at
        out = tmp_path / "out"
        export_dir(store, out, force=True, incremental=True)
        after = store.get("proj/a")
        assert after.source == "a.md"
        assert after.updated_at == before

    def test_incremental_collision_writes_own_file(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """With two docs sharing a slug, an incremental re-export of one
        must rewrite its own ``<slug>-2.md`` — never the sibling's file."""
        store.add(Document(id="reference/x", type="glossary", title="X Ref", body="ref v1"))
        store.add(Document(id="tech/x", type="lesson", title="X Tech", body="tech v1"))
        out = tmp_path / "out"
        export_dir(store, out, force=True, incremental=True)
        assert sorted(p.name for p in out.glob("*.md")) == ["x-2.md", "x.md"]

        store.update("tech/x", body="tech v2")
        n = export_dir(store, out, force=True, incremental=True)

        assert n == 1
        assert "tech v2" in (out / "x-2.md").read_text(encoding="utf-8")
        assert "ref v1" in (out / "x.md").read_text(encoding="utf-8")
        # Source assignments stay put — no flip-flop between siblings.
        assert store.get("tech/x").source == "x-2.md"
        assert store.get("reference/x").source == "x.md"

    def test_export_keeps_file_shared_with_live_sibling(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """A soft-deleted doc whose source file is claimed by a live
        sibling (same slug) must not cause the file to be removed."""
        store.add(Document(id="tech/x", type="lesson", title="X Tech", body="tech body"))
        out = tmp_path / "out"
        export_dir(store, out)
        # Recreate the wild state: deleted doc sharing the live doc's file.
        store.add(Document(id="reference/x", type="glossary", title="X Ref", body="ref body"))
        store.update_source("reference/x", "x.md")
        store.delete("reference/x")

        export_dir(store, out, force=True, incremental=True)

        assert (out / "x.md").is_file()
        assert "tech body" in (out / "x.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# pending_export — store vs export dir diff
# ---------------------------------------------------------------------------


class TestPendingExport:
    """pending_export reports what an export would change, content-based."""

    def test_clean_after_export_including_slug_collisions(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """No pending changes right after an export — even for docs that
        share a slug (regression: the sibling's file was compared before,
        producing a permanent 'modified' ghost)."""
        store.add(Document(id="reference/x", type="glossary", title="X Ref", body="ref"))
        store.add(Document(id="tech/x", type="lesson", title="X Tech", body="tech"))
        store.add(Document(id="proj/y", type="project", title="Y", body="y"))
        out = tmp_path / "out"
        export_dir(store, out, force=True, incremental=True)

        pending = pending_export(store, out)

        assert pending.total == 0, pending

    def test_detects_added_modified_deleted(self, tmp_path: Path, store: SqliteStore) -> None:
        store.add(Document(id="proj/a", type="project", title="A", body="a v1"))
        store.add(Document(id="proj/c", type="project", title="C", body="c"))
        out = tmp_path / "out"
        export_dir(store, out, force=True, incremental=True)

        store.update("proj/a", body="a v2")
        store.add(Document(id="proj/b", type="project", title="B", body="b"))
        store.delete("proj/c")

        pending = pending_export(store, out)

        assert pending.added == ["proj/b"]
        assert pending.modified == ["proj/a"]
        assert pending.deleted == ["proj/c"]

    def test_clean_again_after_incremental_export(self, tmp_path: Path, store: SqliteStore) -> None:
        """An incremental export clears exactly the pending entries."""
        store.add(Document(id="proj/a", type="project", title="A", body="a v1"))
        out = tmp_path / "out"
        export_dir(store, out, force=True, incremental=True)
        store.update("proj/a", body="a v2")
        assert pending_export(store, out).modified == ["proj/a"]

        export_dir(store, out, force=True, incremental=True)

        assert pending_export(store, out).total == 0

    def test_deleted_with_live_slug_sibling_not_reported(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """A soft-deleted doc sharing its file with a live sibling is not
        a pending deletion (regression: permanent 'deleted' ghost)."""
        store.add(Document(id="tech/x", type="lesson", title="X Tech", body="tech"))
        out = tmp_path / "out"
        export_dir(store, out)
        store.add(Document(id="reference/x", type="glossary", title="X Ref", body="ref"))
        store.update_source("reference/x", "x.md")
        store.delete("reference/x")

        pending = pending_export(store, out)

        # tech/x is clean; reference/x's file belongs to the live sibling.
        assert pending.total == 0, pending


# ---------------------------------------------------------------------------
# Path-traversal guard (NFR-S-3) — direct test with ../../../etc/passwd
# ---------------------------------------------------------------------------


class TestPathTraversalGuard:
    """NFR-S-3 explicit acceptance: ../../../etc/passwd must be rejected."""

    def test_import_dir_rejects_traversal_outside_base(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """A symlink whose target resolves outside the import dir is rejected.

        The literal path ``../../../etc/passwd`` cannot be created inside
        the import dir (it would have to be a symlink, since the file
        isn't owned by the test). We simulate the worst-case scenario
        with a symlink to a real file in ``tmp_path`` but OUTSIDE the
        vault directory. ``Path.resolve()`` follows the link, the
        resolved path is no longer under the vault, so the guard fires.
        """
        # Real file outside the vault.
        outside_dir = tmp_path / "outside-target"
        outside_dir.mkdir()
        outside_file = outside_dir / "passwd"
        outside_file.write_text(
            "---\ntype: project\ntitle: Should Not Import\n---\nsecret\n",
            encoding="utf-8",
        )

        # Vault inside tmp_path; inside the vault, a symlink that
        # escapes via "..".
        vault = tmp_path / "vault"
        vault.mkdir()
        leaked = vault / "leaked.md"
        # Use a relative symlink that, when resolved, traverses outside.
        # From vault/, ".." → tmp_path/, but we want to escape
        # FURTHER. We build "../outside-target/passwd" which from inside
        # the vault resolves to tmp_path/outside-target/passwd — the
        # outside file.
        leaked.symlink_to("../outside-target/passwd")

        # Sanity: the resolved path of the symlink is outside the vault.
        resolved = leaked.resolve()
        assert not resolved.is_relative_to(vault.resolve())

        r = import_dir(store, vault)
        assert r.inserted == 0
        assert r.errors, "expected at least one error for the traversal"
        # Error message must mention path traversal.
        assert any("traversal" in e for e in r.errors)
        assert store.export_all() == []

    def test_export_dir_rejects_doc_source_traversal(
        self, tmp_path: Path, store: SqliteStore
    ) -> None:
        """export_dir rejects a doc whose source resolves outside ``dir``."""
        # A doc whose source uses '..' to climb out of the export dir.
        store.add(
            Document(
                id="proj/leaked",
                type="project",
                title="Leaked",
                body="body",
                source="../../../etc/passwd",
            )
        )
        out = tmp_path / "out"
        with pytest.raises(ValidationError):
            export_dir(store, out)
        # And nothing was written.
        assert not (out / "leaked.md").exists()
