"""
attachment_store/refs.py - Inline image ref cache.

Refs are extracted from ticket HTML fields by parse_objects() in
helpers/html.py and written here so that tools/attachments.py can
retrieve them without re-fetching the ticket.

Schema registered at module import time via db.register_schema() so that
db.init() creates the table without any explicit init_db() call from callers.

Security note
-------------
Each cache entry is scoped to a token_hash (SHA-256 hex digest of the raw
bearer token obtained from the async request context). This prevents a
lower-privilege token from reading inline image refs that were cached by a
higher-privilege token during the same TTL window. The raw token is never
stored; only its digest is persisted.
"""

from __future__ import annotations

import hashlib
import time
import logging

import db
from auth import get_bearer_token
from config import INLINE_IMAGE_REF_TTL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema registration (runs at import time, before db.init())
# ---------------------------------------------------------------------------

db.register_schema("""
CREATE TABLE IF NOT EXISTS inline_image_refs (
    token_hash  TEXT NOT NULL,
    obj_class   TEXT NOT NULL,
    obj_id      TEXT NOT NULL,
    img_id      TEXT NOT NULL,
    img_secret  TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (token_hash, obj_class, obj_id, img_id)
)
""")

db.register_schema(
    "CREATE INDEX IF NOT EXISTS idx_iir_lookup "
    "ON inline_image_refs (token_hash, obj_class, obj_id, expires_at)"
)

# Migration: add token_hash to existing databases that were created before
# this column was introduced. SQLite does not allow adding a column with a
# PRIMARY KEY constraint after the fact, so we rebuild the table when the
# column is absent. Existing rows are dropped -- backfilling a meaningful
# token hash is not possible.
def _migrate_add_token_hash(backend) -> None:
    rows = backend.execute("PRAGMA table_info(inline_image_refs)")
    col_names = {row[1] for row in rows}
    if "token_hash" in col_names:
        return  # already up to date

    logger.info(
        "[attachment_store.refs] migrating inline_image_refs: adding token_hash column"
    )
    with backend.transaction():
        backend.execute("DROP TABLE IF EXISTS inline_image_refs_old")
        backend.execute(
            "ALTER TABLE inline_image_refs RENAME TO inline_image_refs_old"
        )
        backend.execute("""
CREATE TABLE inline_image_refs (
    token_hash  TEXT NOT NULL,
    obj_class   TEXT NOT NULL,
    obj_id      TEXT NOT NULL,
    img_id      TEXT NOT NULL,
    img_secret  TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (token_hash, obj_class, obj_id, img_id)
)
""")
        backend.execute(
            "CREATE INDEX IF NOT EXISTS idx_iir_lookup "
            "ON inline_image_refs (token_hash, obj_class, obj_id, expires_at)"
        )
        backend.execute("DROP TABLE inline_image_refs_old")
    logger.info(
        "[attachment_store.refs] migration complete: inline_image_refs rebuilt"
    )

db.register_migration(_migrate_add_token_hash)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_token_hash() -> str:
    """Return the SHA-256 hex digest of the current request bearer token."""
    return hashlib.sha256(get_bearer_token().encode()).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_inline_image_refs(
    obj_class: str,
    obj_id: str,
    refs: list[dict],
) -> None:
    """Upsert inline image refs for a ticket into the cache.

    Deletes all existing refs for (token_hash, obj_class, obj_id) first,
    then inserts the new set. A ref is a dict with keys 'id' and 'secret'.
    Passing an empty refs list clears the cache entry (no inline images found).

    The token hash is derived from the bearer token in the current async
    request context; the raw token is never written to the database.

    Args:
        obj_class: iTop class name, e.g. 'UserRequest'.
        obj_id:    Numeric ticket ID as string.
        refs:      List of {'id': str, 'secret': str} dicts.
    """
    th = _current_token_hash()
    expires_at = time.time() + INLINE_IMAGE_REF_TTL

    with db.transaction():
        db.execute(
            "DELETE FROM inline_image_refs "
            "WHERE token_hash = ? AND obj_class = ? AND obj_id = ?",
            (th, obj_class, obj_id),
        )
        if refs:
            db.executemany(
                "INSERT OR REPLACE INTO inline_image_refs "
                "(token_hash, obj_class, obj_id, img_id, img_secret, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (th, obj_class, obj_id, r["id"], r["secret"], expires_at)
                    for r in refs
                ],
            )
    logger.debug(
        "[attachment_store] write_inline_image_refs: "
        "token_prefix=%s cls=%r id=%r wrote %d ref(s)",
        th[:8], obj_class, obj_id, len(refs),
    )


def read_inline_image_refs(
    obj_class: str,
    obj_id: str,
) -> list[dict] | None:
    """Return cached inline image refs for a ticket, or None on cache miss.

    Returns None when no entry exists or all entries are expired (cache miss).
    Returns an empty list when the entry exists but has zero refs (meaning the
    ticket was previously confirmed to have no inline images for this token).

    Only rows written by the same token (matched via SHA-256 digest of the
    bearer token from the current request context) are returned, preventing
    cross-token ref leakage.

    Args:
        obj_class: iTop class name.
        obj_id:    Numeric ticket ID as string.
    """
    th = _current_token_hash()
    now = time.time()

    rows = db.execute(
        "SELECT img_id, img_secret, expires_at "
        "FROM inline_image_refs "
        "WHERE token_hash = ? AND obj_class = ? AND obj_id = ? "
        "ORDER BY img_id",
        (th, obj_class, obj_id),
    )

    if not rows:
        logger.debug(
            "[attachment_store] read_inline_image_refs: "
            "token_prefix=%s cls=%r id=%r -> miss (no rows)",
            th[:8], obj_class, obj_id,
        )
        return None

    if rows[0][2] < now:
        logger.debug(
            "[attachment_store] read_inline_image_refs: "
            "token_prefix=%s cls=%r id=%r -> miss (expired)",
            th[:8], obj_class, obj_id,
        )
        return None

    found = [
        {"id": row[0], "secret": row[1]}
        for row in rows
        if row[0]  # skip tombstone sentinel rows (empty img_id)
    ]
    logger.debug(
        "[attachment_store] read_inline_image_refs: "
        "token_prefix=%s cls=%r id=%r -> hit %d ref(s)",
        th[:8], obj_class, obj_id, len(found),
    )
    return found


def purge_expired_inline_image_refs() -> int:
    """Delete all expired rows from inline_image_refs. Returns rows removed."""
    logger.debug(
        "[attachment_store] purge_expired_inline_image_refs: running purge"
    )
    now = time.time()

    count_rows = db.execute(
        "SELECT COUNT(*) FROM inline_image_refs WHERE expires_at < ?",
        (now,),
    )
    removed = count_rows[0][0] if count_rows else 0

    with db.transaction():
        db.execute(
            "DELETE FROM inline_image_refs WHERE expires_at < ?",
            (now,),
        )

    logger.debug(
        "[attachment_store] purge_expired_inline_image_refs: removed %d row(s)",
        removed,
    )
    return removed
