"""
tools/attachments.py - Unified attachment tools and resources.

ID-only contract
----------------
All tools require a confirmed integer database ID (obj_id: int).
Use Resolve_object first when you only have a ref or ambiguous identifier.

Public API
----------
register(mcp, client)
    Registers the following MCP tools and resources:

    Tools:
        List_object_attachments(obj_class, obj_id)
            Lists all attachments and inline images for any iTop object.
            Writes metadata to the session store and starts a background
            task that downloads and normalizes image binaries.
            Works for UserRequest, Incident, Change, FAQ, FunctionalCI, etc.

            Same-object guard: if a sync for the same (obj_class, obj_id)
            is already running, the store is NOT reset and start_sync()
            returns as a no-op. is_sync_running() is checked before any
            store write to avoid erasing cached image content.

        Prepare_single_attachment(obj_class, obj_id, id)
            Marks a single attachment for retrieval via get_single_attachment.
            Call List_object_attachments first to populate the store and
            obtain the attachment id from the listing.

    Resources:
        itop://attachment/get_attachments
            Downloads and returns all unserved attachments in a single
            multi-entry contents array. Images are served from cache
            (waits for background sync). Non-images are fetched live
            via REST API (core/get Attachment fields=contents).
            Only successfully returned attachments are marked served;
            failed entries remain served=0 and are retried on next call.
            The actual transport response is produced by the low-level
            router installed in server.py via get_low_level_resource_handlers().
            The FastMCP-decorated stub below keeps the resource visible in
            resources/list but returns [] and is never called for the read.

        itop://attachment/get_single_attachment
            Downloads and returns the single attachment marked by
            Prepare_single_attachment. Images served from cache,
            non-images fetched live via REST API. Marks record as served.

get_low_level_resource_handlers(client)
    Returns a dict mapping resource URIs to low-level handler callables.
    Used by server.py to install the central ReadResourceRequest router.

iTop blob field notes
---------------------
The contents AttributeBlob is returned by the REST API as a dict:
  {"mimetype": "<mime>", "data": "<base64>", "filename": "<name>"}

InlineImage refs are resolved from <img data-img-id data-img-secret> tags
found in iTop HTML fields after _fetch_and_cache_object() has run.
The refs are stored in the inline_image_refs SQLite cache by
helpers/formatters.py (via parse_objects()) and read back here.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import mcp.types as types

from attachment_store import (
    is_sync_running,
    store_attachment_metadata,
    get_unserved_attachment_metadata,
    get_single_attachment_metadata,
    get_selected_attachment_metadata,
    get_current_object_for_token,
    set_selected,
    set_served,
    start_sync,
    wait_for_image,
    wait_for_all,
    read_inline_image_refs,
    write_inline_image_refs,
)
from auth import get_bearer_token
from client import ItopClient
from config import ITOP_URL, logger
from low_level_resource_types import LowLevelResourceHandler


# ---------------------------------------------------------------------------
# Internal data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttachmentPayload:
    """Raw attachment data ready for MCP transport.

    content holds raw bytes. Base64 encoding is performed by the
    low-level handler in get_low_level_resource_handlers(), not here.
    uri follows the scheme itop://attachment/<filename>.
    """
    attachment_id: str
    uri: str
    mime_type: str
    content: bytes


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _inline_image_url(img_id: "str | int", secret: str) -> str:
    return (
        ITOP_URL + "/webservices/ajax.document.php"
        "?operation=download_inlineimage&id=" + str(img_id) + "&s=" + secret
    )


def _unpack_contents(contents: object) -> tuple:
    """Unpack iTop contents blob into (mimetype, b64_data, filename)."""
    if isinstance(contents, dict):
        return (
            (contents.get("mimetype") or "").strip(),
            (contents.get("data") or ""),
            (contents.get("filename") or ""),
        )
    return "", "", ""


async def _fetch_attachment_content(
    client: ItopClient,
    attachment_id: "str | int",
    mimetype_hint: str = "application/octet-stream",
) -> "tuple[bytes, str]":
    """Fetch attachment binary via iTop REST API. Returns (content_bytes, mimetype)."""
    logger.debug("[attachments] _fetch_attachment_content: id=%s", attachment_id)
    result = await client.get_raw(
        "Attachment",
        str(attachment_id),
        fields="contents",
    )
    objects = result.get("objects") or {}
    if not objects:
        raise ValueError("Attachment id=" + str(attachment_id) + " not found")
    obj = next(iter(objects.values()))
    contents = (obj.get("fields") or {}).get("contents") or {}
    mime, b64data, _ = _unpack_contents(contents)
    if not b64data:
        raise ValueError("No content data for attachment id=" + str(attachment_id))
    mime = mime or mimetype_hint
    logger.debug(
        "[attachments] _fetch_attachment_content: done id=%s mime=%s",
        attachment_id, mime,
    )
    return base64.b64decode(b64data), mime


# ---------------------------------------------------------------------------
# Core payload builder (used by the low-level resource handler)
# ---------------------------------------------------------------------------

async def build_unserved_attachment_payloads(
    client: ItopClient,
) -> list[AttachmentPayload]:
    """Collect all unserved attachments for the current bearer token session.

    Returns a list of AttachmentPayload objects with raw bytes content.
    An empty list is returned (without raising) when:
      - the bearer token cannot be read,
      - no active object is set for the token,
      - all entries are already served or none exist.

    Only successfully processed payloads are marked as served.
    Failed entries (missing cache, download error) are skipped and
    remain available for retry on the next call.
    """
    try:
        token = get_bearer_token()
    except Exception as exc:
        logger.warning(
            "[attachments] build_unserved_attachment_payloads:"
            " get_bearer_token raised: %s",
            exc,
        )
        return []

    if not token:
        return []

    current = get_current_object_for_token(token)
    if current is None:
        logger.debug(
            "[attachments] build_unserved_attachment_payloads: no active object"
        )
        return []

    obj_class, obj_id = current

    # Wait for background image sync to finish before reading cache
    await wait_for_all(token)

    entries = get_unserved_attachment_metadata(token, obj_class, obj_id)
    if not entries:
        logger.debug(
            "[attachments] build_unserved_attachment_payloads: no unserved entries"
        )
        return []

    payloads: list[AttachmentPayload] = []
    served_ids: list[str] = []

    for entry in entries:
        entry_id = entry["id"]
        filename = entry["filename"]
        mime = entry["mimetype"]
        source = entry.get("source", "")
        is_image = (source == "InlineImage" or mime.startswith("image/"))

        if is_image:
            fresh = get_single_attachment_metadata(token, obj_class, obj_id, entry_id)
            content_bytes = (fresh or {}).get("content")
            if content_bytes is None:
                logger.warning(
                    "[attachments] build_unserved_attachment_payloads:"
                    " no content for image id=%s -- skipping",
                    entry_id,
                )
                continue
            if fresh:
                mime = fresh.get("mimetype") or mime
        else:
            try:
                content_bytes, dl_mime = await _fetch_attachment_content(
                    client, entry_id, mime,
                )
                if dl_mime:
                    mime = dl_mime
            except Exception as exc:
                logger.warning(
                    "[attachments] build_unserved_attachment_payloads:"
                    " download failed id=%s: %s -- skipping",
                    entry_id, exc,
                )
                continue

        payloads.append(
            AttachmentPayload(
                attachment_id=entry_id,
                uri="itop://attachment/" + filename,
                mime_type=mime,
                content=content_bytes,
            )
        )
        served_ids.append(entry_id)
        logger.debug(
            "[attachments] build_unserved_attachment_payloads:"
            " queued id=%s filename=%s mime=%s bytes=%d",
            entry_id, filename, mime, len(content_bytes),
        )

    # Mark only successfully built payloads as served
    for sid in served_ids:
        set_served(token, obj_class, obj_id, sid)

    logger.debug(
        "[attachments] build_unserved_attachment_payloads:"
        " %d payload(s), %d skipped",
        len(payloads), len(entries) - len(served_ids),
    )
    return payloads


# ---------------------------------------------------------------------------
# Low-level resource handler factory
# ---------------------------------------------------------------------------

def get_low_level_resource_handlers(
    client: ItopClient,
) -> dict[str, LowLevelResourceHandler]:
    """Return a URI -> handler mapping for server.py to install in the router.

    The handler for itop://attachment/get_attachments builds a full
    types.ServerResult with one types.BlobResourceContents entry per
    unserved attachment, each carrying its own URI and MIME type.
    Base64 encoding happens here, not in build_unserved_attachment_payloads().
    """

    async def _build_get_attachments_result() -> types.ServerResult:
        payloads = await build_unserved_attachment_payloads(client)
        return types.ServerResult(
            types.ReadResourceResult(
                contents=[
                    types.BlobResourceContents(
                        uri=types.AnyUrl(payload.uri),
                        mimeType=payload.mime_type,
                        blob=base64.b64encode(payload.content).decode("ascii"),
                    )
                    for payload in payloads
                ]
            )
        )

    return {
        "itop://attachment/get_attachments": _build_get_attachments_result,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(mcp, client: ItopClient):
    """Register attachment tools and resources on the given mcp instance."""

    # ------------------------------------------------------------------
    # Tool: List_object_attachments
    # ------------------------------------------------------------------

    @mcp.tool(name="List_object_attachments")
    async def list_object_attachments(
        obj_class: str,
        obj_id: int,
    ) -> str:
        """List attachments and inline images for an iTop object.

        Use Resolve_object first for the confirmed obj_class and obj_id. Supports
        UserRequest, Incident, Change, FAQ, FunctionalCI, and similar classes.
        Records metadata without downloading binaries, then starts background image
        download and normalization. A running sync for this object is reused; changing
        objects clears the previous cache and returns a warning.

        Returns each attachment's id, filename, mimetype, and source.

        Routing: for all attachments, all remaining attachments, or every file, read
        get_attachments once. It returns every currently unserved attachment, including
        files not yet retrieved after earlier single-file calls. For one identified file
        (for example by filename or id), call Prepare_single_attachment, then read
        get_single_attachment.
        """
        token = get_bearer_token()
        token_preview = (token[:8] + "...") if token and len(token) > 8 else (token or "(empty)")
        obj_id_str = str(obj_id)

        logger.debug(
            "[attachments] list_object_attachments: token=%s cls=%s id=%s",
            token_preview, obj_class, obj_id_str,
        )

        # -- Same-object guard: skip store write if sync is already running --
        if is_sync_running(token, obj_class, obj_id):
            logger.debug(
                "[attachments] list_object_attachments: sync already running for"
                " cls=%s id=%s -- skipping store reset, returning no-op",
                obj_class, obj_id_str,
            )
            await start_sync(token, obj_class, obj_id, [])
            return (
                "Sync already running for " + obj_class + " " + obj_id_str + "."
                " Read resource get_attachments when ready."
            )

        entries: list[dict] = []

        # -- Attachment records (all MIME types) --
        att_oql = (
            "SELECT Attachment"
            " WHERE item_class = '" + obj_class + "'"
            " AND item_id = " + obj_id_str
        )
        att_result = await client.get("Attachment", att_oql, fields="contents")
        att_objects = att_result.get("objects") or {}
        logger.debug(
            "[attachments] list_object_attachments: Attachment OQL returned %d object(s)",
            len(att_objects),
        )

        for obj_key, obj_data in att_objects.items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, _b64, filename = _unpack_contents(fields.get("contents"))
            if not filename:
                filename = "attachment_" + record_id
            if not mimetype:
                mimetype = "application/octet-stream"
            entries.append({
                "id":           record_id,
                "source":       "Attachment",
                "filename":     filename,
                "mimetype":     mimetype,
                "inline_secret": None,
            })
            logger.debug(
                "[attachments] list_object_attachments: Attachment id=%s mime=%s filename=%s",
                record_id, mimetype, filename,
            )

        # -- InlineImage refs via HTML-parsed cache --
        inline_refs = read_inline_image_refs(obj_class, obj_id_str)
        logger.debug(
            "[attachments] list_object_attachments: inline_image_refs cache %s cls=%r id=%r",
            "hit" if inline_refs is not None else "miss",
            obj_class, obj_id_str,
        )

        if inline_refs is None:
            logger.debug(
                "[attachments] list_object_attachments: fetching object to populate ref cache"
                " cls=%r id=%r", obj_class, obj_id_str,
            )
            from tools.crud import _fetch_and_cache_object
            await _fetch_and_cache_object(obj_class, obj_id_str, client)
            inline_refs = read_inline_image_refs(obj_class, obj_id_str)
            if inline_refs is None:
                inline_refs = []
                write_inline_image_refs(obj_class, obj_id_str, [])

        logger.debug(
            "[attachments] list_object_attachments: %d inline ref(s) cls=%r id=%r",
            len(inline_refs), obj_class, obj_id_str,
        )

        for ref in inline_refs:
            img_id = str(ref.get("id", ""))
            secret = ref.get("secret", "")
            filename = ref.get("filename") or ("inline_" + img_id + ".png")
            mimetype = ref.get("mimetype") or "image/png"
            if not img_id:
                continue
            entries.append({
                "id":            img_id,
                "source":        "InlineImage",
                "filename":      filename,
                "mimetype":      mimetype,
                "inline_secret": secret,
            })
            logger.debug(
                "[attachments] list_object_attachments: InlineImage id=%s mime=%s filename=%s",
                img_id, mimetype, filename,
            )

        # Store metadata (no binaries yet) and start background image sync
        current_token = get_bearer_token()
        prev_object = get_current_object_for_token(current_token)
        warning = ""
        if prev_object is not None and prev_object != (obj_class, obj_id):
            prev_cls, prev_id = prev_object
            warning = (
                " WARNING: previous object " + prev_cls + " " + str(prev_id)
                + " cache cleared."
            )

        store_attachment_metadata(token, obj_class, obj_id, entries)
        sync_warning = await start_sync(token, obj_class, obj_id, entries)

        count = len(entries)
        if count == 0:
            return "No attachments found for " + obj_class + " " + obj_id_str + "." + warning

        lines = [
            "Found " + str(count) + " attachment(s) for "
            + obj_class + " " + obj_id_str + ":" + warning
        ]
        for e in entries:
            lines.append(
                "  id=" + e["id"]
                + " source=" + e["source"]
                + " filename=" + e["filename"]
                + " mime=" + e["mimetype"]
            )
        if sync_warning:
            lines.append(sync_warning)
        lines.append(
            "Read get_attachments once for all or all remaining attachments. "
            "Use Prepare_single_attachment and get_single_attachment only for one identified file."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool: Prepare_single_attachment
    # ------------------------------------------------------------------

    @mcp.tool(name="Prepare_single_attachment")
    async def prepare_single_attachment(
        obj_class: str,
        obj_id: int,
        id: str,
    ) -> str:
        """Mark a single attachment for retrieval via get_single_attachment.

        Call List_object_attachments first to populate the store and obtain
        the attachment id from the listing. Then call this tool with the
        desired id, and read the get_single_attachment resource.

        obj_class: iTop class of the parent object (e.g. UserRequest).
        obj_id:    Integer database ID of the parent object.
        id:        Attachment id as returned by List_object_attachments.
        """
        token = get_bearer_token()
        obj_id_str = str(obj_id)

        logger.debug(
            "[attachments] prepare_single_attachment: token=... cls=%s id=%s att_id=%s",
            obj_class, obj_id_str, id,
        )

        entry = get_single_attachment_metadata(token, obj_class, obj_id, id)
        if entry is None:
            return (
                "Attachment id=" + id + " not found in store for "
                + obj_class + " " + obj_id_str + "."
                " Call List_object_attachments first."
            )

        set_selected(token, obj_class, obj_id, id)
        logger.debug(
            "[attachments] prepare_single_attachment: selected id=%s filename=%s",
            id, entry.get("filename", ""),
        )
        return (
            "Attachment id=" + id + " selected ("
            + entry.get("filename", "") + ", "
            + entry.get("mimetype", "") + ")."
            " Read resource get_single_attachment to retrieve the binary."
        )

    # ------------------------------------------------------------------
    # Resource: get_attachments (stub -- actual read via low-level router)
    # ------------------------------------------------------------------

    @mcp.resource(
        "itop://attachment/get_attachments",
        name="Get all remaining attachments",
        description=(
            "PREFERRED for requests to get all attachments, all remaining attachments, "
            "or every file for the current object. It returns all currently unserved "
            "attachments in one multi-entry contents array; do not make repeated "
            "get_single_attachment calls for this use case. "
            "Images are served from cache (waits for background sync if still running). "
            "Non-image attachments are fetched live via REST API. "
            "Each attachment is one entry in the contents array with its own "
            "uri (itop://attachment/<filename>), mimeType and blob. "
            "Previously served attachments (including those returned by "
            "get_single_attachment) are excluded. "
            "Only successfully returned attachments are marked as served; "
            "failed entries remain available for retry on the next call."
        ),
        mime_type="application/octet-stream",
    )
    async def get_attachments() -> list[dict]:
        """Stub: keeps the resource visible in resources/list.

        The actual multi-file response is produced by the low-level router
        installed in server.py. This stub is never called for a resources/read
        of itop://attachment/get_attachments because the router intercepts first.
        """
        return []

    # ------------------------------------------------------------------
    # Resource: get_single_attachment
    # ------------------------------------------------------------------

    @mcp.resource(
        "itop://attachment/get_single_attachment",
        name="Get one selected attachment",
        description=(
            "Use this only when one specific attachment is needed, for example when "
            "a filename or attachment id identifies the requested file. Do not use it "
            "repeatedly to retrieve all or all remaining attachments; use the "
            "get_attachments resource instead, which returns all unserved attachments "
            "in one multi-entry response. "
            "Downloads and returns the single attachment marked by Prepare_single_attachment. "
            "You MUST call List_object_attachments and then Prepare_single_attachment "
            "before reading this resource. "
            "Image is served from cache (waits for background sync if still running). "
            "Non-image attachment is fetched live via REST API. "
            "Marks the returned attachment as served."
        ),
        mime_type="application/octet-stream",
    )
    async def get_single_attachment() -> list[dict]:
        """Serve the attachment marked as selected for the current bearer token session."""
        logger.debug("[attachments] get_single_attachment: resource handler invoked")

        try:
            token = get_bearer_token()
        except Exception as exc:
            logger.warning(
                "[attachments] get_single_attachment: get_bearer_token raised: %s", exc
            )
            return []

        if not token:
            return []

        current = get_current_object_for_token(token)
        if current is None:
            logger.debug("[attachments] get_single_attachment: no active object for token")
            return []

        obj_class, obj_id = current
        entry = get_selected_attachment_metadata(token, obj_class, obj_id)
        if entry is None:
            logger.debug("[attachments] get_single_attachment: no selected entry")
            return []

        entry_id = entry["id"]
        filename = entry["filename"]
        mime = entry["mimetype"]
        source = entry.get("source", "")
        is_image = (source == "InlineImage" or mime.startswith("image/"))

        if is_image:
            await wait_for_image(token, entry_id)
            fresh = get_single_attachment_metadata(token, obj_class, obj_id, entry_id)
            content_bytes = (fresh or {}).get("content")
            if content_bytes is None:
                logger.warning(
                    "[attachments] get_single_attachment: no content for image id=%s",
                    entry_id,
                )
                return []
            if fresh:
                mime = fresh.get("mimetype") or mime
        else:
            try:
                content_bytes, dl_mime = await _fetch_attachment_content(
                    client, entry_id, mime,
                )
                if dl_mime:
                    mime = dl_mime
            except Exception as exc:
                logger.warning(
                    "[attachments] get_single_attachment: download failed id=%s: %s",
                    entry_id, exc,
                )
                return []

        set_served(token, obj_class, obj_id, entry_id)
        logger.debug(
            "[attachments] get_single_attachment: serving id=%s filename=%s mime=%s bytes=%d",
            entry_id, filename, mime, len(content_bytes),
        )
        return [{
            "uri": "itop://attachment/" + filename,
            "blob": base64.b64encode(content_bytes).decode(),
            "mimeType": mime,
        }]
