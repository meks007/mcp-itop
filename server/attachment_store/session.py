# attachment_store/session.py
#
# DEPRECATED - do not use.
#
# This module has been superseded by:
#   attachment_store/metadata.py         (metadata store)
#   attachment_store/attachment_sync.py  (background image sync task)
#
# It is no longer imported by attachment_store/__init__.py.
# The legacy attachment_sessions SQLite table is dropped automatically on
# first server startup by the _migrate_from_sessions migration in metadata.py.
#
# This file is kept to avoid git history loss and will be removed in a
# future cleanup commit.

from __future__ import annotations

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)


class ImageEntry(TypedDict):
    uri: str
    content: bytes
    mimetype: str
    filename: str


def store_images(token: str, images: list) -> None:
    raise RuntimeError("session.py is deprecated. Use metadata.py instead.")


def get_next_image(token: str) -> None:
    raise RuntimeError("session.py is deprecated. Use metadata.py instead.")


def purge_expired_images() -> int:
    raise RuntimeError("session.py is deprecated. Use metadata.py instead.")
