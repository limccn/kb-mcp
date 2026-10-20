from __future__ import annotations

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from kb_mcp_lite.admin import create_app
from kb_mcp_lite.schema import Document
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import VaultManager


def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "kb.db")
    store.add(
        Document(
            id="proj/sample",
            type="project",
            title="Sample Project",
            body="SQLite FTS and MCP integration",
            tags=["sqlite", "mcp"],
        )
    )
    return store


def test_git_page_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path / "kb_home"))
    store = make_store(tmp_path)
    client = TestClient(create_app(store=store))

    response = client.get("/git")
    assert response.status_code == 200
    assert "Git" in response.text
    assert "提交历史" in response.text
    assert "拉取更新" in response.text


def test_api_git_status_uninitialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_HOME", str(tmp_path / "kb_home"))
    vm = VaultManager(tmp_path / "kb_home")
    vm.create("test_vault")
    vm.switch("test_vault")

    store = SqliteStore(vm.resolve_path("test_vault"))
    client = TestClient(create_app(store=store))

    response = client.get("/api/git/status")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["is_git"] is False
    assert data["vault_name"] == "test_vault"
    assert "pending_export" in data


def test_api_git_init_commit_and_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kb_home = tmp_path / "kb_home"
    monkeypatch.setenv("KB_MCP_HOME", str(kb_home))
    vm = VaultManager(kb_home)
    vm.create("test_git_vault")
    vm.switch("test_git_vault")

    store = SqliteStore(vm.resolve_path("test_git_vault"))
    store.add(
        Document(
            id="proj/demo",
            type="project",
            title="Demo Project",
            body="Initial demo body",
            tags=["demo"],
        )
    )

    client = TestClient(create_app(store=store))

    # 1. Initialize Git
    init_res = client.post("/api/git/init", json={})
    assert init_res.status_code == 200
    assert init_res.json()["ok"] is True

    # 2. Check Status
    status_res = client.get("/api/git/status")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["ok"] is True
    assert status_data["is_git"] is True
    assert status_data["pending_export"]["total"] >= 1

    # Configure git user in vault git dir for commits in test env
    import subprocess
    git_dir = Path(status_data["git_dir"])
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(git_dir), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(git_dir), check=True)

    # 3. Commit
    commit_res = client.post("/api/git/commit", json={"message": "feat: initial test commit", "full": True})
    assert commit_res.status_code == 200
    commit_data = commit_res.json()
    assert commit_data["ok"] is True

    # 4. Check Status After Commit
    status_res2 = client.get("/api/git/status")
    assert status_res2.status_code == 200
    status_data2 = status_res2.json()
    assert status_data2["pending_export"]["total"] == 0

    # 5. Check History
    hist_res = client.get("/api/git/history")
    assert hist_res.status_code == 200
    hist_data = hist_res.json()
    assert hist_data["ok"] is True
    assert hist_data["count"] >= 1
    assert hist_data["commits"][0]["message"] == "feat: initial test commit"
    assert hist_data["commits"][0]["author"] == "Test User"

    # 6. Test Pull & Push endpoints (expect error response or output without remote)
    pull_res = client.post("/api/git/pull", json={"remote": "origin", "branch": "main"})
    assert pull_res.status_code in (200, 500)

    push_res = client.post("/api/git/push", json={"remote": "origin", "branch": "main"})
    assert push_res.status_code in (200, 500)

    # 7. Test Sync endpoint
    sync_res = client.post("/api/git/sync", json={"remote": "origin", "branch": "main"})
    assert sync_res.status_code in (200, 500)

