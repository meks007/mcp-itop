"""
attachment_store/metadata.py - Unified attachment metadata store.

Replaces session.py. Stores metadata (filename, MIME type, source, id, etc.)
for all attachment types keyed by bearer token + obj_class + obj_id.
Binary content is stored only for images (after normalization by attachment_sync.py).
Non-images are never cached here; they are downloaded live on resource call.

Schema registered at module import time via db.register_schema() so that
db.init() creates the table without any explicit init_db() call from callers.

The 'selected' column stores the prepared state: selected=1 means the agent
has explicitly requested this attachment via prepare_object_attachments().
get_object_attachments only serves rows with selected=1. Multiple rows can
be selected simultaneously (one per requested attachment id).
"""

from __future__ import annotations

import time
import logging

import db
from config import IMAGE_STORE_TTL

logger = logging.getLogger(__name__)

# Re-use IMAGE_STORE_TTL from config (env var IMAGE_STORE_TTL, default 3600 s).
# Alias used throughout this module for clarity.
ATTACHMENT_STORE_TTL: int = IMAGE_STORE_TTL

# ---------------------------------------------------------------------------
# Schema registration (runs at import time, before db.init())
# ---------------------------------------------------------------------------

db.register_schema("""
CREATE TABLE IF NOT EXISTS attachment_metadata (
    token         TEXT    NOT NULL,
    obj_class     TEXT    NOT NULL,
    obj_id        INTEGER NOT NULL,
    id            TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    filename      TEXT    NOT NULL,
    mimetype      TEXT    NOT NULL,
    inline_secret TEXT,
    content       BLOB,
    expires_at    REAL    NOT NULL,
    selected      INTEGER NOT NULL DEFAULT 0,
    served        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token, obj_class, obj_id, id)
)
""")

db.register_schema(
    "CREATE INDEX IF NOT EXISTS idx_am_token "
    "ON attachment_metadata (token)"
)

db.register_schema(
    "CREATE INDEX IF NOT EXISTS idx_am_expires "
    "ON attachment_metadata (expires_at)"
)


# ---------------------------------------------------------------------------
# Migration: drop legacy attachment_sessions table if present
# ---------------------------------------------------------------------------

def _migrate_from_sessions(backend) -> None:
    """Drop attachment_sessions table left over from session.py.

    attachment_metadata is created by the register_schema DDL above.
    """
    rows = backend.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='attachment_sessions'"
    )
    if rows:
        logger.info(
            "[attachment_store] migration: dropping legacy attachment_sessions table"
        )
        backend.execute("DROP TABLE attachment_sessions")


db.register_migration(_migrate_from_sessions)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_attachment_metadata(
    token: str,
    obj_class: str,
    obj_id: int,
    entries: list[dict],
) -> None:
    """Delete all records for (token, obj_class, obj_id) and insert new ones.

    Each entry dict must have: id, source, filename, mimetype.
    Optional: inline_secret (required when source='InlineImage').
    Sets served=0, selected=0, content=NULL for all new records.
    expires_at is set to time.time() + ATTACHMENT_STORE_TTL.

    Args:
        token:     Raw bearer token from get_bearer_token().
        obj_class: iTop class name, e.g. 'UserRequest', 'FAQ'.
        obj_id:    Confirmed numeric iTop database ID.
        entries:   List of dicts describing each attachment.
    """
    token_preview = token[:8] + "..." if len(token) > 8 else token
    expires_at = time.time() + ATTACHMENT_STORE_TTL

    logger.debug(
        "[attachment_store] store_attachment_metadata: token=%s cls=%s id=%d count=%d",
        token_preview, obj_class, obj_id, len(entries),
    )

    rows = [
        (
            token,
            obj_class,
            obj_id,
            e["id"],
            e["source"],
            e["filename"],
            e["mimetype"],
            e.get("inline_secret"),
            None,   # content - populated later by attachment_sync
            expires_at,
            0,      # selected
            0,      # served
        )
        for e in entries
    ]

    with db.transaction():
        db.execute(
            "DELETE FROM attachment_metadata "
            "WHERE token = ? AND obj_class = ? AND obj_id = ?",
            (token, obj_class, obj_id),
        )
        if rows:
            db.executemany(
                "INSERT INTO attachment_metadata "
                "(token, obj_class, obj_id, id, source, filename, mimetype, "
                " inline_secret, content, expires_at, selected, served) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    logger.debug(
        "[attachment_store] store_attachment_metadata: done token=%s cls=%s id=%d",
        token_preview, obj_class, obj_id,
    )


