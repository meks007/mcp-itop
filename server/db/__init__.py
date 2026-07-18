"""
db/__init__.py - Database backend selection and global accessor.

Usage
-----
At server startup (server.py):
    from db import init_db
    init_db()

In domain modules (e.g. attachment_store/db.py):
    from db import get_db
    rows = get_db().execute("SELECT ...", params)

To add a new backend:
    1. Implement DbBackend in a new file under server/db/.
    2. Add its name to the _BACKENDS dict below.
    3. Set DB_BACKEND=<name> in the environment.
    No changes to any domain module are required.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

from db.base import DbBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

def _build_backends() -> dict:
    from db.sqlite import SqliteDbBackend  # local import avoids circular load order
    return {"sqlite": SqliteDbBackend}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_db: DbBackend | None = None


def init_db() -> None:
    """Open the configured database backend. Called once at server startup.

    Reads DB_BACKEND (default: sqlite) and IMAGE_STORE_DB (default: next to
    the server package) from the environment.  Safe to call multiple times;
    subsequent calls after the first are no-ops.
    """
    global _db
    if _db is not None:
        logger.debug("[db] init_db: already initialised, skipping")
        return

    backend_name = os.getenv("DB_BACKEND", "sqlite").lower()
    backends = _build_backends()
    backend_cls = backends.get(backend_name)
    if backend_cls is None:
        raise ValueError(
            "Unknown DB_BACKEND %r. Available: %s"
            % (backend_name, ", ".join(sorted(backends)))
        )

    if backend_name == "sqlite":
        default_path = Path(__file__).parent.parent / "attachment_store.db"
        db_path = os.getenv("IMAGE_STORE_DB", str(default_path))
        _db = backend_cls(db_path)
    else:
        _db = backend_cls()

    _db.connect()
    logger.info("[db] init_db: backend=%s ready", backend_name)


def get_db() -> DbBackend:
    """Return the active database backend.

    Raises RuntimeError when init_db() has not been called yet.
    """
    if _db is None:
        raise RuntimeError(
            "Database not initialised. Call db.init_db() at server startup."
        )
    return _db
