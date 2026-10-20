"""File watcher and incremental indexer for kb-mcp.

Watches the vault's Markdown directory (`md/`) for changes (creation, modification,
deletion) and synchronizes them to the SQLite database with debouncing.

Two operating modes are supported:

* ``event`` (default when ``watchfiles`` is installed): inotify / FSEvents /
  ReadDirectoryChangesW via the ``watchfiles`` package. The watcher reacts to
  filesystem events with sub-100ms latency and zero idle CPU.
* ``poll`` (fallback): periodic ``os.walk`` + mtime comparison. Used when
  ``watchfiles`` is not installed, or when running inside Docker bind mounts
  where inotify does not propagate reliably.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Literal
from collections.abc import Callable

from kb_mcp_lite.md_io import parse_frontmatter, doc_from_frontmatter, _coerce_links
from kb_mcp_lite.schema import NotFoundError
from kb_mcp_lite.store.sqlite import SqliteStore
from kb_mcp_lite.vault import VaultManager

logger = logging.getLogger("kb_mcp_lite.watcher")

try:
    from watchfiles import Change, awatch

    _WATCHFILES_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when extra is missing
    Change = None  # type: ignore[assignment,misc]
    awatch = None  # type: ignore[assignment]
    _WATCHFILES_AVAILABLE = False

WatchMode = Literal["event", "poll", "auto"]


def _detect_docker() -> bool:
    """Return True if we appear to be running inside a container.

    Heuristic check only — looks for the well-known Docker ``/.dockerenv`` file
    or the ``docker`` / ``containerd`` markers in cgroup info. Used to default
    the watcher to poll mode, since bind-mounted directories often do not
    propagate inotify events out of the host filesystem.
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup")
        if cgroup.exists():
            txt = cgroup.read_text(encoding="utf-8", errors="ignore")
            if "docker" in txt or "containerd" in txt or "kubepods" in txt:
                return True
    except OSError:
        pass
    return False


def resolve_mode(mode: WatchMode = "auto") -> Literal["event", "poll"]:
    """Resolve an ``auto`` mode to a concrete ``event`` or ``poll`` choice."""
    if mode != "auto":
        return mode  # type: ignore[return-value,unused-ignore]
    if not _WATCHFILES_AVAILABLE:
        return "poll"
    if _detect_docker():
        return "poll"
    return "event"