def _row_to_dict(row: tuple) -> dict:
    """Map a DB row tuple to a metadata dict.

    Column order: id, source, filename, mimetype, inline_secret,
                  content, selected, served.
    """
    return {
        "id":            row[0],
        "source":        row[1],
        "filename":      row[2],
        "mimetype":      row[3],
        "inline_secret": row[4],
        "content":       row[5],
        "selected":      row[6],
        "served":        row[7],
    }


_SELECT_COLS = (
    "id, source, filename, mimetype, inline_secret, content, selected, served"
)


def get_all_attachment_metadata(
    token: str,
    obj_class: str,
    obj_id: int,
) -> list[dict]:
    """Return all non-expired records for (token, obj_class, obj_id).

    Includes both served and unserved records.
    """
    now = time.time()
    rows = db.execute(
        "SELECT " + _SELECT_COLS + " FROM attachment_metadata "
        "WHERE token = ? AND obj_class = ? AND obj_id = ? AND expires_at >= ?",
        (token, obj_class, obj_id, now),
    )
    return [_row_to_dict(r) for r in rows]


def get_unserved_attachment_metadata(
    token: str,
    obj_class: str,
    obj_id: int,
) -> list[dict]:
    """Return all non-expired records with served=0 for (token, obj_class, obj_id)."""
    now = time.time()
    rows = db.execute(
        "SELECT " + _SELECT_COLS + " FROM attachment_metadata "
        "WHERE token = ? AND obj_class = ? AND obj_id = ? "
        "AND expires_at >= ? AND served = 0",
        (token, obj_class, obj_id, now),
    )
    return [_row_to_dict(r) for r in rows]


def get_single_attachment_metadata(
    token: str,
    obj_class: str,
    obj_id: int,
    attachment_id: str,
) -> dict | None:
    """Return the non-expired record matching attachment_id, regardless of served."""
    now = time.time()
    rows = db.execute(
        "SELECT " + _SELECT_COLS + " FROM attachment_metadata "
        "WHERE token = ? AND obj_class = ? AND obj_id = ? "
        "AND id = ? AND expires_at >= ?",
        (token, obj_class, obj_id, attachment_id, now),
    )
    return _row_to_dict(rows[0]) if rows else None


def get_prepared_attachment_metadata(
    token: str,
    obj_class: str,
    obj_id: int,
) -> list[dict]:
    """Return all non-expired records with selected=1 for (token, obj_class, obj_id).

    These are the attachments explicitly queued by prepare_object_attachments().
    Multiple rows can be prepared simultaneously.
    """
    now = time.time()
    rows = db.execute(
        "SELECT " + _SELECT_COLS + " FROM attachment_metadata "
        "WHERE token = ? AND obj_class = ? AND obj_id = ? "
        "AND selected = 1 AND expires_at >= ?",
        (token, obj_class, obj_id, now),
    )
    return [_row_to_dict(r) for r in rows]


# Keep the old single-result accessor for any internal callers that still
# rely on it (e.g. attachment_sync.py cache reads). Not part of public API.
def get_selected_attachment_metadata(
    token: str,
    obj_class: str,
    obj_id: int,
) -> dict | None:
    """Return the first non-expired record with selected=1, or None.

    Deprecated: use get_prepared_attachment_metadata() for multi-select.
    Retained for internal use by attachment_sync.py cache reads.
    """
    results = get_prepared_attachment_metadata(token, obj_class, obj_id)
    return results[0] if results else None


def set_prepared(
    token: str,
    obj_class: str,
    obj_id: int,
    attachment_ids: list[str],
) -> None:
    """Mark the given attachment IDs as prepared (selected=1); clear all others.

    Replaces set_selected(). Accepts one or more IDs. Atomically resets
    selected=0 for all rows of the object, then sets selected=1 for each
    ID in attachment_ids. IDs not found in the store are silently ignored.

    Args:
        token:          Raw bearer token from get_bearer_token().
        obj_class:      iTop class name, e.g. 'UserRequest'.
        obj_id:         Confirmed numeric iTop database ID.
        attachment_ids: One or more attachment IDs to mark as prepared.
    """
    with db.transaction():
        db.execute(
            "UPDATE attachment_metadata SET selected = 0 "
            "WHERE token = ? AND obj_class = ? AND obj_id = ?",
            (token, obj_class, obj_id),
        )
        for att_id in attachment_ids:
            db.execute(
                "UPDATE attachment_metadata SET selected = 1 "
                "WHERE token = ? AND obj_class = ? AND obj_id = ? AND id = ?",
                (token, obj_class, obj_id, att_id),
            )


def set_selected(
    token: str,
    obj_class: str,
    obj_id: int,
    attachment_id: str,
) -> None:
    """Set selected=1 for attachment_id, selected=0 for all other records.

    Deprecated: use set_prepared() with a list. Retained for backwards
    compatibility with any callers not yet updated.
    """
    set_prepared(token, obj_class, obj_id, [attachment_id])


