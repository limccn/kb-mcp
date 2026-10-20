"""Vault management for kb-mcp (v0.4).

A *vault* is a named, independent SQLite knowledge base. Each vault lives in
its own subdirectory under ``KB_MCP_HOME`` (default
``~/.local/share/kb-mcp/``) and contains a ``kb.db`` file.

The vault registry (``vaults.json``) tracks known vaults and the current
active vault. Environment variable ``KB_MCP_VAULT`` overrides the current
vault at runtime.

Migration from v0.3: if ``vaults.json`` does not exist but
``KB_MCP_HOME/kb.db`` does, the first access auto-creates the registry and
registers the existing database as the ``default`` vault.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VaultError(Exception):
    """Base exception for vault operations."""


class VaultNotFoundError(VaultError):
    """Raised when a named vault does not exist."""


class VaultAlreadyExistsError(VaultError):
    """Raised when creating a vault whose name already exists."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class VaultInfo:
    """Serialisable metadata for a single vault."""

    name: str
    path: str  # relative to KB_MCP_HOME
    description: str = ""
    sync_dir: str | None = None  # external git sync directory, if set


# ---------------------------------------------------------------------------
# VaultManager
# ---------------------------------------------------------------------------


_DEFAULT_VAULT_NAME = "default"
_VAULTS_JSON = "vaults.json"


def _strip_ssh_warnings(stderr: str) -> str:
    """Remove common SSH/client warning lines from git stderr."""
    lines = []
    for line in stderr.splitlines():
        if line.startswith("**"):
            continue
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines) if lines else stderr.strip()


def get_kb_home() -> Path:
    """Return the KB root directory.

    Order:
    1. ``KB_MCP_HOME`` env var, if set.
    2. ``data_dir`` from ``~/.config/kb-mcp/config.yaml``, if set.
    3. ``~/.local/share/kb-mcp/`` (XDG-style, all platforms).
    """
    env = os.environ.get("KB_MCP_HOME")
    if env:
        return Path(env)
    try:
        from kb_mcp_lite.config import get_data_dir

        return get_data_dir()
    except ImportError:
        return Path.home() / ".local" / "share" / "kb-mcp"


def get_current_vault_name() -> str:
    """Return the active vault name — the same value every read/write path resolves.

    Delegates to :meth:`VaultManager.get_current` (``KB_MCP_VAULT`` first, then
    the registry), so a report cannot name a different vault than the one
    ``resolve_path()`` would open. An env-only version here once disagreed with
    the registry and made the admin page show ``default`` for another vault.
    """
    return VaultManager().get_current()


