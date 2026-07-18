"""
db/sqlite.py - SQLite concrete implementation of DbBackend.

Uses a single sqlite3 connection. check_same_thread=False is safe here
because this module is only opened once at startup and uvicorn runs the
asyncio event loop in a single thread by default.

PRAGMAs applied on connect:
  journal_mode=WAL          -- allows concurrent readers during writes
  auto_vacuum=INCREMENTAL   -- enables page reclamation via incremental_vacuum
"""

from __future__ import annotations

import sqlite3
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from db.base import DbBackend

logger = logging.getLogger(__name__)


class SqliteDbBackend(DbBackend):
    """SQLite-backed DbBackend using the standard library sqlite3 module."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the SQLite file and apply startup PRAGMAs."""
        logger.debug("[db.sqlite] opening DB at path=%s", self._db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self._conn.commit()
        logger.debug("[db.sqlite] connection ready")

    def close(self) -> None:
        """Close the SQLite connection cleanly."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.debug("[db.sqlite] connection closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError(
                "SqliteDbBackend: connect() was not called before execute()."
            )
        return self._conn

    # ------------------------------------------------------------------
    # Query surface
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Execute one SQL statement and return all rows as a list of tuples."""
        cursor = self._get_conn().execute(sql, params)
        rows = cursor.fetchall()
        return rows

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """Execute one SQL statement once per row."""
        self._get_conn().executemany(sql, rows)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Context manager: commit on success, rollback on exception."""
        conn = self._get_conn()
        with conn:
            yield

    # ------------------------------------------------------------------
    # SQLite-specific helpers (not part of DbBackend contract)
    # ------------------------------------------------------------------

    def incremental_vacuum(self) -> None:
        """Run PRAGMA incremental_vacuum to reclaim free pages."""
        try:
            self._get_conn().execute("PRAGMA incremental_vacuum")
            size = Path(self._db_path).stat().st_size
            logger.debug(
                "[db.sqlite] incremental_vacuum done, db_size=%d bytes", size
            )
        except Exception as exc:
            logger.warning("[db.sqlite] incremental_vacuum failed: %s", exc)

    @property
    def db_path(self) -> str:
        return self._db_path
