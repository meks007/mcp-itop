"""
attachment_store/__init__.py - Public API shim for the attachment_store package.

Re-exports all public names from the submodules so that existing imports like
    from attachment_store import store_images, ImageEntry
continue to work without any changes to callers.

Note: init_db() is no longer exported. Schema registration now happens
automatically at import time via db.register_schema() in session.py and
refs.py. Callers only need to call db.init() once at server startup.
"""

from attachment_store.image import _normalize_image
from attachment_store.session import (
    store_images,
    get_next_image,
    purge_expired_images,
    ImageEntry,
)
from attachment_store.refs import (
    write_inline_image_refs,
    read_inline_image_refs,
    purge_expired_inline_image_refs,
)

__all__ = [
    "store_images",
    "get_next_image",
    "purge_expired_images",
    "ImageEntry",
    "write_inline_image_refs",
    "read_inline_image_refs",
    "purge_expired_inline_image_refs",
    "_normalize_image",
]