class VaultWatcher:
    """Watches a Markdown directory and syncs changes to a SqliteStore."""

    def __init__(
        self,
        vault_name: str | None = None,
        debounce_seconds: float = 1.0,
        vault_manager: VaultManager | None = None,
        on_change: Callable[[str, str], None] | None = None,
    ) -> None:
        self.vm = vault_manager or VaultManager()
        self.vault_name = vault_name or self.vm.get_current()
        self.debounce_seconds = debounce_seconds
        self.on_change = on_change
        self._running = False
        self._file_mtimes: dict[Path, float] = {}

    @property
    def watch_dir(self) -> Path:
        """Return the Markdown directory being watched."""
        return self.vm._sync_dir(self.vault_name)

    @property
    def db_path(self) -> Path:
        """Return the database path for the target vault."""
        return self.vm.resolve_path(self.vault_name)

    def scan_once(self, store: SqliteStore | None = None) -> dict[str, list[str]]:
        """Perform a single pass scan over watch_dir and sync detected changes.

        Returns a dict of {"created": [...], "updated": [...], "deleted": [...], "errors": [...]}.
        """
        w_dir = self.watch_dir
        if not w_dir.exists():
            w_dir.mkdir(parents=True, exist_ok=True)

        owns_store = store is None
        if owns_store:
            store = SqliteStore(self.db_path)
        assert store is not None  # narrowed for mypy

        created: list[str] = []
        updated: list[str] = []
        deleted: list[str] = []
        errors: list[str] = []

        current_files: set[Path] = set()

        try:
            for root, dirs, files in os.walk(w_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for file_name in files:
                    if file_name.startswith(".") or not file_name.endswith(".md"):
                        continue
                    file_path = (Path(root) / file_name).resolve()
                    current_files.add(file_path)

                    try:
                        mtime = file_path.stat().st_mtime
                    except OSError:
                        continue

                    prev_mtime = self._file_mtimes.get(file_path)
                    if prev_mtime is None:
                        # Newly detected file. Only record the mtime
                        # AFTER a successful sync — otherwise a failing
                        # _sync_file (e.g. invalid frontmatter) would
                        # permanently desync this file from future
                        # scans and we'd never re-try it.
                        err = self._sync_file(store, file_path, w_dir)
                        if err:
                            errors.append(f"{file_path}: {err}")
                        else:
                            self._file_mtimes[file_path] = mtime
                            created.append(str(file_path.relative_to(w_dir)))
                            if self.on_change:
                                self.on_change("created", str(file_path.relative_to(w_dir)))
                    elif mtime > prev_mtime:
                        # Modified file — same rule as above.
                        err = self._sync_file(store, file_path, w_dir)
                        if err:
                            errors.append(f"{file_path}: {err}")
                        else:
                            self._file_mtimes[file_path] = mtime
                            updated.append(str(file_path.relative_to(w_dir)))
                            if self.on_change:
                                self.on_change("updated", str(file_path.relative_to(w_dir)))

            # Detect deleted files
            deleted_files = set(self._file_mtimes.keys()) - current_files
            for del_path in deleted_files:
                try:
                    rel_path = del_path.relative_to(w_dir)
                    file_id = rel_path.with_suffix("").as_posix()
                    del self._file_mtimes[del_path]
                    try:
                        store.delete(file_id)
                        deleted.append(str(rel_path))
                        if self.on_change:
                            self.on_change("deleted", str(rel_path))
                    except NotFoundError:
                        pass
                except Exception as e:
                    errors.append(f"{del_path}: {e}")

        finally:
            if owns_store and store:
                store.close()

        return {
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "errors": errors,
        }

    def _sync_file(self, store: SqliteStore, file_path: Path, base_dir: Path) -> str | None:
        """Parse and upsert a single markdown file into the store."""
        try:
            text = file_path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            rel_source = str(file_path.relative_to(base_dir))
            file_id = Path(rel_source).with_suffix("").as_posix()
            doc = doc_from_frontmatter(fm, body, source=rel_source, file_id=file_id)

            # Store upsert: if exists, update; else add. A soft-deleted
            # row needs an explicit ``restore_deleted`` first because
            # ``update`` refuses to touch rows with ``deleted_at IS
            # NOT NULL`` (see ``SqliteStore.update``). Without this,
            # an editor's atomic-save burst (delete + add -> collapsed
            # to modified) would silently fail on a previously deleted
            # doc — the row would stay soft-deleted and the next scan
            # would never re-try it.
            try:
                existing = store.get(doc.id, include_deleted=True)
            except NotFoundError:
                store.add(doc)
            else:
                if existing.deleted_at is not None:
                    store.restore_deleted(doc.id)
                store.update(
                    doc.id,
                    title=doc.title,
                    body=doc.body,
                    tags=doc.tags,
                    aliases=doc.aliases,
                    metadata=doc.metadata,
                    source=doc.source,
                )

            # Sync links if defined in frontmatter
            raw_links = fm.get("links")
            if raw_links:
                parsed_links = _coerce_links(raw_links)
                for lnk in parsed_links:
                    try:
                        store.link(doc.id, lnk["to"], rel=lnk.get("rel", "relates-to"))
                    except Exception:
                        pass

            return None
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    def _resolve_doc_id(self, file_path: Path, base_dir: Path) -> str | None:
        """Compute the canonical doc id for a file path. Returns None if outside base_dir."""
        try:
            rel = file_path.relative_to(base_dir)
        except ValueError:
            return None
        return rel.with_suffix("").as_posix()

    def _handle_change_set(
        self,
        changes: set,
        store: SqliteStore,
    ) -> dict[str, list[str]]:
        """Apply a single batch of watchfiles Change events to the store.

        ``changes`` is a set of ``(Change, str_path)`` tuples as yielded by
        ``watchfiles.awatch``. Events for the same path within one batch are
        collapsed: a (delete, add) pair is treated as a single update, which
        matches the common editor "save = delete + recreate" pattern.
        """
        assert Change is not None  # guarded by run_event_based caller
        w_dir = self.watch_dir
        created: list[str] = []
        updated: list[str] = []
        deleted: list[str] = []
        errors: list[str] = []

        # Group events by absolute path, preserve the set of change types.
        by_path: dict[Path, set] = {}
        for change_type, path_str in changes:
            p = Path(path_str)
            by_path.setdefault(p, set()).add(change_type)

        for path, types in by_path.items():
            # Atomic-save pattern: if both delete and add fired, treat as update.
            if Change.deleted in types and (Change.added in types or Change.modified in types):
                types = {Change.modified}

            try:
                doc_id = self._resolve_doc_id(path, w_dir)
                if doc_id is None:
                    continue

                if Change.deleted in types:
                    try:
                        store.delete(doc_id)
                        deleted.append(str(path.relative_to(w_dir)))
                        if self.on_change:
                            self.on_change("deleted", str(path.relative_to(w_dir)))
                    except NotFoundError:
                        # File was never indexed (e.g. invalid frontmatter
                        # on create) — nothing to do.
                        pass
                else:
                    # Added or modified: re-sync the file. Only record
                    # the mtime AFTER a successful sync — a failed
                    # _sync_file must remain eligible for re-try on
                    # the next batch. ``_sync_file`` also handles the
                    # soft-deleted + atomic-save recovery case
                    # internally (see its docstring).
                    err = self._sync_file(store, path, w_dir)
                    if err:
                        errors.append(f"{path}: {err}")
                    else:
                        self._file_mtimes[path] = path.stat().st_mtime
                        rel = str(path.relative_to(w_dir))
                        if Change.added in types:
                            created.append(rel)
                            if self.on_change:
                                self.on_change("created", rel)
                        else:
                            updated.append(rel)
                            if self.on_change:
                                self.on_change("updated", rel)
            except Exception as e:  # pragma: no cover - defensive
                errors.append(f"{path}: {type(e).__name__}: {e}")

        return {
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "errors": errors,
        }

    async def _event_loop(  # pragma: no cover - thin async wrapper
        self,
        debounce_ms: int,
        store: SqliteStore,
    ) -> None:
        """Inner async coroutine that drives ``awatch`` and dispatches events."""
        assert awatch is not None
        async for changes in awatch(
            self.watch_dir,
            step=max(50, debounce_ms // 4),
            debounce=debounce_ms,
            recursive=True,
        ):
            if not self._running:
                break
            try:
                res = self._handle_change_set(changes, store)
                total = len(res["created"]) + len(res["updated"]) + len(res["deleted"])
                if total > 0:
                    logger.info(
                        "Watcher (event) synced: %d created, %d updated, %d deleted",
                        len(res["created"]),
                        len(res["updated"]),
                        len(res["deleted"]),
                    )
            except Exception as e:
                logger.error("Watcher error during event handling: %s", e)

    def run_event_based(
        self,
        debounce_ms: int = 200,
        store: SqliteStore | None = None,
    ) -> None:
        """Run the watcher in event-driven mode (requires ``watchfiles``).

        Blocks until ``stop()`` is called. Falls back to a logged warning and
        a single ``scan_once()`` if ``watchfiles`` is not installed; callers
        who need the polling behaviour should pass ``mode="poll"`` to
        ``run()`` instead.
        """
        if not _WATCHFILES_AVAILABLE:
            logger.warning(
                "watchfiles is not installed; falling back to one-shot scan. "
                "Install with `pip install kb-mcp-lite[v0_8]` for event-driven mode."
            )
            self.scan_once(store=store)
            return

        self._running = True
        logger.info(
            "Starting event-driven watcher for vault %r on %s (debounce=%dms)",
            self.vault_name,
            self.watch_dir,
            debounce_ms,
        )

        owns_store = store is None
        if owns_store:
            store = SqliteStore(self.db_path)
        assert store is not None  # narrowed for mypy

        try:
            # Prime the in-memory mtime cache so the initial scan_once
            # does not re-report every file as "created".
            if self._file_mtimes:
                for p in list(self._file_mtimes):
                    try:
                        self._file_mtimes[p] = p.stat().st_mtime
                    except OSError:
                        self._file_mtimes.pop(p, None)

            # Initial pass: catch up on any changes that happened while we
            # were not running (file system events since last start).
            initial = self.scan_once(store=store)
            if any(len(v) for v in initial.values()):
                logger.info(
                    "Initial scan picked up: %d created, %d updated, %d deleted",
                    len(initial["created"]),
                    len(initial["updated"]),
                    len(initial["deleted"]),
                )

            asyncio.run(self._event_loop(debounce_ms, store))
        finally:
            self._running = False
            if owns_store and store:
                store.close()

    def run(
        self,
        interval_seconds: float = 1.0,
        max_iterations: int | None = None,
        mode: WatchMode = "auto",
        debounce_ms: int = 200,
    ) -> None:
        """Run the watcher loop continuously.

        ``mode`` controls how changes are detected:

        * ``"auto"`` (default) — pick ``"event"`` when watchfiles is installed
          and we are not inside a container, else ``"poll"``.
        * ``"event"`` — use watchfiles (raises if it is not installed).
        * ``"poll"`` — legacy os.walk loop with ``interval_seconds`` between
          full scans.
        """
        resolved = resolve_mode(mode)
        if resolved == "event":
            self.run_event_based(debounce_ms=debounce_ms)
            return

        self._running = True
        logger.info(
            "Starting poll-based watcher for vault %r on %s (interval=%.2fs)",
            self.vault_name,
            self.watch_dir,
            interval_seconds,
        )
        iterations = 0
        while self._running:
            try:
                res = self.scan_once()
                total_changes = len(res["created"]) + len(res["updated"]) + len(res["deleted"])
                if total_changes > 0:
                    logger.info(
                        "Watcher synced: %d created, %d updated, %d deleted",
                        len(res["created"]),
                        len(res["updated"]),
                        len(res["deleted"]),
                    )
            except Exception as e:
                logger.error("Watcher error during scan: %s", e)

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

            time.sleep(interval_seconds)

    def stop(self) -> None:
        """Signal the watcher loop to stop."""
        self._running = False