class VaultManager:
    """Manage named knowledge-base vaults.

    Usage::

        mgr = VaultManager()
        mgr.list_vaults()       # [VaultInfo(name='default', ...), ...]
        mgr.create("project-x") # creates subdirectory + registers
        mgr.switch("project-x") # sets current in vaults.json
        mgr.resolve_path()      # full path to current vault's kb.db
    """

    def __init__(self, kb_home: Path | str | None = None) -> None:
        self._kb_home = Path(kb_home) if kb_home else get_kb_home()
        self._registry_path = self._kb_home / _VAULTS_JSON
        self._ensure_registry()

    # ---- properties -----------------------------------------------------

    @property
    def kb_home(self) -> Path:
        return self._kb_home

    # ---- registry I/O ---------------------------------------------------

    def _read_registry(self) -> dict[str, Any]:
        if not self._registry_path.exists():
            return {"current": _DEFAULT_VAULT_NAME, "vaults": []}
        return json.loads(self._registry_path.read_text(encoding="utf-8"))

    def _write_registry(self, data: dict[str, Any]) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _ensure_registry(self) -> None:
        """Create vaults.json if missing, migrating any existing kb.db.

        Always ensures at least a ``default`` vault exists.
        """
        if self._registry_path.exists():
            return
        legacy_db = self._kb_home / "kb.db"
        default_dir = self._kb_home / _DEFAULT_VAULT_NAME
        default_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "current": _DEFAULT_VAULT_NAME,
            "vaults": [],
        }
        if legacy_db.exists():
            # Migrate: register the existing kb.db as the default vault
            target = default_dir / "kb.db"
            if not target.exists():
                shutil.move(str(legacy_db), str(target))
        data["vaults"].append(
            {
                "name": _DEFAULT_VAULT_NAME,
                "path": _DEFAULT_VAULT_NAME,
                "description": "Default vault",
            }
        )
        self._write_registry(data)

    # ---- vault CRUD -----------------------------------------------------

    def list_vaults(self) -> list[VaultInfo]:
        """Return metadata for every registered vault."""
        data = self._read_registry()
        return [VaultInfo(**v) for v in data.get("vaults", [])]

    def get_current(self) -> str:
        """Return the current active vault name.

        ``KB_MCP_VAULT`` (when set to a non-blank value) overrides the
        registry's ``current`` key — the module docstring promises the variable
        wins, and every read/write path reaches its vault through
        :meth:`resolve_path`, which defaults to this method.
        """
        env = os.environ.get("KB_MCP_VAULT", "").strip()
        if env:
            return env
        data = self._read_registry()
        return data.get("current", _DEFAULT_VAULT_NAME)

    def create(
        self,
        name: str,
        description: str = "",
    ) -> VaultInfo:
        """Create a new vault with the given name.

        Creates the vault subdirectory and registers it in vaults.json.
        Does NOT create the SQLite database (that happens lazily on first
        access via SqliteStore).

        Raises:
            VaultAlreadyExistsError: if the name is already registered.
        """
        if not name or "/" in name or "\\" in name:
            raise VaultError(f"invalid vault name: {name!r}")
        data = self._read_registry()
        existing = {v["name"] for v in data["vaults"]}
        if name in existing:
            raise VaultAlreadyExistsError(f"vault {name!r} already exists")
        vault_dir = self._kb_home / name
        vault_dir.mkdir(parents=True, exist_ok=True)
        info = {"name": name, "path": name, "description": description}
        data["vaults"].append(info)
        self._write_registry(data)
        return VaultInfo(**info)

    def switch(self, name: str) -> None:
        """Set the current active vault.

        Raises:
            VaultNotFoundError: if the vault is not registered.
        """
        data = self._read_registry()
        names = {v["name"] for v in data["vaults"]}
        if name not in names:
            raise VaultNotFoundError(f"vault {name!r} not found")
        data["current"] = name
        self._write_registry(data)

    def rename(self, old_name: str, new_name: str) -> None:
        """Rename a vault (both registry entry and directory).

        Raises:
            VaultNotFoundError: if ``old_name`` does not exist.
            VaultAlreadyExistsError: if ``new_name`` is already taken.
        """
        if not new_name or "/" in new_name or "\\" in new_name:
            raise VaultError(f"invalid vault name: {new_name!r}")
        data = self._read_registry()
        names = {v["name"] for v in data["vaults"]}
        if old_name not in names:
            raise VaultNotFoundError(f"vault {old_name!r} not found")
        if new_name in names:
            raise VaultAlreadyExistsError(f"vault {new_name!r} already exists")

        old_dir = self._kb_home / old_name
        new_dir = self._kb_home / new_name
        if old_dir.exists():
            old_dir.rename(new_dir)

        for v in data["vaults"]:
            if v["name"] == old_name:
                v["name"] = new_name
                v["path"] = new_name
        if data.get("current") == old_name:
            data["current"] = new_name
        self._write_registry(data)

    def remove(self, name: str, *, delete_files: bool = False) -> None:
        """Remove a vault from the registry.

        Args:
            name: Vault name.
            delete_files: If True, also delete the vault directory and
                all its contents. Default False (registry-only removal).

        Raises:
            VaultNotFoundError: if the vault is not registered.
            VaultError: if trying to remove the last vault.
        """
        data = self._read_registry()
        vaults = data["vaults"]
        idx = next((i for i, v in enumerate(vaults) if v["name"] == name), None)
        if idx is None:
            raise VaultNotFoundError(f"vault {name!r} not found")
        if len(vaults) == 1:
            raise VaultError("cannot remove the last vault")
        vaults.pop(idx)
        if data.get("current") == name:
            data["current"] = vaults[0]["name"]
        self._write_registry(data)
        if delete_files:
            vault_dir = self._kb_home / name
            if vault_dir.exists():
                shutil.rmtree(vault_dir)

    def info(self, name: str) -> VaultInfo:
        """Return metadata for a single vault.

        Raises:
            VaultNotFoundError: if the vault is not registered.
        """
        data = self._read_registry()
        for v in data["vaults"]:
            if v["name"] == name:
                return VaultInfo(**v)
        raise VaultNotFoundError(f"vault {name!r} not found")

    # ---- path resolution ------------------------------------------------

    def resolve_path(self, name: str | None = None) -> Path:
        """Return the full path to a vault's ``kb.db``.

        If ``name`` is None, uses the current active vault.
        """
        if name is None:
            name = self.get_current()
        data = self._read_registry()
        for v in data["vaults"]:
            if v["name"] == name:
                return self._kb_home / v["path"] / "kb.db"
        raise VaultNotFoundError(f"vault {name!r} not found")

    def vault_dir(self, name: str | None = None) -> Path:
        """Return the vault's directory path (parent of ``kb.db``)."""
        return self.resolve_path(name).parent

    def md_dir(self, name: str | None = None) -> Path:
        """Return the ``md/`` sync directory inside the vault."""
        return self.vault_dir(name) / "md"

    # ---- Git sync -------------------------------------------------------

    def _sync_dir(self, name: str | None = None) -> Path:
        """Return the git sync directory for a vault.

        Checks vaults.json for an explicit ``sync_dir``; falls back to
        the vault's own ``md/`` directory.
        """
        data = self._read_registry()
        for v in data["vaults"]:
            if v["name"] == (name or self.get_current()):
                sync = v.get("sync_dir")
                if sync:
                    return Path(sync) / "md"
                break
        return self.md_dir(name)

    def init_git(self, name: str | None = None, sync_dir: Path | str | None = None) -> str:
        """Initialise a Git repository for the vault's Markdown export.

        By default, creates the repo inside the vault directory (``md/``
        subdirectory). Pass ``sync_dir`` to point at an existing git clone
        — the vault's ``md/`` directory will be created there, and the
        location is saved in vaults.json so subsequent sync commands reuse it.

        Creates ``.gitignore`` and an empty ``md/`` directory. Returns the
        git command output.
        """
        import subprocess

        vdir = self.vault_dir(name)
        if sync_dir is None:
            mdir = self.md_dir(name)
        else:
            sync_dir = Path(sync_dir).resolve()
            # Save sync_dir in vaults.json
            data = self._read_registry()
            vault_name = name or self.get_current()
            for v in data["vaults"]:
                if v["name"] == vault_name:
                    v["sync_dir"] = str(sync_dir)
                    break
            self._write_registry(data)
            mdir = sync_dir / "md"
        mdir.mkdir(parents=True, exist_ok=True)
        # Placeholder so git tracks the md/ directory
        placeholder = mdir / ".gitkeep"
        placeholder.write_text("")

        # .gitignore — in the git root (sync_dir or vault dir)
        gitignore_root = sync_dir if sync_dir else vdir
        gitignore = gitignore_root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# kb-mcp vault sync\n"
                "kb.db\n"
                "*.db-journal\n"
                "*.db-wal\n"
                "*.db-shm\n"
                "__pycache__/\n"
                ".venv/\n"
            )

        # git init — in the sync root (sync_dir itself, not md/)
        git_cwd = str(sync_dir) if sync_dir else str(vdir)
        result = subprocess.run(
            ["git", "init"],
            cwd=git_cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise VaultError(f"git init failed: {_strip_ssh_warnings(result.stderr)}")
        return result.stdout.strip()

    def commit(
        self,
        message: str,
        name: str | None = None,
        full: bool = False,
    ) -> str:
        """Export the vault to Markdown, then git add + git commit.

        Returns the git commit output.
        """
        import subprocess

        from kb_mcp_lite.md_io import export_dir as _export_dir
        from kb_mcp_lite.store.sqlite import SqliteStore

        sync_root = self._sync_dir(name)
        mdir = self.md_dir(name)  # fallback if sync_dir not set
        git_dir = sync_root.parent if sync_root != self.md_dir(name) else self.vault_dir(name)
        export_target = sync_root if sync_root != self.md_dir(name) else mdir

        # Ensure git repo exists
        if not (git_dir / ".git").exists():
            raise VaultError("vault not initialised for git; run `kb vault init-git` first")

        # Export to md/
        store = SqliteStore(self.resolve_path(name))
        try:
            _export_dir(store, export_target, force=True, incremental=not full)
        finally:
            store.close()

        # git add + commit
        result = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise VaultError(f"git add failed: {_strip_ssh_warnings(result.stderr)}")

        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
                return "nothing to commit"
            raise VaultError(f"git commit failed: {_strip_ssh_warnings(result.stderr)}")
        return result.stdout.strip()

    def push(
        self,
        remote: str = "origin",
        branch: str = "main",
        name: str | None = None,
    ) -> str:
        """Git push the vault's Markdown export.

        Returns the git output.
        """
        import os

        sync_root = self._sync_dir(name)
        git_dir = sync_root.parent if sync_root != self.md_dir(name) else self.vault_dir(name)

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        result = subprocess.run(
            ["git", "push", remote, branch],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise VaultError(f"git push failed: {_strip_ssh_warnings(result.stderr)}")
        return result.stdout.strip()

    def pull(
        self,
        remote: str = "origin",
        branch: str = "main",
        name: str | None = None,
    ) -> str:
        """Git pull and import Markdown into the vault.

        Returns the git + import summary.
        """
        import subprocess

        from kb_mcp_lite.md_io import import_dir as _import_dir
        from kb_mcp_lite.store.sqlite import SqliteStore

        sync_root = self._sync_dir(name)
        git_dir = sync_root.parent if sync_root != self.md_dir(name) else self.vault_dir(name)
        import_target = sync_root if sync_root != self.md_dir(name) else self.md_dir(name)

        if not (git_dir / ".git").exists():
            raise VaultError("vault not initialised for git; run `kb vault init-git` first")

        import os

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        result = subprocess.run(
            ["git", "pull", "--no-edit", remote, branch],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise VaultError(f"git pull failed: {_strip_ssh_warnings(result.stderr)}")
        pull_output = result.stdout.strip()

        # Import the Markdown files (skip if already up to date)
        is_up_to_date = "Already up to date" in pull_output or "Already up-to-date" in pull_output
        if import_target.exists() and not is_up_to_date:
            store = SqliteStore(self.resolve_path(name))
            try:
                report = _import_dir(store, import_target)
            finally:
                store.close()
            import_summary = (
                f"imported {report.inserted + report.updated} docs ({report.skipped} skipped)"
            )
        elif is_up_to_date:
            import_summary = "already up to date (no files imported)"
        else:
            import_summary = "no md/ directory to import"

        return f"{pull_output}\n{import_summary}"

    def status(
        self,
        name: str | None = None,
    ) -> str:
        """Get the Git status of the vault's sync repository.

        The report opens with a "pending export" section — documents whose
        database state has not been exported to the sync directory yet —
        followed by the plain ``git status`` output.
        """
        import subprocess

        sync_root = self._sync_dir(name)
        git_dir = sync_root.parent if sync_root != self.md_dir(name) else self.vault_dir(name)
        vault_name = name or self.get_current()
        pending_summary = self._pending_export_summary(name)

        if not (git_dir / ".git").exists():
            return (
                f"Vault: {vault_name}\nGit Directory: {git_dir}\n\n"
                f"{pending_summary}\n\n"
                "Git sync not initialized for this vault. "
                "Run `kb vault init-git` to initialize."
            )

        # Get current branch
        result_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        branch = result_branch.stdout.strip() if result_branch.returncode == 0 else "unknown"

        # Get status
        result_status = subprocess.run(
            ["git", "status"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        if result_status.returncode != 0:
            raise VaultError(f"git status failed: {_strip_ssh_warnings(result_status.stderr)}")

        status_output = result_status.stdout.strip()
        return (
            f"Vault: {vault_name}\nBranch: {branch}\nGit Directory: {git_dir}\n\n"
            f"{pending_summary}\n\n{status_output}"
        )

    def _pending_export_summary(self, name: str | None = None) -> str:
        """Summarise database changes not yet exported to the sync directory.

        Read-only: nothing is exported or written.
        """
        from kb_mcp_lite.md_io import pending_export
        from kb_mcp_lite.store.sqlite import SqliteStore

        store = SqliteStore(self.resolve_path(name))
        try:
            pending = pending_export(store, self._sync_dir(name))
        finally:
            store.close()

        if pending.total == 0:
            return "Pending export: none — database and export directory are in sync."

        lines = [f"Pending export: {pending.total} document(s) differ from the export directory"]
        for label, ids in (
            ("added", pending.added),
            ("modified", pending.modified),
            ("deleted", pending.deleted),
        ):
            if not ids:
                continue
            shown = ", ".join(ids[:5])
            if len(ids) > 5:
                shown += f", … and {len(ids) - 5} more"
            lines.append(f"  {label}: {len(ids)} ({shown})")
        lines.append("Run `kb vault commit -m <message>` to export and commit these changes.")
        return "\n".join(lines)

    def sync(
        self,
        message: str = "sync: auto-commit local changes",
        remote: str = "origin",
        branch: str = "main",
        name: str | None = None,
    ) -> str:
        """Run a full bi-directional sync cycle.

        1. Commit local database changes to the local Git repository.
        2. Pull incoming changes from the remote Git repository.
        3. Push local changes back to the remote.
        4. Re-import Git-merged Markdown files into the SQLite database.
        """
        vault_name = name or self.get_current()
        output_lines = [f"Starting bi-directional sync for vault '{vault_name}'..."]

        # Step 1: Export & Commit local changes
        try:
            commit_res = self.commit(message=message, name=name)
            output_lines.append(f"-> Commit local changes: {commit_res}")
        except Exception as e:
            output_lines.append(f"-> Commit local changes failed: {e}")
            raise VaultError(f"Sync aborted. Commit failed: {e}") from e

        # Step 2: Pull remote changes
        try:
            pull_res = self.pull(remote=remote, branch=branch, name=name)
            output_lines.append(f"-> Pull remote changes:\n{pull_res}")
        except Exception as e:
            output_lines.append(f"-> Pull failed: {e}")
            raise VaultError(f"Sync aborted. Pull failed: {e}") from e

        # Step 3: Push changes back
        try:
            push_res = self.push(remote=remote, branch=branch, name=name)
            output_lines.append(f"-> Push local changes: {push_res or 'OK'}")
        except Exception as e:
            output_lines.append(f"-> Push failed (continuing to import): {e}")

        return "\n".join(output_lines)

    def git_log(self, name: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """Return the recent Git commit history for the vault's sync repository."""
        import subprocess

        sync_root = self._sync_dir(name)
        git_dir = sync_root.parent if sync_root != self.md_dir(name) else self.vault_dir(name)
        if not (git_dir / ".git").exists():
            return []

        # Format: hash%x1fshort_hash%x1fauthor_name%x1fauthor_email%x1fauthor_date_iso%x1fauthor_date_relative%x1fsubject
        fmt = "%H%x1f%h%x1f%an%x1f%ae%x1f%ad%x1f%ar%x1f%s"
        res = subprocess.run(
            ["git", "log", f"-n{limit}", f"--pretty=format:{fmt}", "--date=iso"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return []
        commits = []
        for line in res.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\x1f")
            if len(parts) >= 7:
                commits.append({
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "author": parts[2],
                    "email": parts[3],
                    "date": parts[4],
                    "relative_date": parts[5],
                    "message": parts[6],
                })
        return commits

    def git_status_info(self, name: str | None = None) -> dict[str, Any]:
        """Return structured Git and pending export status for the vault."""
        import subprocess
        from kb_mcp_lite.md_io import pending_export
        from kb_mcp_lite.store.sqlite import SqliteStore

        sync_root = self._sync_dir(name)
        git_dir = sync_root.parent if sync_root != self.md_dir(name) else self.vault_dir(name)
        vault_name = name or self.get_current()
        is_git = (git_dir / ".git").exists()

        store = SqliteStore(self.resolve_path(name))
        try:
            pending = pending_export(store, self._sync_dir(name))
        finally:
            store.close()

        pending_dict = {
            "total": pending.total,
            "added": pending.added,
            "modified": pending.modified,
            "deleted": pending.deleted,
        }

        if not is_git:
            return {
                "vault_name": vault_name,
                "git_dir": str(git_dir),
                "is_git": False,
                "branch": "",
                "status_raw": "Git sync not initialized for this vault.",
                "pending_export": pending_dict,
                "staged": [],
                "unstaged": [],
                "untracked": [],
                "ahead": 0,
                "behind": 0,
            }

        # Branch
        res_b = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        branch = res_b.stdout.strip() if res_b.returncode == 0 else "unknown"

        # Raw status
        res_s = subprocess.run(
            ["git", "status"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        status_raw = res_s.stdout.strip() if res_s.returncode == 0 else ""

        # Porcelain status for structured staged/unstaged/untracked
        res_p = subprocess.run(
            ["git", "status", "--porcelain=v1", "-b"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        staged, unstaged, untracked = [], [], []
        ahead, behind = 0, 0
        if res_p.returncode == 0:
            lines = res_p.stdout.splitlines()
            for line in lines:
                if line.startswith("##"):
                    if "[ahead " in line:
                        try:
                            ahead = int(line.split("[ahead ")[1].split("]")[0].split(",")[0])
                        except Exception:
                            pass
                    if "behind " in line:
                        try:
                            behind = int(line.split("behind ")[1].split("]")[0])
                        except Exception:
                            pass
                    continue
                if len(line) >= 3:
                    x, y, path = line[0], line[1], line[3:]
                    if x == "?" and y == "?":
                        untracked.append(path)
                    else:
                        if x not in (" ", "?"):
                            staged.append({"status": x, "path": path})
                        if y not in (" ", "?"):
                            unstaged.append({"status": y, "path": path})

        is_clean = (
            len(staged) == 0
            and len(unstaged) == 0
            and len(untracked) == 0
            and pending_dict["total"] == 0
        )

        return {
            "vault_name": vault_name,
            "git_dir": str(git_dir),
            "is_git": True,
            "branch": branch,
            "clean": is_clean,
            "status_raw": status_raw,
            "pending_export": pending_dict,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "modified": [u["path"] for u in unstaged if u.get("status") == "M"],
            "ahead": ahead,
            "behind": behind,
        }

    def git_diff(
        self,
        name: str | None = None,
        path: str | None = None,
        staged: bool = False,
    ) -> str:
        """Return git diff output for the sync repository."""
        import subprocess

        sync_root = self._sync_dir(name)
        git_dir = sync_root.parent if sync_root != self.md_dir(name) else self.vault_dir(name)
        if not (git_dir / ".git").exists():
            return ""

        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        if path:
            cmd.extend(["--", path])

        res = subprocess.run(
            cmd,
            cwd=str(git_dir),
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return ""

        diff_out = res.stdout
        # If specific untracked file, generate unified diff against /dev/null
        if not diff_out and path and not staged:
            target_p = git_dir / path
            if target_p.is_file():
                res_untracked = subprocess.run(
                    ["git", "diff", "--no-index", "--", "/dev/null", path],
                    cwd=str(git_dir),
                    capture_output=True,
                    text=True,
                )
                if res_untracked.stdout:
                    diff_out = res_untracked.stdout
        return diff_out

    def pending_export_diff(
        self,
        doc_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, str]:
        """Return unified diffs for pending export documents (Database vs on-disk Markdown)."""
        import difflib
        from kb_mcp_lite.md_io import _export_candidate, _same_export_content, render_document
        from kb_mcp_lite.store.sqlite import SqliteStore

        store = SqliteStore(self.resolve_path(name))
        diffs: dict[str, str] = {}
        try:
            base = self._sync_dir(name).resolve()
            all_docs = store.export_all(include_deleted=True)
            for doc in all_docs:
                if doc_id and doc.id != doc_id:
                    continue
                if doc.deleted_at is not None:
                    if doc.source:
                        candidate = _export_candidate(base, doc)
                        if candidate.is_file():
                            old_lines = candidate.read_text(encoding="utf-8").splitlines(keepends=True)
                            d = "".join(
                                difflib.unified_diff(
                                    old_lines,
                                    [],
                                    fromfile=f"a/{candidate.name}",
                                    tofile="/dev/null",
                                )
                            )
                            if d:
                                diffs[doc.id] = d
                    continue

                candidate = _export_candidate(base, doc)
                rendered = render_document(doc, outlinks=store.outlinks(doc.id))
                rendered_lines = rendered.splitlines(keepends=True)

                if not candidate.is_file():
                    d = "".join(
                        difflib.unified_diff(
                            [],
                            rendered_lines,
                            fromfile="/dev/null",
                            tofile=f"b/{candidate.name}",
                        )
                    )
                    if d:
                        diffs[doc.id] = d
                else:
                    disk_text = candidate.read_text(encoding="utf-8")
                    if not _same_export_content(disk_text, rendered):
                        old_lines = disk_text.splitlines(keepends=True)
                        d = "".join(
                            difflib.unified_diff(
                                old_lines,
                                rendered_lines,
                                fromfile=f"a/{candidate.name}",
                                tofile=f"b/{candidate.name}",
                            )
                        )
                        if d:
                            diffs[doc.id] = d
        finally:
            store.close()
        return diffs


__all__ = [
    "VaultManager",
    "VaultInfo",
    "VaultError",
    "VaultNotFoundError",
    "VaultAlreadyExistsError",
    "get_kb_home",
    "get_current_vault_name",
]
