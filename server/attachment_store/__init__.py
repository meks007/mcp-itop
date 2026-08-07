"""
attachment_store/__init__.py - Public API shim for the attachment_store package.

Re-exports all public names from the submodules so callers can use:
    from attachment_store import store_attachment_metadata, start_sync, ...

session.py is superseded by metadata.py + attachment_sync.py.
It is kept in the directory but no longer imported here.
db.init() runs the _migrate_from_sessions migration in metadata.py which
drops the legacy attachment_sessions table on first startup.
"""

from attachment_store.metadata import (
    store_attachment_metadata,
    get_all_attachment_metadata,
    get_unserved_attachment_metadata,
    get_single_attachment_metadata,
    get_selected_attachment_metadata,
    set_selected,
    set_served,
    set_all_served,
    store_image_content,
    clear_attachment_metadata,
    get_current_object_for_token,
    purge_expired_metadata,
)
from attachment_store.attachment_sync import (
    start_sync,
    wait_for_image,
    wait_for_all,
    cancel_sync,
)
from attachment_store.refs import (
    write_inline_image_refs,
    read_inline_image_refs,
    purge_expired_inline_image_refs,
)

__all__ = [
    # metadata
    "store_attachment_metadata",
    "get_all_attachment_metadata",
    "get_unserved_attachment_metadata",
    "get_single_attachment_metadata",
    "get_selected_attachment_metadata",
    "set_selected",
    "set_served",
    "set_all_served",
    "store_image_content",
    "clear_attachment_metadata",
    "get_current_object_for_token",
    "purge_expired_metadata",
    # sync
    "start_sync",
    "wait_for_image",
    "wait_for_all",
    "cancel_sync",
    # refs (unchanged)
    "write_inline_image_refs",
    "read_inline_image_refs",
    "purge_expired_inline_image_refs",
]
