"""
attachment_store.py - SQLite-backed store for itop image content.

Stores image binaries (as BLOB) and metadata keyed by the bearer token.
Used by the static MCP resource handler itop://attachment/images to retrieve
the image set produced by the most recent itop_get_ticket_images tool call
for the current client session.

Schema
------
TABLE attachment_sessions (
    token       TEXT NOT NULL,
    uri         TEXT NOT NULL,      -- short itop:// reference, never a data: URI
    content     BLOB,               -- raw image bytes (always PNG after normalization)
    mimetype    TEXT NOT NULL,      -- always image/png after normalization
    filename    TEXT NOT NULL,
    expires_at  REAL NOT NULL       -- Unix timestamp (UTC)
)

All images are converted to PNG and downscaled if they exceed IMAGE_MAX_BYTES
before being written to the database. See _normalize_image() for details.

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
from io import BytesIO
from pathlib import Path
from typing import TypedDict

from PIL import Image as _PILImage

from config import IMAGE_MAX_BYTES, logger

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
    uri: str           # short itop:// reference
    content: bytes     # raw PNG bytes (always populated before store)
    mimetype: str      # always image/png after normalization
    filename: str


# ---------------------------------------------------------------------------
# Image normalization
# ---------------------------------------------------------------------------

def _normalize_image(
    data: bytes,
    mimetype: str,
    filename: str,
) -> tuple[bytes, str, str]:
    """Convert any image to PNG and downscale if it exceeds IMAGE_MAX_BYTES.

    Steps:
      1. Open with Pillow (supports JPEG, BMP, TIFF, WebP, GIF, ICO, ...).
      2. Convert to RGBA so palette/mode differences are flattened.
      3. Save as PNG. If the result exceeds IMAGE_MAX_BYTES, scale down by
         75% per iteration until it fits or the image is smaller than 10%.
      4. Rename the file extension to .png.

    Falls back to the original data and mimetype on any Pillow error so that
    a broken image does not block the whole request.
    """
    if not data:
        return data, mimetype, filename

    max_bytes = IMAGE_MAX_BYTES

    try:
        img = _PILImage.open(BytesIO(data)).convert("RGBA")

        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        new_filename = stem + ".png"

        scale = 1.0
        while True:
            buf = BytesIO()
            if scale < 1.0:
                new_w = max(1, int(img.width * scale))
                new_h = max(1, int(img.height * scale))
                frame = img.resize((new_w, new_h), _PILImage.LANCZOS)
            else:
                frame = img
            frame.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()

            if max_bytes <= 0 or len(png_bytes) <= max_bytes or scale < 0.10:
                break
            scale *= 0.75

        logger.debug(
            "[attachment_store] _normalize_image: %s -> PNG %d bytes"
            " (original %d bytes, scale=%.2f)",
            filename, len(png_bytes), len(data), scale,
        )
        return png_bytes, "image/png", new_filename

    except Exception as exc:
        logger.warning(
            "[attachment_store] _normalize_image: failed for %s, keeping original: %s",
            filename, exc,
        )
        return data, mimetype, filename


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Module-level connection. Set by init_db(); never None after startup.
_conn: sqlite3.Connection | None = None

# Background vacuum timer thread. Kept to avoid garbage collection.
_vacuum_thread: threading.Thread | None = None


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
    # Enable incremental auto_vacuum so SQLite tracks free pages for later
    # reclamation via PRAGMA incremental_vacuum.
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
    """Persist image entries for the given bearer token.

    Each entry with a non-None content is normalized to PNG (format
    conversion + size capping) via _normalize_image() before insertion.
    Entries without content (should not occur with Option B) are stored as-is.

    Replaces any existing entries for this token and purges all expired
    rows from the table. Each entry is valid for IMAGE_STORE_TTL_SECONDS.

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

        # Normalize each image to PNG, then insert.
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
