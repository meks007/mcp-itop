"""
attachment_store.py - SQLite-backed store for itop image URIs.

Stores image metadata (URI, mimetype, filename) keyed by the bearer token
in cleartext. Used by the static MCP resource handler itop://attachment/images
to retrieve the image set produced by the most recent itop_get_ticket_images
tool call for the current client session.

Schema
------
TABLE attachment_sessions (
    token       TEXT NOT NULL,
    uri         TEXT NOT NULL,
    mimetype    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    expires_at  REAL NOT NULL   -- Unix timestamp (UTC)
)

TTL is fixed at IMAGE_STORE_TTL_SECONDS (default 3600 s = 1 h).
Expired rows are purged automatically on every write.

Vacuum
------
PRAGMA auto_vacuum = INCREMENTAL is set on db open so SQLite tracks free
pages automatically. A background daemon thread runs PRAGMA incremental_vacuum
every IMAGE_STORE_VACUUM_INTERVAL seconds (env, default 3600 s). Set the env
var to 0 to disable the timer entirely. A single incremental_vacuum is also
run immediately after _open_db() completes to reclaim any leftover free pages
from a previous process.

The database connection is opened eagerly at server startup via init_db().
Call init_db() once from server.py before the ASGI app starts serving
requests. All subsequent calls to store_images / get_images reuse the
module-level connection without any lazy-init overhead.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TypedDict

from config import logger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IMAGE_STORE_TTL_SECONDS: float = float(
    os.getenv("IMAGE_STORE_TTL", "3600")
)

IMAGE_STORE_VACUUM_INTERVAL: float = float(
    os.getenv("IMAGE_STORE_VACUUM_INTERVAL", "3600")
)

# DB file lives next to this module unless overridden by env var.
_DEFAULT_DB_PATH = Path(__file__).parent / "attachment_store.db"
IMAGE_STORE_DB_PATH: str = os.getenv(
    "IMAGE_STORE_DB", str(_DEFAULT_DB_PATH)
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class ImageEntry(TypedDict):
    uri: str
    mimetype: str
    filename: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Module-level connection. Set by init_db(); never None after startup.
_conn: sqlite3.Connection | None = None

# Background vacuum timer thread. Kept to avoid garbage collection.
_vacuum_thread: threading.Thread | None = None


def _run_incremental_vacuum() -> None:
    """Execute PRAGMA incremental_vacuum on the module-level connection.

    Reclaims free pages that auto_vacuum has already identified. This is
    lightweight and does not rebuild the database file.
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
    # Enable incremental auto_vacuum so SQLite tracks free pages for later
    # reclamation via PRAGMA incremental_vacuum. Must be set before any tables
    # exist on a new DB; on an existing DB it is a no-op if the mode was
    # already set, or ignored if the DB was created without it.
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachment_sessions (
            token       TEXT NOT NULL,
            uri         TEXT NOT NULL,
            mimetype    TEXT NOT NULL,
            filename    TEXT NOT NULL,
            expires_at  REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token "
        "ON attachment_sessions (token)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expires_at "
        "ON attachment_sessions (expires_at)"
    )
    conn.commit()
    logger.debug("[attachment_store] DB ready, tables and indexes verified")

    # Reclaim any free pages left by a previous process run.
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

    Starts a background daemon thread that runs PRAGMA incremental_vacuum
    every IMAGE_STORE_VACUUM_INTERVAL seconds. Set IMAGE_STORE_VACUUM_INTERVAL
    to 0 to disable the timer.
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_images(token: str, images: list[ImageEntry]) -> None:
    """Persist image metadata for the given bearer token.

    Replaces any existing entries for this token and purges all expired
    rows from the table. Each entry is valid for IMAGE_STORE_TTL_SECONDS.

    Args:
        token:  The raw bearer token for the current MCP client session.
        images: List of dicts with keys: uri, mimetype, filename.
                Extra keys (e.g. source) are silently ignored.
    """
    token_preview = token[:8] + "..." if len(token) > 8 else token
    expires_at = time.time() + IMAGE_STORE_TTL_SECONDS

    logger.debug(
        "[attachment_store] store_images: token=%s image_count=%d "
        "ttl=%.0fs expires_at=%.0f db=%s",
        token_preview,
        len(images),
        IMAGE_STORE_TTL_SECONDS,
        expires_at,
        IMAGE_STORE_DB_PATH,
    )

    conn = _get_conn()

    with conn:
        # Remove stale entries for this token.
        deleted_token = conn.execute(
            "DELETE FROM attachment_sessions WHERE token = ?",
            (token,),
        ).rowcount
        logger.debug(
            "[attachment_store] store_images: deleted %d old row(s) for token=%s",
            deleted_token,
            token_preview,
        )

        # Purge all globally expired rows.
        deleted_expired = conn.execute(
            "DELETE FROM attachment_sessions WHERE expires_at < ?",
            (time.time(),),
        ).rowcount
        logger.debug(
            "[attachment_store] store_images: purged %d globally expired row(s)",
            deleted_expired,
        )

        # Insert new entries.
        rows = [
            (
                token,
                img["uri"],
                img.get("mimetype", "application/octet-stream"),
                img.get("filename", "attachment"),
                expires_at,
            )
            for img in images
        ]
        conn.executemany(
            "INSERT INTO attachment_sessions "
            "(token, uri, mimetype, filename, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        logger.debug(
            "[attachment_store] store_images: inserted %d new row(s) for token=%s",
            len(rows),
            token_preview,
        )

    for i, img in enumerate(images):
        logger.debug(
            "[attachment_store] store_images: [%d] uri=%s mimetype=%s filename=%s",
            i,
            img.get("uri", ""),
            img.get("mimetype", ""),
            img.get("filename", ""),
        )

    logger.debug(
        "[attachment_store] store_images: done, token=%s total_stored=%d",
        token_preview,
        len(images),
    )


