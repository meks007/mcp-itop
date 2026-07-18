"""
db/base.py - Abstract database backend interface.

Any module that needs persistent storage calls get_db() from db/__init__.py
and speaks SQL through this interface. The concrete engine is swapped in
db/__init__.py without touching any caller.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Iterator


class DbBackend(ABC):
    """Backend-agnostic SQL execution surface.

    Implementations handle connection management, transactions, and any
    engine-specific details. Domain modules call execute() / executemany()
    with plain SQL strings and parameter tuples; they never import sqlite3
    or any other DB driver directly.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open the connection and run any schema initialisation."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection cleanly."""

    @abstractmethod
    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Execute a single SQL statement and return all result rows.

        Always returns a list of tuples. Returns an empty list for
        statements that produce no rows (INSERT, UPDATE, DELETE, DDL).
        """

    @abstractmethod
    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """Execute a single SQL statement once for each row in rows."""

    @abstractmethod
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Context manager that commits on exit and rolls back on exception."""
