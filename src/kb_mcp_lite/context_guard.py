"""Git Diff Context Guard and Proactive Decision/Lesson Recommender.

Analyzes uncommitted changes (git diff) or changed file paths in a repository,
queries the knowledge base for relevant decisions, lessons, runbooks and APIs,
and formats proactive context/constraints for AI agents.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from kb_mcp_lite.schema import SearchHit
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import VaultManager


def get_git_diff_summary(cwd: Path | str | None = None) -> dict[str, Any]:
    """Extract modified file paths and changed symbols from `git diff`."""
    work_dir = Path(cwd) if cwd else Path.cwd()
    if not (work_dir / ".git").exists() and not (work_dir.parent / ".git").exists():
        return {"files": [], "keywords": [], "raw_diff": ""}

    try:
        # Get list of modified files (both staged and unstaged).
        #
        # stdin=DEVNULL is load-bearing, not hygiene: under the stdio transport
        # the server's stdin is the JSON-RPC pipe, with a read pending on it.
        # git probes its stdin handle at startup (ntdll!NtQueryObject with
        # ObjectNameInformation) and that name query blocks on a pipe that has
        # a pending read — the tool then never returns. git diff never reads
        # stdin, so NUL is safe. Details: .trellis/tasks/09-29-kbmcp-e2e-defects
        # (R5).
        #
        # encoding="utf-8" is load-bearing too: the default text mode on Windows
        # is cp932, git emits UTF-8, and a CJK diff crashes the capture reader
        # thread — stdout comes back None and the except below swallows it into
        # a silently empty result (R5b).
        res_files = subprocess.run(
            ["git", "diff", "HEAD", "--name-only"],
            cwd=str(work_dir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        files = [f.strip() for f in res_files.stdout.splitlines() if f.strip()]

        # Get diff patch
        res_diff = subprocess.run(
            ["git", "diff", "HEAD", "-U1"],
            cwd=str(work_dir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        diff_text = res_diff.stdout

        # Extract keywords from changed paths and modified function/class names
        keywords: set[str] = set()
        for f in files:
            p = Path(f)
            keywords.add(p.stem)
            for part in p.parts[:-1]:
                if part not in ("src", "lib", "pkg", "app", "tests"):
                    keywords.add(part)

        # Extract added/removed function or variable keywords
        for line in diff_text.splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                words = re.findall(r"[A-Za-z0-9_]{3,}", line)
                for w in words[:10]:
                    if not w.isdigit():
                        keywords.add(w)

        return {
            "files": files,
            "keywords": sorted(keywords)[:20],
            "raw_diff": diff_text[:5000],
        }
    except Exception:
        return {"files": [], "keywords": [], "raw_diff": ""}


class ContextGuard:
    """Evaluates git changes against the knowledge base and generates proactive constraints."""

    def __init__(self, store: SqliteStore | None = None, vault_name: str | None = None) -> None:
        if store:
            self.store = store
            self._owns_store = False
        else:
            vm = VaultManager()
            v_name = vault_name or vm.get_current()
            self.store = SqliteStore(vm.resolve_path(v_name))
            self._owns_store = True

    def evaluate_diff(
        self, cwd: Path | str | None = None, limit_per_type: int = 3
    ) -> dict[str, Any]:
        """Scan git diff and find related lessons, decisions, and runbooks."""
        diff_info = get_git_diff_summary(cwd)
        files = diff_info["files"]
        keywords = diff_info["keywords"]

        if not files and not keywords:
            return {
                "has_recommendations": False,
                "files": [],
                "decisions": [],
                "lessons": [],
                "apis": [],
                "prompt_context": "",
            }

        # Search for related items
        query = " ".join(keywords[:10])
        hits: list[SearchHit] = self.store.search(query, limit=15, mode="hybrid") if query else []

        decisions = []
        lessons = []
        apis = []
        seen = set()

        for h in hits:
            if h.doc.id in seen:
                continue
            seen.add(h.doc.id)
            if h.doc.type == "decision":
                decisions.append(h.doc)
            elif h.doc.type == "lesson":
                lessons.append(h.doc)
            elif h.doc.type == "api":
                apis.append(h.doc)

        decisions = decisions[:limit_per_type]
        lessons = lessons[:limit_per_type]
        apis = apis[:limit_per_type]

        # Generate structured context prompt for AI agents
        prompt_lines: list[str] = []
        if decisions or lessons or apis:
            prompt_lines.append(
                "<!-- KB-MCP CONTEXT GUARD: Mandatory Team Constraints & Lessons -->"
            )
            prompt_lines.append("The current codebase changes relate to existing team knowledge:")

            if decisions:
                prompt_lines.append("\n[Architectural Decisions to Follow]")
                for d in decisions:
                    prompt_lines.append(f"- {d.id} ({d.title}): {d.body[:150].strip()}...")

            if lessons:
                prompt_lines.append("\n[Prior Lessons / Pitfalls to Avoid]")
                for lesson in lessons:
                    prompt_lines.append(
                        f"- ⚠️ {lesson.id} ({lesson.title}): {lesson.body[:150].strip()}..."
                    )

            if apis:
                prompt_lines.append("\n[Related API Contracts]")
                for a in apis:
                    prompt_lines.append(f"- {a.id} ({a.title})")

            prompt_lines.append(
                "\nPlease review these constraints to ensure backward compatibility and prevent regression."
            )

        return {
            "has_recommendations": bool(decisions or lessons or apis),
            "files": files,
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "lessons": [lesson.model_dump(mode="json") for lesson in lessons],
            "apis": [a.model_dump(mode="json") for a in apis],
            "prompt_context": "\n".join(prompt_lines),
        }

    def close(self) -> None:
        if self._owns_store:
            self.store.close()
