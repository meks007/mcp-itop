"""
attachment_store.py - SQLite-backed store for itop image URIs.

Stores image metadata (URI, mimetype, filename) keyed by a SHA-256 hash of
the bearer token. Used by the static MCP resource handler itop://attachment/images
to retrieve the image set produced by the most recent itop_get_ticket_images
tool call for the current client session.

Schema
------
TABLE attachment_sessions (
    token_hash  TEXT NOT NULL,
    uri         TEXT NOT NULL,
    mimetype    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    expires_at  REAL NOT NULL   -- Unix timestamp (UTC)
)

TTL is fixed at IMAGE_STORE_TTL_SECONDS (default 3600 s = 1 h).
Expired rows are purged automatically on every write.
"""

from __future__ import annotations

import hashlib
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

def _hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    """Open (and initialise if necessary) the SQLite database."""
    conn = sqlite3.connect(IMAGE_STORE_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachment_sessions (
            token_hash  TEXT NOT NULL,
            uri         TEXT NOT NULL,
            mimetype    TEXT NOT NULL,
            filename    TEXT NOT NULL,
            expires_at  REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_hash "
        "ON attachment_sessions (token_hash)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expires_at "
        "ON attachment_sessions (expires_at)"
    )
    conn.commit()
    return conn


# Module-level connection (created lazily).
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        logger.debug(
            "[attachment_store] opened DB at %s", IMAGE_STORE_DB_PATH
        )
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
    token_hash = _hash_token(token)
    expires_at = time.time() + IMAGE_STORE_TTL_SECONDS
    conn = _get_conn()

    with conn:
        # Remove stale entries for this token and all globally expired rows.
        conn.execute(
            "DELETE FROM attachment_sessions WHERE token_hash = ?",
            (token_hash,),
        )
        conn.execute(
            "DELETE FROM attachment_sessions WHERE expires_at < ?",
            (time.time(),),
        )
        # Insert new entries.
        conn.executemany(
            "INSERT INTO attachment_sessions "
            "(token_hash, uri, mimetype, filename, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    token_hash,
                    img["uri"],
                    img.get("mimetype", "application/octet-stream"),
                    img.get("filename", "attachment"),
                    expires_at,
                )
                for img in images
            ],
        )

    logger.debug(
        "[attachment_store] stored %d image(s) for token_hash=%s expires_at=%.0f",
        len(images),
        token_hash[:8] + "...",
        expires_at,
    )


def get_images(token: str) -> list[ImageEntry]:
    """Return all non-expired image entries for the given bearer token.

    Returns an empty list when no valid entries exist (not yet stored,
    expired, or wrong token).

    Args:
        token: The raw bearer token for the current MCP client session.
    """
    token_hash = _hash_token(token)
    conn = _get_conn()
    cursor = conn.execute(
        "SELECT uri, mimetype, filename FROM attachment_sessions "
        "WHERE token_hash = ? AND expires_at >= ? "
        "ORDER BY rowid",
        (token_hash, time.time()),
    )
    rows = cursor.fetchall()
    entries: list[ImageEntry] = [
        {"uri": row[0], "mimetype": row[1], "filename": row[2]}
        for row in rows
    ]
    logger.debug(
        "[attachment_store] get_images token_hash=%s -> %d entry/entries",
        token_hash[:8] + "...",
        len(entries),
    )
    return entries


def purge_expired() -> int:
    """Delete all expired rows from the store. Returns the number of rows removed."""
    conn = _get_conn()
    with conn:
        cursor = conn.execute(
            "DELETE FROM attachment_sessions WHERE expires_at < ?",
            (time.time(),),
        )
    removed = cursor.rowcount
    logger.debug("[attachment_store] purge_expired removed %d row(s)", removed)
    return removed
