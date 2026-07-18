"""
db/__init__.py - Database backend accessor.

Usage
-----
In server.py (once at startup, before domain modules run):
    from db import set_db
    from db.sqlite import SqliteDbBackend
    set_db(SqliteDbBackend(db_path))

In domain modules (attachment_store/db.py, session.py, refs.py):
    from db import get_db
    rows = get_db().execute("SELECT ...", params)

To add a new backend:
    1. Implement DbBackend in a new file under server/db/.
    2. Instantiate it in server.py and pass it to set_db().
    No changes to any domain module are required.
"""

from __future__ import annotations

import logging

from db.base import DbBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-global backend singleton -- set once by server.py at startup
# ---------------------------------------------------------------------------

_db: DbBackend | None = None


def set_db(backend: DbBackend) -> None:
    """Bind the process-global database backend.

    Must be called once at server startup, before any domain module that
    uses get_db() runs. Safe to call again with the same instance (no-op);
    raises RuntimeError when called with a different instance after the
    first call to prevent accidental replacement at runtime.
    """
    global _db
    if _db is not None:
        if _db is backend:
            logger.debug("[db] set_db: same backend instance, skipping")
            return
        raise RuntimeError(
            "[db] set_db: backend already set -- cannot replace at runtime"
        )
    _db = backend
    logger.info("[db] set_db: backend=%s registered", type(backend).__name__)


def get_db() -> DbBackend:
    """Return the active database backend.

    Raises RuntimeError when set_db() has not been called yet.
    """
    if _db is None:
        raise RuntimeError(
            "Database backend not set. "
            "Call db.set_db(backend) in server.py at startup."
        )
    return _db
