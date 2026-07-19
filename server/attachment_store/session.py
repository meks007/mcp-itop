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
    expires_at  REAL NOT NULL,
    served      INTEGER NOT NULL DEFAULT 0
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


def _migrate_served_column(backend) -> None:
    """Add the served column to attachment_sessions if missing.

    Needed for databases created before the served column was introduced.
    Existing rows default to served=0 so they are served on next resource call.
    Cache housekeeping (purge_expired_images) is not affected: it evicts by
    expires_at only and never reads served.
    """
    rows = backend.execute("PRAGMA table_info(attachment_sessions)")
    existing_cols = {row[1] for row in rows}
    if "served" not in existing_cols:
        logger.info(
            "[attachment_store] migrating schema: adding served column"
        )
        backend.execute(
            "ALTER TABLE attachment_sessions ADD COLUMN served INTEGER NOT NULL DEFAULT 0"
        )


db.register_migration(_migrate_content_column)
db.register_migration(_migrate_served_column)

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
    IMAGE_STORE_TTL. The served flag is reset to 0 implicitly because all
    old rows are deleted and new rows default to served=0.

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
            "(token, uri, content, mimetype, filename, expires_at, served) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
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


def get_next_image(token: str) -> ImageEntry | None:
    """Return the next unserved non-expired image entry for the given token.

    Selects the lowest-rowid row where token matches, served=0, and the
    entry has not expired. Marks that row served=1 atomically within the
    same transaction before returning. Returns None when no unserved entry
    exists (store empty, all served, or all expired).

    Cache housekeeping is not affected: purge_expired_images evicts by
    expires_at only and never reads the served flag.

    Args:
        token: The raw bearer token for the current MCP client session.
    """
    token_preview = token[:8] + "..." if len(token) > 8 else token
    now = time.time()

    logger.debug(
        "[attachment_store] get_next_image: looking up token=%s", token_preview
    )

    with db.transaction():
        rows = db.execute(
            "SELECT rowid, uri, content, mimetype, filename, expires_at "
            "FROM attachment_sessions "
            "WHERE token = ? AND served = 0 AND expires_at >= ? "
            "ORDER BY rowid "
            "LIMIT 1",
            (token, now),
        )

        if not rows:
            logger.debug(
                "[attachment_store] get_next_image: no unserved entry for token=%s",
                token_preview,
            )
            return None

        row = rows[0]
        rowid = row[0]
        content: bytes | None = row[2]
        remaining_ttl = row[5] - now

        db.execute(
            "UPDATE attachment_sessions SET served = 1 WHERE rowid = ?",
            (rowid,),
        )

    entry: ImageEntry = {
        "uri": row[1],
        "content": content,
        "mimetype": row[3],
        "filename": row[4],
    }

    logger.debug(
        "[attachment_store] get_next_image: serving rowid=%d uri=%s"
        " mimetype=%s filename=%s content=%s remaining_ttl=%.0fs",
        rowid,
        entry["uri"],
        entry["mimetype"],
        entry["filename"],
        ("%d bytes" % len(content)) if content is not None else "None",
        remaining_ttl,
    )

    return entry


def purge_expired_images() -> int:
    """Delete all expired rows from attachment_sessions. Returns rows removed.

    Evicts by expires_at only. The served flag has no effect on eviction.
    """
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
