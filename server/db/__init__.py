"""
db/__init__.py - Backend-agnostic database layer.

Usage in domain modules
-----------------------
    import db

    db.register_schema(\"\"\"
    CREATE TABLE IF NOT EXISTS my_table (
        id INTEGER PRIMARY KEY,
        value TEXT NOT NULL
    )
    \"\"\")

    # At runtime (after db.init() has been called by server.py):
    rows = db.execute("SELECT * FROM my_table WHERE id = ?", (42,))
    with db.transaction():
        db.executemany("INSERT INTO my_table (value) VALUES (?)", rows)

Usage in server.py (once at startup)
-------------------------------------
    import db
    db.init()   # connect backend, run all registered DDL + migrations

Backend selection
-----------------
Set the DB_BACKEND env var (default: "sqlite"). db/__init__.py imports
"db.<backend>" and instantiates its Backend class. If the module does not
exist the layer falls back to db.sqlite. Any new backend only needs a single
file in server/db/ -- no other change required.

Adding a new backend
---------------------
    1. Create server/db/mssql.py (or any name) with class Backend(DbBackend).
    2. Backend.__init__ reads its own env vars (e.g. MSSQL_HOST, ...).
    3. Backend.connect() opens the connection.
    4. Implement execute(), executemany(), transaction(), close().
    5. Set DB_BACKEND=mssql in the environment.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Callable

from db.base import DbBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend selection -- instance created at import time, not yet connected
# ---------------------------------------------------------------------------

_backend_name: str = os.getenv("DB_BACKEND", "sqlite").lower()

try:
    _mod = importlib.import_module("db." + _backend_name)
    logger.debug("[db] loaded backend module: db.%s", _backend_name)
except ModuleNotFoundError:
    logger.warning(
        "[db] backend 'db.%s' not found, falling back to db.sqlite",
        _backend_name,
    )
    import db.sqlite as _mod  # type: ignore[no-redef]

_instance: DbBackend = _mod.Backend()

# ---------------------------------------------------------------------------
# Schema + migration registration
# ---------------------------------------------------------------------------

_schemas: list[str] = []
_migrations: list[Callable[[DbBackend], None]] = []


def register_schema(ddl: str) -> None:
    """Register a DDL string to be executed during db.init().

    Call this at module level in any domain module that owns a table.
    Registration order is preserved; db.init() runs them in that order.
    All DDL is run before any migration callables.
    """
    _schemas.append(ddl)


def register_migration(fn: Callable[[DbBackend], None]) -> None:
    """Register a migration callable to be executed during db.init().

    Use this for schema changes that cannot be expressed as a plain DDL
    string (e.g. ALTER TABLE with a prior PRAGMA table_info() check).
    Migrations run after all registered DDL strings, in registration order.

    The callable receives the active DbBackend instance as its only argument.
    """
    _migrations.append(fn)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

_initialised: bool = False


def init() -> None:
    """Connect the backend and run all registered DDL + migrations.

    Called once from server.py _serve(). Safe to call multiple times
    (subsequent calls are no-ops).

    By the time this is called, all tool modules have already been imported
    (server.py imports them at module level), so all register_schema() and
    register_migration() calls have already happened.
    """
    global _initialised
    if _initialised:
        logger.debug("[db] init: already initialised, skipping")
        return

    _instance.connect()

    for ddl in _schemas:
        _instance.execute(ddl)

    for fn in _migrations:
        fn(_instance)

    _initialised = True
    logger.info(
        "[db] init: backend=%s ready, ran %d schema block(s), %d migration(s)",
        type(_instance).__module__,
        len(_schemas),
        len(_migrations),
    )


# ---------------------------------------------------------------------------
# Proxy surface -- DbBackend interface callable as db.execute(...)
# ---------------------------------------------------------------------------

def execute(sql: str, params: tuple = ()) -> list[tuple]:
    """Execute one SQL statement and return all rows as a list of tuples."""
    return _instance.execute(sql, params)


def executemany(sql: str, rows: list[tuple]) -> None:
    """Execute one SQL statement once per row."""
    _instance.executemany(sql, rows)


def transaction():
    """Return a context manager that commits on success, rolls back on error."""
    return _instance.transaction()
