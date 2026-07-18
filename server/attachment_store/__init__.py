"""
attachment_store/__init__.py - Public API shim for the attachment_store package.

Re-exports all public names from the submodules so that existing imports like
    from attachment_store import store_images, init_db
continue to work without any changes to callers.
"""

from attachment_store.db import (
    init_db,
    IMAGE_STORE_TTL_SECONDS,
    IMAGE_STORE_DB_PATH,
)
from attachment_store.image import _normalize_image
from attachment_store.session import (
    store_images,
    get_images,
    purge_expired_images,
    ImageEntry,
)
from attachment_store.refs import (
    write_inline_image_refs,
    read_inline_image_refs,
    purge_expired_inline_image_refs,
)

__all__ = [
    "init_db",
    "store_images",
    "get_images",
    "purge_expired_images",
    "ImageEntry",
    "write_inline_image_refs",
    "read_inline_image_refs",
    "purge_expired_inline_image_refs",
    "_normalize_image",
    "IMAGE_STORE_TTL_SECONDS",
    "IMAGE_STORE_DB_PATH",
]
