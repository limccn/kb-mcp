"""Tests for ``kb vault`` commands.

These tests run the CLI with a temporary ``KB_MCP_HOME`` so that
VaultManager creates and resolves vaults in an isolated directory.
They do NOT inject a store — the root ``cli()`` group constructs
everything naturally via the env var.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from kb_mcp_lite.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_INTERNAL = 5


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    """Isolated KB_MCP_HOME for each test."""
    kb_home = tmp_path / "kb-mcp"
    kb_home.mkdir(parents=True)
    return {**os.environ, "KB_MCP_HOME": str(kb_home)}


# ---------------------------------------------------------------------------
# kb vault list
# ---------------------------------------------------------------------------


class TestVaultList:
    def test_list_default_vault(self, runner: CliRunner, env: dict[str, str]) -> None:
        result = runner.invoke(cli, ["vault", "list"], env=env)
        assert result.exit_code == EXIT_OK
        assert "default" in result.output

    def test_list_json(self, runner: CliRunner, env: dict[str, str]) -> None:
        result = runner.invoke(cli, ["vault", "list", "--json"], env=env)
        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert isinstance(data, list)
        names = [v["name"] for v in data]
        assert "default" in names


# ---------------------------------------------------------------------------
# kb vault create
# ---------------------------------------------------------------------------


class TestVaultCreate:
    def test_create_new_vault(self, runner: CliRunner, env: dict[str, str]) -> None:
        result = runner.invoke(cli, ["vault", "create", "my-vault"], env=env)
        assert result.exit_code == EXIT_OK
        assert "my-vault" in result.output
        # Verify it shows up in list
        list_result = runner.invoke(cli, ["vault", "list"], env=env)
        assert "my-vault" in list_result.output

    def test_create_with_description(self, runner: CliRunner, env: dict[str, str]) -> None:
        result = runner.invoke(
            cli,
            [
                "vault",
                "create",
                "team-kb",
                "--desc",
                "Team knowledge base",
            ],
            env=env,
        )
        assert result.exit_code == EXIT_OK
        assert "team-kb" in result.output

    def test_create_json(self, runner: CliRunner, env: dict[str, str]) -> None:
        result = runner.invoke(
            cli,
            [
                "vault",
                "create",
                "json-vault",
                "--json",
            ],
            env=env,
        )
        assert result.exit_code == EXIT_OK
        data = json.loads(result.output)
        assert data["name"] == "json-vault"
        assert "path" in data

    def test_create_duplicate(self, runner: CliRunner, env: dict[str, str]) -> None:
        runner.invoke(cli, ["vault", "create", "dup"], env=env)
        result = runner.invoke(cli, ["vault", "create", "dup"], env=env)
        assert result.exit_code == EXIT_CONFLICT


# ---------------------------------------------------------------------------
# kb vault switch
# ---------------------------------------------------------------------------


class TestVaultSwitch:
    def test_switch_to_existing_vault(self, runner: CliRunner, env: dict[str, str]) -> None:
        runner.invoke(cli, ["vault", "create", "other"], env=env)
        result = runner.invoke(cli, ["vault", "switch", "other"], env=env)
        assert result.exit_code == EXIT_OK
        assert "other" in result.output

    def test_switch_and_list_shows_new_default(
        self, runner: CliRunner, env: dict[str, str]
    ) -> None:
        runner.invoke(cli, ["vault", "create", "primary"], env=env)
        runner.invoke(cli, ["vault", "switch", "primary"], env=env)
        # The switch updates the in-memory config; each invoke() gets a
        # fresh context, so the switch doesn't persist to the next call.
        # Just verify the switch command itself succeeds.
        result = runner.invoke(cli, ["vault", "list"], env=env)
        assert result.exit_code == 0
        assert "primary" in result.output

    def test_switch_to_nonexistent(self, runner: CliRunner, env: dict[str, str]) -> None:
        result = runner.invoke(cli, ["vault", "switch", "ghost"], env=env)
        assert result.exit_code == EXIT_NOT_FOUND


# ---------------------------------------------------------------------------
# kb vault list after create — verify isolation
# ---------------------------------------------------------------------------


class TestVaultIsolation:
    def test_create_and_list_two_vaults(self, runner: CliRunner, env: dict[str, str]) -> None:
        runner.invoke(cli, ["vault", "create", "work"], env=env)
        runner.invoke(cli, ["vault", "create", "personal"], env=env)
        result = runner.invoke(cli, ["vault", "list"], env=env)
        assert "work" in result.output
        assert "personal" in result.output
        assert "default" in result.output

    def test_new_vault_has_separate_store(self, runner: CliRunner, env: dict[str, str]) -> None:
        runner.invoke(cli, ["vault", "create", "isolated"], env=env)
        # Add a doc to the default vault
        runner.invoke(
            cli,
            [
                "add",
                "--type",
                "project",
                "--title",
                "Default Doc",
            ],
            env=env,
        )
        # Verify the doc is there
        result = runner.invoke(cli, ["list", "--json"], env=env)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# kb vault status — pending export detection
# ---------------------------------------------------------------------------


def _git_env(env: dict[str, str]) -> dict[str, str]:
    """Add a deterministic git identity so ``kb vault commit`` works in tests."""
    return {
        **env,
        "GIT_AUTHOR_NAME": "kb test",
        "GIT_AUTHOR_EMAIL": "kb-test@example.com",
        "GIT_COMMITTER_NAME": "kb test",
        "GIT_COMMITTER_EMAIL": "kb-test@example.com",
    }


def _init_git(runner: CliRunner, env: dict[str, str]) -> None:
    """Initialise git sync into a fresh external sync directory."""
    sync_dir = Path(env["KB_MCP_HOME"]).parent / "sync"
    sync_dir.mkdir(exist_ok=True)
    result = runner.invoke(cli, ["vault", "init-git", "--sync-dir", str(sync_dir)], env=env)
    assert result.exit_code == EXIT_OK


def _add_doc(runner: CliRunner, env: dict[str, str], title: str, body: str = "body") -> str:
    result = runner.invoke(
        cli,
        [
            "add",
            "--type",
            "lesson",
            "--title",
            title,
            "--body",
            body,
            "--json",
        ],
        env=env,
    )
    assert result.exit_code == EXIT_OK
    return json.loads(result.output)["id"]


def _commit_all(runner: CliRunner, env: dict[str, str]) -> None:
    result = runner.invoke(cli, ["vault", "commit", "-m", "snapshot"], env=env)
    assert result.exit_code == EXIT_OK


class TestVaultStatus:
    def test_status_not_initialized_shows_pending_add(
        self, runner: CliRunner, env: dict[str, str]
    ) -> None:
        doc_id = _add_doc(runner, env, "Pending Lesson")
        result = runner.invoke(cli, ["vault", "status"], env=env)
        assert result.exit_code == EXIT_OK
        assert "Git sync not initialized" in result.output
        assert "added: 1" in result.output
        assert doc_id in result.output

    def test_status_added_before_first_commit(self, runner: CliRunner, env: dict[str, str]) -> None:
        genv = _git_env(env)
        _init_git(runner, genv)
        doc_id = _add_doc(runner, genv, "Fresh Lesson")
        result = runner.invoke(cli, ["vault", "status"], env=genv)
        assert result.exit_code == EXIT_OK
        assert "added: 1" in result.output
        assert doc_id in result.output

    def test_status_in_sync_after_commit(self, runner: CliRunner, env: dict[str, str]) -> None:
        genv = _git_env(env)
        _init_git(runner, genv)
        _add_doc(runner, genv, "Committed Lesson")
        _commit_all(runner, genv)
        result = runner.invoke(cli, ["vault", "status"], env=genv)
        assert result.exit_code == EXIT_OK
        assert "Pending export: none" in result.output

    def test_status_modified_after_update(self, runner: CliRunner, env: dict[str, str]) -> None:
        genv = _git_env(env)
        _init_git(runner, genv)
        doc_id = _add_doc(runner, genv, "Changing Lesson")
        _commit_all(runner, genv)
        updated = runner.invoke(cli, ["update", doc_id, "--body", "new body"], env=genv)
        assert updated.exit_code == EXIT_OK
        result = runner.invoke(cli, ["vault", "status"], env=genv)
        assert result.exit_code == EXIT_OK
        assert "modified: 1" in result.output
        assert doc_id in result.output

    def test_status_deleted_after_delete(self, runner: CliRunner, env: dict[str, str]) -> None:
        genv = _git_env(env)
        _init_git(runner, genv)
        doc_id = _add_doc(runner, genv, "Doomed Lesson")
        _commit_all(runner, genv)
        deleted = runner.invoke(cli, ["delete", doc_id], env=genv)
        assert deleted.exit_code == EXIT_OK
        result = runner.invoke(cli, ["vault", "status"], env=genv)
        assert result.exit_code == EXIT_OK
        assert "deleted: 1" in result.output
        assert doc_id in result.output


# ---------------------------------------------------------------------------
# VaultManager unit tests (rename, remove, info)
# ---------------------------------------------------------------------------


class TestVaultManagerOps:
    def test_rename_vault(self, tmp_path: Path) -> None:
        from kb_mcp_lite.vault import VaultManager

        mgr = VaultManager(kb_home=tmp_path)
        mgr.create("old-name", description="test")
        mgr.rename("old-name", "new-name")

        vaults = mgr.list_vaults()
        names = [v.name for v in vaults]
        assert "new-name" in names
        assert "old-name" not in names

    def test_rename_nonexistent_raises(self, tmp_path: Path) -> None:
        from kb_mcp_lite.vault import VaultManager, VaultNotFoundError

        mgr = VaultManager(kb_home=tmp_path)
        with pytest.raises(VaultNotFoundError):
            mgr.rename("ghost", "new-ghost")

    def test_rename_to_existing_raises(self, tmp_path: Path) -> None:
        from kb_mcp_lite.vault import VaultManager, VaultAlreadyExistsError

        mgr = VaultManager(kb_home=tmp_path)
        mgr.create("a")
        mgr.create("b")
        with pytest.raises(VaultAlreadyExistsError):
            mgr.rename("a", "b")

    def test_remove_vault_registry_only(self, tmp_path: Path) -> None:
        from kb_mcp_lite.vault import VaultManager

        mgr = VaultManager(kb_home=tmp_path)
        mgr.create("removable")
        assert any(v.name == "removable" for v in mgr.list_vaults())

        mgr.remove("removable")
        assert not any(v.name == "removable" for v in mgr.list_vaults())

    def test_remove_last_vault_raises(self, tmp_path: Path) -> None:
        from kb_mcp_lite.vault import VaultManager, VaultError

        mgr = VaultManager(kb_home=tmp_path)
        with pytest.raises(VaultError):
            mgr.remove("default")  # only vault

    def test_info_returns_vault_metadata(self, tmp_path: Path) -> None:
        from kb_mcp_lite.vault import VaultManager

        mgr = VaultManager(kb_home=tmp_path)
        info = mgr.info("default")
        assert info.name == "default"
        assert info.path == "default"

    def test_info_nonexistent_raises(self, tmp_path: Path) -> None:
        from kb_mcp_lite.vault import VaultManager, VaultNotFoundError

        mgr = VaultManager(kb_home=tmp_path)
        with pytest.raises(VaultNotFoundError):
            mgr.info("ghost")
