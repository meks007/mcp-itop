"""
attachment_store/db.py - SQLite connection, schema, and vacuum for the
attachment store.

Manages a single module-level connection opened at server startup via
init_db(). All other submodules obtain the connection via _get_conn().
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import logging
from pathlib import Path

from config import INLINE_IMAGE_REF_TTL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IMAGE_STORE_TTL_SECONDS: float = float(
    os.getenv("IMAGE_STORE_TTL", "3600")
)

IMAGE_STORE_VACUUM_INTERVAL: float = float(
    os.getenv("IMAGE_STORE_VACUUM_INTERVAL", "3600")
)

# DB file lives next to the package root unless overridden by env var.
_DEFAULT_DB_PATH = Path(__file__).parent.parent / "attachment_store.db"
IMAGE_STORE_DB_PATH: str = os.getenv(
    "IMAGE_STORE_DB", str(_DEFAULT_DB_PATH)
)

# Module-level connection. Set by init_db(); never None after startup.
_conn: sqlite3.Connection | None = None

# Background vacuum timer thread. Kept to avoid garbage collection.
_vacuum_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_incremental_vacuum() -> None:
    """Execute PRAGMA incremental_vacuum on the module-level connection.

    Reclaims free pages that auto_vacuum has already identified. Lightweight
    and does not rebuild the database file.
    Called from the background timer thread and once at startup.
    """
    conn = _get_conn()
    try:
        conn.execute("PRAGMA incremental_vacuum")
        db_size = Path(IMAGE_STORE_DB_PATH).stat().st_size
        logger.debug(
            "[attachment_store] incremental_vacuum done, db_size=%d bytes", db_size
        )
    except Exception as exc:
        logger.warning("[attachment_store] incremental_vacuum failed: %s", exc)


def _vacuum_loop(interval: float) -> None:
    """Background thread body: sleep interval seconds, then vacuum, repeat."""
    logger.debug(
        "[attachment_store] vacuum_loop: started, interval=%.0fs", interval
    )
    while True:
        time.sleep(interval)
        logger.debug("[attachment_store] vacuum_loop: running scheduled incremental_vacuum")
        _run_incremental_vacuum()


def _open_db() -> sqlite3.Connection:
    """Open and initialise the SQLite database. Called once at startup."""
    logger.debug(
        "[attachment_store] opening DB at path=%s", IMAGE_STORE_DB_PATH
    )
    conn = sqlite3.connect(IMAGE_STORE_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachment_sessions (
            token       TEXT NOT NULL,
            uri         TEXT NOT NULL,
            content     BLOB,
            mimetype    TEXT NOT NULL,
            filename    TEXT NOT NULL,
            expires_at  REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inline_image_refs (
            obj_class   TEXT NOT NULL,
            obj_id      TEXT NOT NULL,
            img_id      TEXT NOT NULL,
            img_secret  TEXT NOT NULL,
            expires_at  REAL NOT NULL,
            PRIMARY KEY (obj_class, obj_id, img_id)
        )
        """
    )
    # Migrate existing DBs that pre-date the content column.
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(attachment_sessions)")
    }
    if "content" not in existing_cols:
        logger.info(
            "[attachment_store] migrating schema: adding content column"
        )
        conn.execute(
            "ALTER TABLE attachment_sessions ADD COLUMN content BLOB"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token "
        "ON attachment_sessions (token)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expires_at "
        "ON attachment_sessions (expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_iir_lookup "
        "ON inline_image_refs (obj_class, obj_id, expires_at)"
    )
    conn.commit()
    logger.debug("[attachment_store] DB ready, tables and indexes verified")

    try:
        conn.execute("PRAGMA incremental_vacuum")
        db_size = Path(IMAGE_STORE_DB_PATH).stat().st_size
        logger.debug(
            "[attachment_store] startup incremental_vacuum done, db_size=%d bytes",
            db_size,
        )
    except Exception as exc:
        logger.warning(
            "[attachment_store] startup incremental_vacuum failed: %s", exc
        )

    return conn


def _get_conn() -> sqlite3.Connection:
    """Return the module-level DB connection. Raises if init_db() was not called."""
    if _conn is None:
        raise RuntimeError(
            "[attachment_store] DB not initialised. "
            "Call attachment_store.init_db() at server startup."
        )
    return _conn


# ---------------------------------------------------------------------------
# Startup initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Open the SQLite database, prepare the schema, and start the vacuum timer.

    Must be called once at server startup, before any store_images or
    get_images call. Safe to call multiple times (subsequent calls are
    no-ops).
    """
    global _conn, _vacuum_thread
    if _conn is not None:
        logger.debug("[attachment_store] init_db: already initialised, skipping")
        return
    _conn = _open_db()
    logger.info(
        "[attachment_store] init_db: DB opened at %s", IMAGE_STORE_DB_PATH
    )

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