def set_served(
    token: str,
    obj_class: str,
    obj_id: int,
    attachment_id: str,
) -> None:
    """Set served=1 for the given attachment_id."""
    db.execute(
        "UPDATE attachment_metadata SET served = 1 "
        "WHERE token = ? AND obj_class = ? AND obj_id = ? AND id = ?",
        (token, obj_class, obj_id, attachment_id),
    )


def set_all_served(
    token: str,
    obj_class: str,
    obj_id: int,
) -> None:
    """Set served=1 for all records of (token, obj_class, obj_id)."""
    db.execute(
        "UPDATE attachment_metadata SET served = 1 "
        "WHERE token = ? AND obj_class = ? AND obj_id = ?",
        (token, obj_class, obj_id),
    )


def store_image_content(
    token: str,
    obj_class: str,
    obj_id: int,
    attachment_id: str,
    content: bytes,
    mimetype: str,
) -> None:
    """Write normalized image bytes and updated mimetype into the content column.

    Called by attachment_sync.py after _normalize_image() succeeds.
    """
    db.execute(
        "UPDATE attachment_metadata SET content = ?, mimetype = ? "
        "WHERE token = ? AND obj_class = ? AND obj_id = ? AND id = ?",
        (content, mimetype, token, obj_class, obj_id, attachment_id),
    )


def clear_attachment_metadata(
    token: str,
    obj_class: str,
    obj_id: int,
) -> None:
    """Delete all records for (token, obj_class, obj_id).

    Called by start_sync() when switching to a different object.
    """
    with db.transaction():
        db.execute(
            "DELETE FROM attachment_metadata "
            "WHERE token = ? AND obj_class = ? AND obj_id = ?",
            (token, obj_class, obj_id),
        )


def clear_if_all_served(
    token: str,
    obj_class: str,
    obj_id: int,
) -> bool:
    """Delete active metadata for an object when every entry has been served.

    Checks atomically inside one transaction whether all non-expired records
    for (token, obj_class, obj_id) have served=1 and, if so, deletes them all
    (including cached image BLOBs stored in the same rows).

    Returns True when rows were deleted.
    Returns False when unserved active entries remain or no active metadata exists.
    """
    now = time.time()

    with db.transaction():
        active_rows = db.execute(
            "SELECT COUNT(*) FROM attachment_metadata "
            "WHERE token = ? AND obj_class = ? AND obj_id = ? "
            "AND expires_at >= ?",
            (token, obj_class, obj_id, now),
        )
        active_count = active_rows[0][0] if active_rows else 0
        if active_count == 0:
            return False

        unserved_rows = db.execute(
            "SELECT COUNT(*) FROM attachment_metadata "
            "WHERE token = ? AND obj_class = ? AND obj_id = ? "
            "AND expires_at >= ? AND served = 0",
            (token, obj_class, obj_id, now),
        )
        unserved_count = unserved_rows[0][0] if unserved_rows else 0
        if unserved_count:
            return False

        db.execute(
            "DELETE FROM attachment_metadata "
            "WHERE token = ? AND obj_class = ? AND obj_id = ?",
            (token, obj_class, obj_id),
        )

    logger.debug(
        "[attachment_store] clear_if_all_served: removed completed metadata "
        "cls=%s id=%d",
        obj_class,
        obj_id,
    )
    return True


def get_current_object_for_token(token: str) -> tuple[str, int] | None:
    """Return (obj_class, obj_id) of the most recently stored object for this token.

    Returns None when no non-expired record exists for the token.
    Used by resource handlers to identify the active object without
    passing parameters (MCP resources have no call parameters).
    """
    now = time.time()
    rows = db.execute(
        "SELECT obj_class, obj_id FROM attachment_metadata "
        "WHERE token = ? AND expires_at >= ? "
        "ORDER BY expires_at DESC LIMIT 1",
        (token, now),
    )
    if not rows:
        return None
    return rows[0][0], int(rows[0][1])


def purge_expired_metadata() -> int:
    """Delete all rows with expires_at < now(). Returns count removed.

    Called by the housekeeping loop in background_tasks.py.
    """
    logger.debug("[attachment_store] purge_expired_metadata: running purge")
    now = time.time()

    count_rows = db.execute(
        "SELECT COUNT(*) FROM attachment_metadata WHERE expires_at < ?",
        (now,),
    )
    removed = count_rows[0][0] if count_rows else 0

    with db.transaction():
        db.execute(
            "DELETE FROM attachment_metadata WHERE expires_at < ?",
            (now,),
        )

    logger.debug(
        "[attachment_store] purge_expired_metadata: removed %d row(s)", removed
    )
    return removed
