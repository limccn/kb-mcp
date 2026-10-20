"""Tests for the event-based watcher mode (kb-mcp v0.8.0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kb_mcp_lite.vault import VaultManager
from kb_mcp_lite.watcher import (
    VaultWatcher,
    _detect_docker,
    resolve_mode,
)


# ---------------------------------------------------------------------------
# Mode resolution and Docker detection
# ---------------------------------------------------------------------------


def test_resolve_mode_explicit_poll() -> None:
    assert resolve_mode("poll") == "poll"


def test_resolve_mode_explicit_event() -> None:
    assert resolve_mode("event") == "event"


def test_resolve_mode_auto_falls_back_when_watchfiles_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kb_mcp_lite.watcher._WATCHFILES_AVAILABLE", False)
    assert resolve_mode("auto") == "poll"


def test_resolve_mode_auto_falls_back_in_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kb_mcp_lite.watcher._WATCHFILES_AVAILABLE", True)
    monkeypatch.setattr("kb_mcp_lite.watcher._detect_docker", lambda: True)
    assert resolve_mode("auto") == "poll"


def test_resolve_mode_auto_picks_event_when_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kb_mcp_lite.watcher._WATCHFILES_AVAILABLE", True)
    monkeypatch.setattr("kb_mcp_lite.watcher._detect_docker", lambda: False)
    assert resolve_mode("auto") == "event"


def test_detect_docker_returns_bool() -> None:
    """Smoke test: the detection function does not raise; it returns a bool."""
    result = _detect_docker()
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _handle_change_set: pure function tests (no watchfiles required)
# ---------------------------------------------------------------------------


@pytest.fixture
def watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VaultWatcher:
    """A VaultWatcher with a real on-disk vault directory."""
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path))
    vm = VaultManager(tmp_path)
    md_dir = tmp_path / "default" / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    return VaultWatcher(vault_manager=vm)


def _md_text(title: str, body: str = "Body.") -> str:
    return f"---\ntype: project\ntitle: {title}\ntags: [watch]\n---\n# {title}\n{body}\n"


class _FakeChange:
    """Stand-in for watchfiles.Change enum used in tests."""

    added = "added"
    modified = "modified"
    deleted = "deleted"


# Patch Change in the watcher's module so the tests don't require watchfiles.
@pytest.fixture(autouse=True)
def _patch_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import kb_mcp_lite.watcher as wmod

    monkeypatch.setattr(wmod, "Change", _FakeChange, raising=False)


def test_handle_change_set_added(watcher: VaultWatcher, tmp_path: Path) -> None:
    from kb_mcp_lite.store.sqlite import SqliteStore

    md = tmp_path / "default" / "md"
    target = md / "proj" / "alpha.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_md_text("Alpha"), encoding="utf-8")

    store = SqliteStore(watcher.db_path)
    try:
        res = watcher._handle_change_set({(_FakeChange.added, str(target))}, store)
        assert res["created"] == ["proj/alpha.md"]
        assert res["updated"] == []
        assert res["deleted"] == []
        assert res["errors"] == []
        # Verify the document made it into the store
        doc = store.get("proj/alpha")
        assert doc.title == "Alpha"
    finally:
        store.close()


def test_handle_change_set_modified(watcher: VaultWatcher, tmp_path: Path) -> None:
    from kb_mcp_lite.store.sqlite import SqliteStore

    md = tmp_path / "default" / "md"
    target = md / "proj" / "beta.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_md_text("Beta v1"), encoding="utf-8")

    store = SqliteStore(watcher.db_path)
    try:
        # Seed the index
        watcher._handle_change_set({(_FakeChange.added, str(target))}, store)

        # Now modify
        target.write_text(_md_text("Beta v2", body="Updated."), encoding="utf-8")
        res = watcher._handle_change_set({(_FakeChange.modified, str(target))}, store)
        assert res["updated"] == ["proj/beta.md"]
        assert res["created"] == []
        assert res["errors"] == []
        doc = store.get("proj/beta")
        assert doc.title == "Beta v2"
    finally:
        store.close()


def test_handle_change_set_deleted(watcher: VaultWatcher, tmp_path: Path) -> None:
    from kb_mcp_lite.store.sqlite import SqliteStore

    md = tmp_path / "default" / "md"
    target = md / "proj" / "gamma.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_md_text("Gamma"), encoding="utf-8")

    store = SqliteStore(watcher.db_path)
    try:
        watcher._handle_change_set({(_FakeChange.added, str(target))}, store)
        assert store.get("proj/gamma").title == "Gamma"

        target.unlink()
        res = watcher._handle_change_set({(_FakeChange.deleted, str(target))}, store)
        assert res["deleted"] == ["proj/gamma.md"]
        assert res["errors"] == []
        # Soft delete: doc is still fetchable with include_deleted
        from kb_mcp_lite.schema import NotFoundError

        with pytest.raises(NotFoundError):
            store.get("proj/gamma")
    finally:
        store.close()


def test_handle_change_set_atomic_save_pattern(watcher: VaultWatcher, tmp_path: Path) -> None:
    """A (deleted, added) pair within one batch should be treated as update."""
    from kb_mcp_lite.store.sqlite import SqliteStore

    md = tmp_path / "default" / "md"
    target = md / "proj" / "delta.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_md_text("Delta v1"), encoding="utf-8")

    store = SqliteStore(watcher.db_path)
    try:
        watcher._handle_change_set({(_FakeChange.added, str(target))}, store)
        # Simulate an editor "save = delete + recreate" burst
        target.write_text(_md_text("Delta v2", body="atomic save"), encoding="utf-8")
        res = watcher._handle_change_set(
            {
                (_FakeChange.deleted, str(target)),
                (_FakeChange.added, str(target)),
            },
            store,
        )
        # Collapsed into a single update, not a delete+create.
        assert res["updated"] == ["proj/delta.md"]
        assert res["created"] == []
        assert res["deleted"] == []
        doc = store.get("proj/delta")
        assert doc.title == "Delta v2"
        assert "atomic save" in doc.body
    finally:
        store.close()


def test_handle_change_set_ignores_paths_outside_watch_dir(
    watcher: VaultWatcher, tmp_path: Path
) -> None:
    from kb_mcp_lite.store.sqlite import SqliteStore

    outside = tmp_path / "other.md"
    outside.write_text(_md_text("Other"), encoding="utf-8")

    store = SqliteStore(watcher.db_path)
    try:
        res = watcher._handle_change_set({(_FakeChange.added, str(outside))}, store)
        assert res["created"] == []
        assert res["updated"] == []
        assert res["deleted"] == []
        assert res["errors"] == []
    finally:
        store.close()


def test_handle_change_set_deleted_for_unknown_doc_is_noop(
    watcher: VaultWatcher, tmp_path: Path
) -> None:
    """A delete event for a file we never indexed should not error."""
    from kb_mcp_lite.store.sqlite import SqliteStore

    md = tmp_path / "default" / "md"
    target = md / "proj" / "never_existed.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Note: file doesn't exist on disk, but the path is within watch_dir.
    # The handler should still try to compute the doc id and call store.delete,
    # which will raise NotFoundError — but the handler swallows that.
    store = SqliteStore(watcher.db_path)
    try:
        res = watcher._handle_change_set({(_FakeChange.deleted, str(target))}, store)
        assert res["errors"] == []
    finally:
        store.close()


def test_handle_change_set_collects_errors(
    watcher: VaultWatcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When _sync_file reports an error, it appears in the errors list."""
    from kb_mcp_lite.store.sqlite import SqliteStore

    md = tmp_path / "default" / "md"
    target = md / "proj" / "bad.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not valid frontmatter at all\n", encoding="utf-8")

    store = SqliteStore(watcher.db_path)
    try:
        res = watcher._handle_change_set({(_FakeChange.added, str(target))}, store)
        # The bad file should be reported as an error, not a success.
        assert res["created"] == []
        assert res["errors"], "expected at least one error"
        assert any("bad.md" in e for e in res["errors"])
    finally:
        store.close()