def get_images(token: str) -> list[ImageEntry]:
    """Return all non-expired image entries for the given bearer token.

    Returns an empty list when no valid entries exist (not yet stored,
    expired, or wrong token).

    Args:
        token: The raw bearer token for the current MCP client session.
    """
    token_preview = token[:8] + "..." if len(token) > 8 else token
    now = time.time()

    logger.debug(
        "[attachment_store] get_images: looking up token=%s", token_preview
    )

    conn = _get_conn()
    cursor = conn.execute(
        "SELECT uri, mimetype, filename, expires_at FROM attachment_sessions "
        "WHERE token = ? AND expires_at >= ? "
        "ORDER BY rowid",
        (token, now),
    )
    rows = cursor.fetchall()

    logger.debug(
        "[attachment_store] get_images: raw query returned %d row(s) for token=%s",
        len(rows),
        token_preview,
    )

    entries: list[ImageEntry] = []
    for i, row in enumerate(rows):
        entry: ImageEntry = {
            "uri": row[0],
            "mimetype": row[1],
            "filename": row[2],
        }
        remaining_ttl = row[3] - now
        logger.debug(
            "[attachment_store] get_images: [%d] uri=%s mimetype=%s filename=%s "
            "remaining_ttl=%.0fs",
            i,
            entry["uri"],
            entry["mimetype"],
            entry["filename"],
            remaining_ttl,
        )
        entries.append(entry)

    logger.debug(
        "[attachment_store] get_images: returning %d valid entry/entries for token=%s",
        len(entries),
        token_preview,
    )
    return entries


def purge_expired() -> int:
    """Delete all expired rows from the store. Returns the number of rows removed."""
    logger.debug("[attachment_store] purge_expired: running manual purge")
    conn = _get_conn()
    with conn:
        cursor = conn.execute(
            "DELETE FROM attachment_sessions WHERE expires_at < ?",
            (time.time(),),
        )
    removed = cursor.rowcount
    logger.debug("[attachment_store] purge_expired: removed %d row(s)", removed)
    return removed
