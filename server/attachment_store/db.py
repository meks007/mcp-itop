"""
attachment_store/db.py - Schema, vacuum, and startup init for the attachment store.

Owns the DDL for the two attachment-store tables (attachment_sessions and
inline_image_refs) and the incremental-vacuum background thread.

All connection and transaction management is delegated to the db/ layer.
Call attachment_store.init_db() once at server startup, after db.init_db()
has already run.

Callers (session.py, refs.py) obtain the backend via get_db() from db/ and
use the DbBackend interface directly -- no _get_conn() shim exists here.
"""

from __future__ import annotations

import os
import threading
import time
import logging
from pathlib import Path

import db as _db_layer
from config import INLINE_IMAGE_REF_TTL  # noqa: F401 -- re-exported for callers

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (re-exported so session.py / refs.py can import from one place)
# ---------------------------------------------------------------------------

IMAGE_STORE_TTL_SECONDS: float = float(
    os.getenv("IMAGE_STORE_TTL", "3600")
)

IMAGE_STORE_VACUUM_INTERVAL: float = float(
    os.getenv("IMAGE_STORE_VACUUM_INTERVAL", "3600")
)

# Path used only for size logging in vacuum output.
_DEFAULT_DB_PATH = Path(__file__).parent.parent / "attachment_store.db"
IMAGE_STORE_DB_PATH: str = os.getenv(
    "IMAGE_STORE_DB", str(_DEFAULT_DB_PATH)
)

# Startup guard.
_initialised: bool = False

# Background vacuum timer thread.
_vacuum_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL_ATTACHMENT_SESSIONS = """
CREATE TABLE IF NOT EXISTS attachment_sessions (
    token       TEXT NOT NULL,
    uri         TEXT NOT NULL,
    content     BLOB,
    mimetype    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    expires_at  REAL NOT NULL
)
"""

_DDL_INLINE_IMAGE_REFS = """
CREATE TABLE IF NOT EXISTS inline_image_refs (
    obj_class   TEXT NOT NULL,
    obj_id      TEXT NOT NULL,
    img_id      TEXT NOT NULL,
    img_secret  TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (obj_class, obj_id, img_id)
)
"""

_DDL_IDX_TOKEN = (
    "CREATE INDEX IF NOT EXISTS idx_token "
    "ON attachment_sessions (token)"
)
_DDL_IDX_EXPIRES = (
    "CREATE INDEX IF NOT EXISTS idx_expires_at "
    "ON attachment_sessions (expires_at)"
)
_DDL_IDX_IIR = (
    "CREATE INDEX IF NOT EXISTS idx_iir_lookup "
    "ON inline_image_refs (obj_class, obj_id, expires_at)"
)


# ---------------------------------------------------------------------------
# Schema migration helper
# ---------------------------------------------------------------------------

def _ensure_content_column() -> None:
    """Add the content column to attachment_sessions if missing (pre-migration DBs)."""
    rows = _db_layer.get_db().execute(
        "PRAGMA table_info(attachment_sessions)"
    )
    existing_cols = {row[1] for row in rows}
    if "content" not in existing_cols:
        logger.info(
            "[attachment_store] migrating schema: adding content column"
        )
        _db_layer.get_db().execute(
            "ALTER TABLE attachment_sessions ADD COLUMN content BLOB"
        )


# ---------------------------------------------------------------------------
# Vacuum helpers
# ---------------------------------------------------------------------------

def _run_incremental_vacuum() -> None:
    """Run PRAGMA incremental_vacuum on the active backend.

    Delegates to SqliteDbBackend.incremental_vacuum() when available so
    the size-logging logic lives in one place. Falls back to a raw
    execute() call for other backends that understand the pragma.
    """
    backend = _db_layer.get_db()
    if hasattr(backend, "incremental_vacuum"):
        backend.incremental_vacuum()
    else:
        try:
            backend.execute("PRAGMA incremental_vacuum")
            logger.debug("[attachment_store] incremental_vacuum done")
        except Exception as exc:
            logger.warning("[attachment_store] incremental_vacuum failed: %s", exc)


def _vacuum_loop(interval: float) -> None:
    """Background thread body: sleep interval seconds, then vacuum, repeat."""
    logger.debug(
        "[attachment_store] vacuum_loop: started, interval=%.0fs", interval
    )
    while True:
        time.sleep(interval)
        logger.debug(
            "[attachment_store] vacuum_loop: running scheduled incremental_vacuum"
        )
        _run_incremental_vacuum()


# ---------------------------------------------------------------------------
# Startup initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Register schema and start the vacuum timer.

    Must be called once at server startup, after db.init_db() has run.
    Safe to call multiple times (subsequent calls are no-ops).
    """
    global _initialised, _vacuum_thread
    if _initialised:
        logger.debug("[attachment_store] init_db: already initialised, skipping")
        return

    backend = _db_layer.get_db()
    backend.execute(_DDL_ATTACHMENT_SESSIONS)
    backend.execute(_DDL_INLINE_IMAGE_REFS)
    _ensure_content_column()
    backend.execute(_DDL_IDX_TOKEN)
    backend.execute(_DDL_IDX_EXPIRES)
    backend.execute(_DDL_IDX_IIR)

    logger.info(
        "[attachment_store] init_db: schema ready, db=%s", IMAGE_STORE_DB_PATH
    )

    _run_incremental_vacuum()
    _initialised = True

    if IMAGE_STORE_VACUUM_INTERVAL > 0:
        _vacuum_thread = threading.Thread(
            target=_vacuum_loop,
            args=(IMAGE_STORE_VACUUM_INTERVAL,),
            daemon=True,
            name="attachment-store-vacuum",
        )
        _vacuum_thread.start()
        logger.info(
            "[attachment_store] init_db: vacuum timer started,"
            " interval=%.0fs", IMAGE_STORE_VACUUM_INTERVAL,
        )
    else:
        logger.info(
            "[attachment_store] init_db: vacuum timer disabled"
            " (IMAGE_STORE_VACUUM_INTERVAL=0)"
        )
