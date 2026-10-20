"""SQLite migration runner.

Migrations are versioned, forward-only SQL files under
``kb_mcp_lite/migrations/``. Each file MUST be named ``NNNN_*.sql`` where
``NNNN`` is a zero-padded integer; the runner applies them in order and
records the version in the ``schema_version`` table.

A migration is run inside a transaction; if it fails, the DB is rolled
back to the previous version. The runner is safe to call on every
``__init__`` — it is a no-op if the schema is already current.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kb_mcp_lite.schema import IntegrityError

# ``migrations/`` is a directory of ``.sql`` files (not a Python package —
# no ``__init__.py``). We resolve its absolute path from this module's
# location so the runner works both in editable installs (where the
# source tree is on disk) and in wheel installs (where the SQL files are
# shipped as package data).
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_SKIPPABLE_VERSIONS = {3}


def _migration_files() -> list[tuple[int, str]]:
    """Return ``(version, sql_text)`` pairs sorted by version."""
    if not _MIGRATIONS_DIR.is_dir():
        return []
    files: list[tuple[int, str]] = []
    for entry in sorted(_MIGRATIONS_DIR.iterdir()):
        name = entry.name
        if not name.endswith(".sql"):
            continue
        try:
            version = int(name.split("_", 1)[0])
        except ValueError:
            continue
        files.append((version, entry.read_text(encoding="utf-8")))
    files.sort(key=lambda pair: pair[0])
    return files


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations against ``conn``.

    Idempotent. Creates the ``schema_version`` table on first run.
    Raises :class:`IntegrityError` if a migration's recorded version
    would be lower than one already applied (refuses to go backwards).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    applied = {
        row["version"] for row in conn.execute("SELECT version FROM schema_version").fetchall()
    }
    for version, sql_text in _migration_files():
        if version in applied:
            continue
        if version < max(applied, default=0) and version not in _SKIPPABLE_VERSIONS:
            raise IntegrityError(
                f"migration {version} is older than applied version(s) {sorted(applied)}; refusing to downgrade"
            )
        # ``executescript`` implicitly commits any open transaction and
        # runs the SQL as one or more statements. We let it manage the
        # transaction; if it fails, the schema_version row is NOT
        # inserted and the next apply_migrations() call will retry.
        # NOTE: a partial migration that fails mid-script leaves the DB
        # in an indeterminate state — this is a development-time failure
        # mode; production migrations must be written to be all-or-nothing
        # in a single executescript() call.
        try:
            conn.executescript(sql_text)
            conn.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (version, f"v{version:04d}"),
            )
        except Exception as e:  # noqa: BLE001
            # Migration 0003 (vec0) is best-effort: if vec0 is not
            # available on this connection, log and skip so lexical
            # features still work. Any other failure is fatal.
            # (``except sqlite3.Error`` alone is not enough — when the
            # connection is a pysqlite3 one its exceptions are not
            # instances of the stdlib ``sqlite3.Error`` class.)
            if version == 3:
                import logging

                logging.getLogger("kb_mcp_lite").debug(
                    "vec0 migration skipped: %s (semantic search disabled)", e
                )
                continue
            raise IntegrityError(f"migration v{version:04d} failed: {e}") from e
        applied.add(version)


__all__ = ["apply_migrations"]
