"""
attachment_store/session.py - Session-bound image storage.

Stores and retrieves image entries keyed by the current client bearer token.
Each entry is valid for IMAGE_STORE_TTL (from config.py).

Schema registered at module import time via db.register_schema() so that
db.init() creates the table without any explicit init_db() call from callers.
"""

from __future__ import annotations

import time
import logging
from typing import TypedDict

import db
from config import IMAGE_STORE_TTL
from attachment_store.image import _normalize_image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema registration (runs at import time, before db.init())
# ---------------------------------------------------------------------------

db.register_schema("""
CREATE TABLE IF NOT EXISTS attachment_sessions (
    token       TEXT NOT NULL,
    uri         TEXT NOT NULL,
    content     BLOB,
    mimetype    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    expires_at  REAL NOT NULL
)
""")

db.register_schema(
    "CREATE INDEX IF NOT EXISTS idx_as_token "
    "ON attachment_sessions (token)"
)

db.register_schema(
    "CREATE INDEX IF NOT EXISTS idx_as_expires "
    "ON attachment_sessions (expires_at)"
)


def _migrate_content_column(backend) -> None:
    """Add the content column to attachment_sessions if missing.

    Needed for databases created before the content column was introduced.
    Cannot be expressed as a plain DDL string because it requires a
    PRAGMA table_info() check first.
    """
    rows = backend.execute("PRAGMA table_info(attachment_sessions)")
    existing_cols = {row[1] for row in rows}
    if "content" not in existing_cols:
        logger.info(
            "[attachment_store] migrating schema: adding content column"
        )
        backend.execute(
            "ALTER TABLE attachment_sessions ADD COLUMN content BLOB"
        )


db.register_migration(_migrate_content_column)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ImageEntry(TypedDict):
    uri: str        # short itop:// reference
    content: bytes  # raw JPEG bytes (always populated before store)
    mimetype: str   # always image/jpeg after normalization
    filename: str


def store_images(token: str, images: list[ImageEntry]) -> None:
    """Persist image entries for the given bearer token.

    Each entry with non-None content is normalized to JPEG via
    _normalize_image() before insertion. Entries without content are
    stored as-is (should not occur with eager download).

    Replaces any existing entries for this token. Expired rows are purged
    by the central housekeeping task. Each entry is valid for
    IMAGE_STORE_TTL.

    Args:
        token:  The raw bearer token for the current MCP client session.
        images: List of dicts with keys: uri, content, mimetype, filename.
                uri must be a short itop:// reference (no data: URIs).
                content must be raw image bytes (all sources pre-downloaded).
                Extra keys (e.g. source) are silently ignored.
    """
    token_preview = token[:8] + "..." if len(token) > 8 else token
    expires_at = time.time() + IMAGE_STORE_TTL

    logger.debug(
        "[attachment_store] store_images: token=%s image_count=%d "
        "ttl=%.0fs expires_at=%.0f",
        token_preview,
        len(images),
        IMAGE_STORE_TTL,
        expires_at,
    )

    rows = []
    for img in images:
        raw: bytes | None = img.get("content")
        mimetype: str = img.get("mimetype", "application/octet-stream")
        filename: str = img.get("filename", "attachment")

        if raw is not None:
            raw, mimetype, filename = _normalize_image(raw, mimetype, filename)

        rows.append((
            token,
            img["uri"],
            raw,
            mimetype,
            filename,
            expires_at,
        ))

    with db.transaction():
        db.execute(
            "DELETE FROM attachment_sessions WHERE token = ?",
            (token,),
        )
        logger.debug(
            "[attachment_store] store_images: deleted old rows for token=%s",
            token_preview,
        )

        db.executemany(
            "INSERT INTO attachment_sessions "
            "(token, uri, content, mimetype, filename, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        logger.debug(
            "[attachment_store] store_images: inserted %d new row(s) for token=%s",
            len(rows),
            token_preview,
        )

    for i, img in enumerate(images):
        content = img.get("content")
        logger.debug(
            "[attachment_store] store_images: [%d] uri=%s mimetype=%s filename=%s content=%s",
            i,
            img.get("uri", ""),
            img.get("mimetype", ""),
            img.get("filename", ""),
            ("%d bytes" % len(content)) if content is not None else "None",
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

    rows = db.execute(
        "SELECT uri, content, mimetype, filename, expires_at "
        "FROM attachment_sessions "
        "WHERE token = ? AND expires_at >= ? "
        "ORDER BY rowid",
        (token, now),
    )

    logger.debug(
        "[attachment_store] get_images: raw query returned %d row(s) for token=%s",
        len(rows),
        token_preview,
    )

    entries: list[ImageEntry] = []
    for i, row in enumerate(rows):
        content: bytes | None = row[1]
        entry: ImageEntry = {
            "uri": row[0],
            "content": content,
            "mimetype": row[2],
            "filename": row[3],
        }
        remaining_ttl = row[4] - now
        logger.debug(
            "[attachment_store] get_images: [%d] uri=%s mimetype=%s filename=%s"
            " content=%s remaining_ttl=%.0fs",
            i,
            entry["uri"],
            entry["mimetype"],
            entry["filename"],
            ("%d bytes" % len(content)) if content is not None else "None",
            remaining_ttl,
        )
        entries.append(entry)

    logger.debug(
        "[attachment_store] get_images: returning %d valid entry/entries for token=%s",
        len(entries),
        token_preview,
    )
    return entries


def purge_expired_images() -> int:
    """Delete all expired rows from attachment_sessions. Returns rows removed."""
    logger.debug("[attachment_store] purge_expired_images: running purge")
    now = time.time()

    count_rows = db.execute(
        "SELECT COUNT(*) FROM attachment_sessions WHERE expires_at < ?",
        (now,),
    )
    removed = count_rows[0][0] if count_rows else 0

    with db.transaction():
        db.execute(
            "DELETE FROM attachment_sessions WHERE expires_at < ?",
            (now,),
        )

    logger.debug(
        "[attachment_store] purge_expired_images: removed %d row(s)", removed
    )
    return removed
