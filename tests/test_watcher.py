import time
from pathlib import Path
import pytest
from kb_mcp_lite.vault import VaultManager
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.watcher import VaultWatcher


def test_watcher_create_modify_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    vm = VaultManager(tmp_path)

    # Initialize vault
    md_dir = tmp_path / "default" / "md"
    md_dir.mkdir(parents=True, exist_ok=True)

    events = []

    def on_change(event_type, path):
        events.append((event_type, path))

    watcher = VaultWatcher(vault_manager=vm, on_change=on_change)

    # 1. Create file
    doc_file = md_dir / "proj" / "test.md"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    doc_file.write_text(
        "---\ntype: project\ntitle: Watcher Test\ntags: [watch]\n---\n# Initial Body\nSome text.\n",
        encoding="utf-8",
    )

    res = watcher.scan_once()
    assert len(res["created"]) == 1
    assert "proj/test.md" in res["created"]

    # Verify in DB
    store = SqliteStore(vm.resolve_path())
    doc = store.get("proj/test")
    assert doc.title == "Watcher Test"
    assert "Some text." in doc.body

    # 2. Modify file
    time.sleep(0.05)
    doc_file.write_text(
        "---\ntype: project\ntitle: Watcher Test Updated\ntags: [watch, updated]\n---\n# Initial Body\nModified text.\n",
        encoding="utf-8",
    )

    res = watcher.scan_once(store=store)
    assert len(res["updated"]) == 1
    doc_updated = store.get("proj/test")
    assert doc_updated.title == "Watcher Test Updated"
    assert "Modified text." in doc_updated.body

    # 3. Delete file
    doc_file.unlink()
    res = watcher.scan_once(store=store)
    assert len(res["deleted"]) == 1

    # Verify soft-deleted
    with pytest.raises(Exception):
        store.get("proj/test")

    store.close()


def test_scan_once_retries_after_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4 regression: ``_file_mtimes`` must not be advanced on a
    failed ``_sync_file``. A file that errored on the first scan
    must be re-tried on the next scan (and succeed once the cause
    is fixed).

    Pre-fix: ``_file_mtimes[path] = mtime`` was set BEFORE calling
    ``_sync_file``. A bad frontmatter raised an exception that the
    outer ``try/except`` swallowed, but the mtime was already
    cached — the next scan saw no change (``mtime <= prev_mtime``)
    and skipped the file forever.
    """
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    vm = VaultManager(tmp_path)
    md = tmp_path / "default" / "md"
    md.mkdir(parents=True, exist_ok=True)
    target = md / "bad.md"
    # Invalid frontmatter: ``title:`` is empty, which the schema
    # rejects. ``_sync_file`` will return an error string.
    target.write_text("---\ntype: project\ntitle:\n---\n\nbody\n", encoding="utf-8")
    watcher = VaultWatcher(vault_manager=vm)
    res1 = watcher.scan_once()
    assert res1["errors"], f"expected a sync error, got {res1}"
    # The mtime cache must NOT have been populated for the failed
    # file — otherwise the next scan would skip it.
    assert target.resolve() not in watcher._file_mtimes
    # Now fix the file. The next scan must surface the new doc.
    target.write_text("---\ntype: project\ntitle: Fixed\n---\n\nbody\n", encoding="utf-8")
    res2 = watcher.scan_once()
    assert res2["created"] == ["bad.md"], res2
    assert res2["errors"] == [], res2
    assert target.resolve() in watcher._file_mtimes
