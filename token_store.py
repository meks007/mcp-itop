"""
UPN-to-iTop-token mapping store.

Reads token_store.yaml at startup (path from oauth_config.yaml).
The store is loaded once and held in memory. Restart the server to
pick up changes to the mapping file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from config import logger


class TokenStore:
    """In-memory UPN -> iTop token lookup."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._mapping: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            raise FileNotFoundError(
                f"Token store file not found: {self._path}. "
                "Create it from token_store.yaml.example."
            )
        with open(self._path) as fh:
            raw = yaml.safe_load(fh) or {}
        users = raw.get("users") or {}
        if not isinstance(users, dict):
            raise ValueError("token_store.yaml: 'users' must be a mapping of UPN -> token.")
        self._mapping = {str(k).strip(): str(v).strip() for k, v in users.items()}
        logger.info("Token store loaded: %d user(s) from %s", len(self._mapping), self._path)

    def get_itop_token(self, upn: str) -> str | None:
        """Return the iTop token for a given UPN, or None if not mapped."""
        return self._mapping.get(upn)
