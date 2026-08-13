"""
attachment_store/attachment_sync.py - Background image sync task.

After List_object_attachments populates the metadata store,
start_sync() launches one asyncio Task per bearer token that downloads
and normalizes all image binaries for the current object, writing them
into the content column via store_image_content().

Non-image attachments (PDF, Word, Excel, etc.) are never downloaded here.
They are fetched live from iTop when a resource handler serves them.

Image detection rule:
    source == 'InlineImage'  OR  mimetype.startswith('image/')

Fetch strategy:
    source == 'InlineImage':
        ajax.document.php + auth_token query param (no REST API endpoint)
    source == 'Attachment', mime starts with 'image/':
        core/get Attachment fields=contents via REST API

In-memory state (_sync_state) is keyed by raw bearer token and holds
the current (obj_class, obj_id), the running Task, per-id Events, and
a done_event. State is not persisted to SQLite.

Same-object guard
-----------------
is_sync_running(token, obj_class, obj_id) must be called by
List_object_attachments BEFORE store_attachment_metadata() to prevent
the store from being reset while a sync for the same object is already
running. start_sync() itself enforces the same guard, but by the time
it is called the store write has already occurred if the guard is not
checked beforehand.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field

import httpx

from attachment_store.image import _normalize_image
from attachment_store.metadata import (
    store_image_content,
    clear_attachment_metadata,
)
from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, ITOP_VERSION

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _inline_image_url(img_id: str, secret: str) -> str:
    return (
        ITOP_URL + "/webservices/ajax.document.php"
        "?operation=download_inlineimage&id=" + str(img_id) + "&s=" + secret
    )


async def _download_inline_image(
    img_id: str,
    secret: str,
    token: str,
    http_client: httpx.AsyncClient,
) -> "tuple[bytes, str]":
    """Download an InlineImage via ajax.document.php with auth_token."""
    url = _inline_image_url(img_id, secret)
    url = url + "&auth_token=" + token
    logger.debug("[attachment_sync] _download_inline_image: GET id=%s", img_id)
    response = await http_client.get(url)
    response.raise_for_status()
    ct = response.headers.get("content-type", "application/octet-stream")
    mimetype = ct.split(";")[0].strip()
    logger.debug(
        "[attachment_sync] _download_inline_image: done id=%s mime=%s bytes=%d",
        img_id, mimetype, len(response.content),
    )
    return response.content, mimetype


async def _fetch_image_attachment(
    attachment_id: str,
    token: str,
    http_client: httpx.AsyncClient,
    mimetype_hint: str = "application/octet-stream",
) -> "tuple[bytes, str]":
    """Fetch image-type Attachment binary via iTop REST API (core/get)."""
    logger.debug("[attachment_sync] _fetch_image_attachment: id=%s", attachment_id)
    url = ITOP_URL + "/webservices/rest.php"
    op = {
        "operation": "core/get",
        "class": "Attachment",
        "key": attachment_id,
        "output_fields": "contents",
    }
    data = {
        "version": ITOP_VERSION,
        "json_data": json.dumps(op),
        "auth_token": token,
    }
    response = await http_client.post(url, data=data)
    response.raise_for_status()
    result = response.json()
    objects = result.get("objects") or {}
    if not objects:
        raise ValueError("Attachment id=" + attachment_id + " not found via REST API")
    obj = next(iter(objects.values()))
    contents = (obj.get("fields") or {}).get("contents") or {}
    if isinstance(contents, dict):
        mime = (contents.get("mimetype") or mimetype_hint).strip()
        b64data = contents.get("data") or ""
    else:
        raise ValueError("Unexpected contents format for attachment id=" + attachment_id)
    if not b64data:
        raise ValueError("No content data for attachment id=" + attachment_id)
    logger.debug(
        "[attachment_sync] _fetch_image_attachment: done id=%s mime=%s",
        attachment_id, mime,
    )
    return base64.b64decode(b64data), mime


# ---------------------------------------------------------------------------
# In-memory sync state
# ---------------------------------------------------------------------------

@dataclass
class _SyncState:
    obj_class: str
    obj_id: int
    task: asyncio.Task
    events: dict[str, asyncio.Event] = field(default_factory=dict)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)


# keyed by raw bearer token
_sync_state: dict[str, _SyncState] = {}


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _sync_task(
    token: str,
    obj_class: str,
    obj_id: int,
    entries: list[dict],
) -> None:
    """Download and normalize image binaries for all image entries.

    Signals per-id Event after each entry (success or failure).
    Signals done_event when all entries have been processed, guaranteed
    via try/finally so that waiters in wait_for_all() are always unblocked
    even when the task is cancelled mid-run.
    """
    token_preview = token[:8] + "..." if len(token) > 8 else token
    logger.debug(
        "[attachment_sync] _sync_task: start token=%s cls=%s id=%d entries=%d",
        token_preview, obj_class, obj_id, len(entries),
    )

    try:
        async with httpx.AsyncClient(
            verify=ITOP_VERIFY_SSL,
            timeout=ITOP_TIMEOUT,
        ) as http_client:
            for entry in entries:
                entry_id = entry["id"]
                source = entry.get("source", "")
                mimetype = entry.get("mimetype", "")
                is_image = (
                    source == "InlineImage"
                    or mimetype.startswith("image/")
                )

                if not is_image:
                    # Signal event immediately so wait_for_image() never blocks.
                    state = _sync_state.get(token)
                    if state and entry_id in state.events:
                        state.events[entry_id].set()
                    continue

                try:
                    if source == "InlineImage":
                        secret = entry.get("inline_secret") or ""
                        binary, dl_mimetype = await _download_inline_image(
                            entry_id, secret, token, http_client,
                        )
                    else:
                        binary, dl_mimetype = await _fetch_image_attachment(
                            entry_id, token, http_client, mimetype,
                        )

                    used_mime = dl_mimetype if dl_mimetype else mimetype
                    filename = entry.get("filename", "attachment")

                    normalized, norm_mime, norm_filename = _normalize_image(
                        binary, used_mime, filename
                    )
                    # Pass norm_filename so the DB filename column is updated to
                    # the .jpg name. build_prepared_attachment_payloads() reads it
                    # back via get_single_attachment_metadata() to build the URI,
                    # ensuring uri and mimeType are consistent (both .jpg / jpeg).
                    store_image_content(
                        token, obj_class, obj_id, entry_id,
                        normalized, norm_mime, norm_filename,
                    )
                    logger.debug(
                        "[attachment_sync] _sync_task: stored id=%s"
                        " mime=%s filename=%s bytes=%d",
                        entry_id, norm_mime, norm_filename, len(normalized),
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    logger.warning(
                        "[attachment_sync] _sync_task: failed id=%s: %s",
                        entry_id, exc,
                    )

                finally:
                    state = _sync_state.get(token)
                    if state and entry_id in state.events:
                        state.events[entry_id].set()

    finally:
        # Always unblock waiters regardless of cancellation or error.
        state = _sync_state.get(token)
        if state:
            state.done_event.set()

    logger.debug(
        "[attachment_sync] _sync_task: done token=%s cls=%s id=%d",
        token_preview, obj_class, obj_id,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_sync_running(token: str, obj_class: str, obj_id: int) -> bool:
    """Return True when a sync task for (token, obj_class, obj_id) is active.

    Call this BEFORE store_attachment_metadata() in List_object_attachments
    to avoid resetting a cache that is currently being populated by a running
    sync task for the same object. If this returns True, skip the store write
    and let start_sync() confirm the no-op.
    """
    state = _sync_state.get(token)
    if state is None:
        return False
    return state.obj_class == obj_class and state.obj_id == obj_id


async def start_sync(
    token: str,
    obj_class: str,
    obj_id: int,
    entries: list[dict],
) -> "str | None":
    """Start or replace the background sync task for this token.

    If the token already has a running sync for the same (obj_class, obj_id):
        -> no-op; returns None.
        Note: store_attachment_metadata() must NOT have been called before
        this point for same-object calls. Use is_sync_running() to guard.

    If the token has a running sync for a different object:
        -> cancel the existing task
        -> clear metadata for the old object
        -> start a new task for the new object
        -> return warning string describing the switch.

    If the token has no running sync:
        -> start a new task; returns None.

    Creates one asyncio.Event per entry. Non-image entry events are set
    immediately by the task so wait_for_image() never blocks on them.

    Race-condition note
    -------------------
    The old task's done callback (_on_done) may fire and remove
    _sync_state[token] while we are awaiting asyncio.shield(existing.task).
    The removal is therefore performed with an identity-checked pop:
    the entry is only deleted when it still belongs to the old _SyncState
    instance, preventing a KeyError and avoiding accidental removal of a
    state that was already replaced by another coroutine.
    """
    token_preview = token[:8] + "..." if len(token) > 8 else token
    existing = _sync_state.get(token)
    warning: "str | None" = None

    if existing is not None:
        if existing.obj_class == obj_class and existing.obj_id == obj_id:
            logger.debug(
                "[attachment_sync] start_sync: no-op, same object token=%s cls=%s id=%d",
                token_preview, obj_class, obj_id,
            )
            return None

        # Different object -- cancel old task and clear old metadata.
        old_class = existing.obj_class
        old_id = existing.obj_id
        logger.debug(
            "[attachment_sync] start_sync: switching object token=%s "
            "old=%s/%d new=%s/%d",
            token_preview, old_class, old_id, obj_class, obj_id,
        )
        existing.task.cancel()
        try:
            await asyncio.shield(existing.task)
        except (asyncio.CancelledError, Exception):
            pass

        # Identity-checked removal: the done callback may have already removed
        # the entry while we were awaiting the cancelled task above. Only pop
        # when the slot still holds the same _SyncState we started with.
        if _sync_state.get(token) is existing:
            _sync_state.pop(token, None)
            logger.debug(
                "[attachment_sync] start_sync: state removed token=%s", token_preview
            )

        clear_attachment_metadata(token, old_class, old_id)
        warning = (
            "Cache for " + old_class + " obj_id " + str(old_id)
            + " cleared. Now preparing " + obj_class + " obj_id " + str(obj_id) + "."
        )

    # Build per-entry events.
    events: dict[str, asyncio.Event] = {e["id"]: asyncio.Event() for e in entries}
    done_event = asyncio.Event()

    task = asyncio.create_task(
        _sync_task(token, obj_class, obj_id, entries),
        name="attachment_sync_" + token_preview,
    )

    _sync_state[token] = _SyncState(
        obj_class=obj_class,
        obj_id=obj_id,
        task=task,
        events=events,
        done_event=done_event,
    )

    # Clean up state dict when task finishes.
    def _on_done(t: asyncio.Task) -> None:
        state = _sync_state.get(token)
        if state and state.task is t:
            del _sync_state[token]
            logger.debug(
                "[attachment_sync] _on_done: state removed token=%s", token_preview
            )

    task.add_done_callback(_on_done)

    logger.debug(
        "[attachment_sync] start_sync: task started token=%s cls=%s id=%d",
        token_preview, obj_class, obj_id,
    )
    return warning


async def wait_for_image(token: str, attachment_id: str) -> None:
    """Wait until the binary for the given attachment id is available in the cache.

    Returns immediately when:
    - no sync state exists for the token (sync done or never started), or
    - the event for attachment_id does not exist (not an image entry).
    """
    state = _sync_state.get(token)
    if state is None:
        return
    evt = state.events.get(attachment_id)
    if evt is None:
        return
    await evt.wait()


async def wait_for_all(token: str) -> None:
    """Wait until all image binaries for this token's current sync are cached.

    Returns immediately when no sync is running.
    """
    state = _sync_state.get(token)
    if state is None:
        return
    await state.done_event.wait()


def cancel_sync(token: str) -> None:
    """Cancel the running sync task and remove state for this token.

    Safe to call when no sync is running.
    """
    state = _sync_state.get(token)
    if state is None:
        return
    state.task.cancel()
    del _sync_state[token]
    token_preview = token[:8] + "..." if len(token) > 8 else token
    logger.debug("[attachment_sync] cancel_sync: cancelled token=%s", token_preview)
