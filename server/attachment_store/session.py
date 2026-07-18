"""
attachment_store/session.py - Session-bound image storage.

Stores and retrieves image entries keyed by the current client bearer token.
Each entry is valid for IMAGE_STORE_TTL_SECONDS.
"""

from __future__ import annotations

import time
import logging
from typing import TypedDict

from attachment_store.db import _get_conn, IMAGE_STORE_TTL_SECONDS, IMAGE_STORE_DB_PATH
from attachment_store.image import _normalize_image

logger = logging.getLogger(__name__)


class ImageEntry(TypedDict):
    uri: str       # short itop:// reference
    content: bytes # raw JPEG bytes (always populated before store)
    mimetype: str  # always image/jpeg after normalization
    filename: str


def store_images(token: str, images: list[ImageEntry]) -> None:
    """Persist image entries for the given bearer token.

    Each entry with non-None content is normalized to JPEG via
    _normalize_image() before insertion. Entries without content are
    stored as-is (should not occur with Option B eager download).

    Replaces any existing entries for this token. Expired rows are purged
    by the central housekeeping task. Each entry is valid for
    IMAGE_STORE_TTL_SECONDS.

    Args:
        token:  The raw bearer token for the current MCP client session.
        images: List of dicts with keys: uri, content, mimetype, filename.
                uri must be a short itop:// reference (no data: URIs).
                content must be raw image bytes (all sources pre-downloaded).
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
        deleted_token = conn.execute(
            "DELETE FROM attachment_sessions WHERE token = ?",
            (token,),
        ).rowcount
        logger.debug(
            "[attachment_store] store_images: deleted %d old row(s) for token=%s",
            deleted_token,
            token_preview,
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

        conn.executemany(
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

    conn = _get_conn()
    cursor = conn.execute(
        "SELECT uri, content, mimetype, filename, expires_at "
        "FROM attachment_sessions "
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
    conn = _get_conn()
    with conn:
        cursor = conn.execute(
            "DELETE FROM attachment_sessions WHERE expires_at < ?",
            (time.time(),),
        )
    removed = cursor.rowcount
    logger.debug("[attachment_store] purge_expired_images: removed %d row(s)", removed)
    return removed