# ---------------------------------------------------------------------------
# run_event_based fallback when watchfiles is missing
# ---------------------------------------------------------------------------


def test_run_event_based_falls_back_when_watchfiles_missing(
    watcher: VaultWatcher,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If watchfiles is not installed, run_event_based logs a warning and exits."""
    import logging

    import kb_mcp_lite.watcher as wmod

    monkeypatch.setattr(wmod, "_WATCHFILES_AVAILABLE", False)

    # Seed one file so scan_once has something to do.
    md = tmp_path / "default" / "md"
    target = md / "proj" / "seeded.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_md_text("Seeded"), encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="kb_mcp_lite.watcher")
    watcher.run_event_based()

    assert any("watchfiles is not installed" in rec.message for rec in caplog.records), (
        "expected a warning about watchfiles being missing"
    )


# ---------------------------------------------------------------------------
# run() routes to event-based when mode="event"
# ---------------------------------------------------------------------------


def test_run_routes_to_event_based_when_watchfiles_missing(
    watcher: VaultWatcher,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """mode='event' should still work (via fallback) when watchfiles is absent."""
    import logging

    import kb_mcp_lite.watcher as wmod

    monkeypatch.setattr(wmod, "_WATCHFILES_AVAILABLE", False)

    md = tmp_path / "default" / "md"
    target = md / "proj" / "alpha.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_md_text("Alpha"), encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="kb_mcp_lite.watcher")
    watcher.run(mode="event")

    # The fallback should still have indexed the file via scan_once.
    from kb_mcp_lite.store.sqlite import SqliteStore

    store = SqliteStore(watcher.db_path)
    try:
        doc = store.get("proj/alpha")
        assert doc.title == "Alpha"
    finally:
        store.close()


def test_handle_change_set_atomic_save_restores_soft_deleted_doc(
    watcher: VaultWatcher,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3 regression: editor atomic-save on a previously soft-deleted
    document must restore the row, not collide with ``store.update``
    on a ``deleted_at IS NOT NULL`` row.

    Pre-fix: ``{deleted, added}`` collapsed to ``{modified}`` and
    ``_sync_file`` called ``store.update`` on a soft-deleted row,
    which raises ``NotFoundError`` (the store's ``get`` skips
    deleted rows). The exception was silently swallowed by the
    outer ``try/except``, the event still reported ``updated``, and
    the mtime was advanced — so the doc stayed soft-deleted forever.
    """
    from kb_mcp_lite.store.sqlite import SqliteStore

    md = tmp_path / "default" / "md"
    target = md / "proj" / "alpha.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_md_text("Alpha v1"), encoding="utf-8")

    store = SqliteStore(watcher.db_path)
    try:
        # Initial add.
        watcher._handle_change_set({(_FakeChange.added, str(target))}, store)
        # Soft-delete the doc through the store API.
        store.delete("proj/alpha")
        # Editor atomic save: delete + add in one batch.
        target.write_text(_md_text("Alpha v2", body="restored body"), encoding="utf-8")
        res = watcher._handle_change_set(
            {
                (_FakeChange.deleted, str(target)),
                (_FakeChange.added, str(target)),
            },
            store,
        )
        # The doc must surface as updated, not stay soft-deleted.
        assert "proj/alpha.md" in res["updated"], res
        doc = store.get("proj/alpha")
        assert doc.title == "Alpha v2"
        assert "restored body" in doc.body
    finally:
        store.close()
