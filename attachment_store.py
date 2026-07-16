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
"""

from __future__ import annotations

import os
import sqlite3
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

def _connect() -> sqlite3.Connection:
    """Open (and initialise if necessary) the SQLite database."""
    logger.debug(
        "[attachment_store] connecting to DB at path=%s", IMAGE_STORE_DB_PATH
    )
    conn = sqlite3.connect(IMAGE_STORE_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
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
    logger.debug("[attachment_store] DB initialised, tables and indexes ready")
    return conn


# Module-level connection (created lazily).
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        logger.debug("[attachment_store] lazy-init: opening DB connection")
        _conn = _connect()
        logger.debug("[attachment_store] DB connection established")
    else:
        logger.debug("[attachment_store] reusing existing DB connection")
    return _conn


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
        "[attachment_store] get_images: looking up token=%s now=%.0f db=%s",
        token_preview,
        now,
        IMAGE_STORE_DB_PATH,
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
