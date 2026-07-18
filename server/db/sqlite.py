"""
db/sqlite.py - SQLite concrete implementation of DbBackend.

Uses a single sqlite3 connection. check_same_thread=False is safe here
because this module is only opened once at startup and uvicorn runs the
asyncio event loop in a single thread by default.

PRAGMAs applied on connect:
  journal_mode=WAL          -- allows concurrent readers during writes
  auto_vacuum=INCREMENTAL   -- enables page reclamation via incremental_vacuum

Env vars read by this module:
  SQLITE_DB_PATH          -- path to the database file
                             default: <server_root>/mcp_itop.db
  SQLITE_VACUUM_INTERVAL  -- seconds between incremental_vacuum runs
                             set to 0 to disable; default: 3600
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from db.base import DbBackend

logger = logging.getLogger(__name__)


class Backend(DbBackend):
    """SQLite-backed DbBackend using the standard library sqlite3 module."""

    def __init__(self) -> None:
        default = Path(__file__).parent.parent / "mcp_itop.db"
        self._db_path: str = os.getenv("SQLITE_DB_PATH", str(default))
        self._conn: sqlite3.Connection | None = None
        self._vacuum_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the SQLite file, apply startup PRAGMAs, start vacuum thread."""
        logger.debug("[db.sqlite] opening DB at path=%s", self._db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self._conn.commit()
        logger.debug("[db.sqlite] connection ready")

        interval = float(os.getenv("SQLITE_VACUUM_INTERVAL", "3600"))
        if interval > 0:
            self._vacuum_thread = threading.Thread(
                target=self._vacuum_loop,
                args=(interval,),
                daemon=True,
                name="sqlite-vacuum",
            )
            self._vacuum_thread.start()
            logger.info(
                "[db.sqlite] vacuum thread started, interval=%.0fs", interval
            )
        else:
            logger.info("[db.sqlite] vacuum thread disabled (SQLITE_VACUUM_INTERVAL=0)")

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
                "Backend: connect() was not called before execute()."
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

    def _vacuum_loop(self, interval: float) -> None:
        """Background thread body: sleep, then vacuum, repeat."""
        logger.debug(
            "[db.sqlite] vacuum_loop: started, interval=%.0fs", interval
        )
        while True:
            time.sleep(interval)
            logger.debug("[db.sqlite] vacuum_loop: running incremental_vacuum")
            self.incremental_vacuum()

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
