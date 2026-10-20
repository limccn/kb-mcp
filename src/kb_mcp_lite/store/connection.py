"""Public sqlite3 connection factory for kb-mcp.

This module hosts the helpers used by every sqlite3 connection the
project opens: the main store, the vec0 side connection, and the
background embedding worker. They were previously private functions
on :mod:`kb_mcp_lite.store.sqlite`; promoting them here lets the
worker and the embedding mixin open their own connections without
reaching into another module's underscore-prefixed API.

Why one factory
---------------
All connections to the same database MUST go through
:func:`make_sqlite_connection` so they share one SQLite library
(pysqlite3 or stdlib). Mixing the two against one WAL-mode database
corrupts the shared WAL index — observed as "database disk image is
malformed" when a stdlib connection closes while a pysqlite3
connection is still live.

This module is part of the ``store`` subpackage's internal API: it
is not re-exported from :mod:`kb_mcp_lite.store` because end users
should not open ad-hoc sqlite3 connections. Subpackage modules
(``worker.py``, ``embedding.py``, ``sqlite.py``) are the only
expected callers.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

__all__ = ["make_sqlite_connection", "sqlite_row_factory"]


def sqlite_row_factory(conn: sqlite3.Connection) -> Any:
    """Return the correct Row class for ``conn`` (stdlib sqlite3 vs pysqlite3).

    pysqlite3's cursor objects are not accepted by the stdlib
    :class:`sqlite3.Row` constructor, so each connection must use the
    Row class from the same library that created it.
    """
    try:
        import pysqlite3 as _psql

        if isinstance(conn, _psql.dbapi2.Connection):
            return _psql.dbapi2.Row
    except ImportError:
        pass
    return sqlite3.Row


def _try_load_vec0(conn: sqlite3.Connection) -> None:
    """Best-effort load of vec0 extension (module-private)."""
    try:
        import sqlite_vec

        sqlite_vec.load(conn)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("kb_mcp_lite").debug("vec0 not loaded: %s", e)


def make_sqlite_connection(
    db_path: str,
    *,
    isolation_level: str | None = None,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a sqlite3 connection that supports vec0 if possible.

    Args:
        db_path: Path to the SQLite database file (or ``":memory:"``).
        isolation_level: Forwarded to ``connect()``. Pass ``""`` for
            autocommit (the store's default).
        check_same_thread: Forwarded to ``connect()``. The worker uses
            ``False`` to share one connection between the main thread
            and the background embedding thread; SQLite's own WAL +
            busy_timeout guarantee serialisation, so the relaxed check
            is safe.
    """
    try:
        import pysqlite3 as _psql

        conn: sqlite3.Connection = _psql.connect(
            db_path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=isolation_level,
            # pysqlite3 accepts this kwarg; stdlib sqlite3.connect
            # also accepts it.
            check_same_thread=check_same_thread,
        )
        conn.enable_load_extension(True)
        _try_load_vec0(conn)
        return conn
    except ImportError:
        pass
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=isolation_level,  # type: ignore[arg-type]  # "" (legacy) is valid but missing from typeshed
        check_same_thread=check_same_thread,
    )
    try:
        conn.enable_load_extension(True)
        _try_load_vec0(conn)
    except Exception:  # noqa: BLE001
        pass
    return conn
