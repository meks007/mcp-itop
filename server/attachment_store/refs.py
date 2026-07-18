"""
attachment_store/refs.py - Inline image ref cache.

Refs are extracted from ticket HTML fields by parse_objects() in
helpers/html.py and written here so that tools/attachments.py can
retrieve them without re-fetching the ticket.
"""

from __future__ import annotations

import time
import logging

import db as _db_layer
from config import INLINE_IMAGE_REF_TTL

logger = logging.getLogger(__name__)


def write_inline_image_refs(
    obj_class: str,
    obj_id: str,
    refs: list[dict],
) -> None:
    """Upsert inline image refs for a ticket into the cache.

    Deletes all existing refs for (obj_class, obj_id) first, then inserts
    the new set. A ref is a dict with keys 'id' and 'secret'. Passing an
    empty refs list clears the cache entry (no inline images found).

    Args:
        obj_class: iTop class name, e.g. 'UserRequest'.
        obj_id:    Numeric ticket ID as string.
        refs:      List of {'id': str, 'secret': str} dicts.
    """
    expires_at = time.time() + INLINE_IMAGE_REF_TTL
    backend = _db_layer.get_db()

    with backend.transaction():
        backend.execute(
            "DELETE FROM inline_image_refs WHERE obj_class = ? AND obj_id = ?",
            (obj_class, obj_id),
        )
        if refs:
            backend.executemany(
                "INSERT OR REPLACE INTO inline_image_refs "
                "(obj_class, obj_id, img_id, img_secret, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (obj_class, obj_id, r["id"], r["secret"], expires_at)
                    for r in refs
                ],
            )
    logger.debug(
        "[attachment_store] write_inline_image_refs: cls=%r id=%r wrote %d ref(s)",
        obj_class, obj_id, len(refs),
    )


def read_inline_image_refs(
    obj_class: str,
    obj_id: str,
) -> list[dict] | None:
    """Return cached inline image refs for a ticket, or None on cache miss.

    Returns None when no entry exists or all entries are expired (cache miss).
    Returns an empty list when the entry exists but has zero refs (meaning the
    ticket was previously confirmed to have no inline images).

    Args:
        obj_class: iTop class name.
        obj_id:    Numeric ticket ID as string.
    """
    now = time.time()

    rows = _db_layer.get_db().execute(
        "SELECT img_id, img_secret, expires_at "
        "FROM inline_image_refs "
        "WHERE obj_class = ? AND obj_id = ? "
        "ORDER BY img_id",
        (obj_class, obj_id),
    )

    if not rows:
        logger.debug(
            "[attachment_store] read_inline_image_refs: cls=%r id=%r -> miss (no rows)",
            obj_class, obj_id,
        )
        return None

    if rows[0][2] < now:
        logger.debug(
            "[attachment_store] read_inline_image_refs: cls=%r id=%r -> miss (expired)",
            obj_class, obj_id,
        )
        return None

    refs = [
        {"id": row[0], "secret": row[1]}
        for row in rows
        if row[0]  # skip tombstone sentinel rows (empty img_id)
    ]
    logger.debug(
        "[attachment_store] read_inline_image_refs: cls=%r id=%r -> hit %d ref(s)",
        obj_class, obj_id, len(refs),
    )
    return refs


def purge_expired_inline_image_refs() -> int:
    """Delete all expired rows from inline_image_refs. Returns rows removed."""
    logger.debug(
        "[attachment_store] purge_expired_inline_image_refs: running purge"
    )
    now = time.time()
    backend = _db_layer.get_db()

    count_rows = backend.execute(
        "SELECT COUNT(*) FROM inline_image_refs WHERE expires_at < ?",
        (now,),
    )
    removed = count_rows[0][0] if count_rows else 0

    with backend.transaction():
        backend.execute(
            "DELETE FROM inline_image_refs WHERE expires_at < ?",
            (now,),
        )

    logger.debug(
        "[attachment_store] purge_expired_inline_image_refs: removed %d row(s)",
        removed,
    )
    return removed
