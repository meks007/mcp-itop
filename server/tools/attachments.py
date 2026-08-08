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
            task that downloads and normalises image binaries.
            Works for UserRequest, Incident, Change, FAQ, FunctionalCI, etc.

            Same-object guard: if a sync for the same (obj_class, obj_id)
            is already running, the store is NOT reset and start_sync()
            returns as a no-op. is_sync_running() is checked before any
            store write to avoid erasing cached image content.

            After listing, call Prepare_object_attachments with the IDs
            you want to retrieve (all IDs for everything, a subset for
            specific files). Then read get_object_attachments.

            Image MIME type note: all image attachments are normalised to
            JPEG by the background sync task. The listing reports the
            effective stored MIME type (image/jpeg) and the .jpg filename
            immediately, so the agent does not need to wait for the sync.

        Prepare_object_attachments(obj_class, obj_id, ids)
            Accepts one or more attachment IDs as returned by
            List_object_attachments. Marks exactly those attachments as
            prepared. A subsequent read of get_object_attachments serves
            only the prepared IDs and nothing else.
            Call this tool again to change the selection before re-reading.

    Resources:
        itop://attachment/get_object_attachments
            Serves all attachments that were prepared by
            Prepare_object_attachments. Returns a multi-entry contents
            array; each entry carries its own uri, mimeType and blob.
            Images are served from the background-sync cache.
            Non-image attachments are fetched live via iTop REST API.
            Only successfully returned attachments are marked served;
            failed entries remain prepared and can be retried.
            The transport response is produced by the low-level router
            installed in server.py via get_low_level_resource_handlers().

get_low_level_resource_handlers(client)
    Returns a dict mapping resource URIs to low-level handler callables.
    Used by server.py to install the central ReadResourceRequest router.

Image normalisation
-------------------
All image attachments (source InlineImage or mimetype starting with image/)
are converted to JPEG by attachment_store/image.py regardless of their
original format. The background sync stores the converted bytes and updates
the mimetype column to image/jpeg. The filename extension is changed to .jpg.
List_object_attachments reports image/jpeg and the .jpg filename immediately
so the agent always sees the effective stored format without waiting for sync.

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
    get_single_attachment_metadata,
    get_prepared_attachment_metadata,
    get_current_object_for_token,
    set_prepared,
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

_RESOURCE_URI = "itop://attachment/get_object_attachments"

# MIME type and extension that the image normalisation pipeline always produces.
_IMAGE_NORMALISED_MIME = "image/jpeg"
_IMAGE_NORMALISED_EXT = ".jpg"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_image_entry(entry: dict) -> bool:
    """Return True when the entry will be normalised to JPEG by attachment_sync."""
    return (
        entry.get("source") == "InlineImage"
        or (entry.get("mimetype") or "").startswith("image/")
    )


def _normalised_filename(filename: str) -> str:
    """Return filename with extension replaced by .jpg."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem + _IMAGE_NORMALISED_EXT


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

async def build_prepared_attachment_payloads(
    client: ItopClient,
) -> list[AttachmentPayload]:
    """Collect all prepared attachments for the current bearer token session.

    Reads the IDs that were marked by Prepare_object_attachments (selected=1)
    and retrieves their binaries. Returns a list of AttachmentPayload objects
    with raw bytes content.

    An empty list is returned (without raising) when:
      - the bearer token cannot be read,
      - no active object is set for the token,
      - no attachments have been prepared.

    Only successfully processed payloads are marked as served.
    Failed entries (missing cache, download error) are skipped and
    remain prepared so they can be retried on the next call.
    """
    try:
        token = get_bearer_token()
    except Exception as exc:
        logger.warning(
            "[attachments] build_prepared_attachment_payloads:"
            " get_bearer_token raised: %s",
            exc,
        )
        return []

    if not token:
        return []

    current = get_current_object_for_token(token)
    if current is None:
        logger.debug(
            "[attachments] build_prepared_attachment_payloads: no active object"
        )
        return []

    obj_class, obj_id = current
    entries = get_prepared_attachment_metadata(token, obj_class, obj_id)

    if not entries:
        logger.debug(
            "[attachments] build_prepared_attachment_payloads: no prepared entries"
        )
        return []

    # Wait for background sync before reading image cache entries.
    has_image = any(_is_image_entry(e) for e in entries)
    if has_image:
        await wait_for_all(token)

    payloads: list[AttachmentPayload] = []
    served_ids: list[str] = []

    for entry in entries:
        entry_id = entry["id"]
        filename = entry["filename"]
        mime = entry["mimetype"]

        if _is_image_entry(entry):
            if not has_image:
                await wait_for_image(token, entry_id)
            fresh = get_single_attachment_metadata(token, obj_class, obj_id, entry_id)
            content_bytes = (fresh or {}).get("content")
            if content_bytes is None:
                logger.warning(
                    "[attachments] build_prepared_attachment_payloads:"
                    " no content for image id=%s -- skipping",
                    entry_id,
                )
                continue
            if fresh:
                mime = fresh.get("mimetype") or mime
                filename = fresh.get("filename") or filename
        else:
            try:
                content_bytes, dl_mime = await _fetch_attachment_content(
                    client, entry_id, mime,
                )
                if dl_mime:
                    mime = dl_mime
            except Exception as exc:
                logger.warning(
                    "[attachments] build_prepared_attachment_payloads:"
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
            "[attachments] build_prepared_attachment_payloads:"
            " queued id=%s filename=%s mime=%s bytes=%d",
            entry_id, filename, mime, len(content_bytes),
        )

    for sid in served_ids:
        set_served(token, obj_class, obj_id, sid)

    logger.debug(
        "[attachments] build_prepared_attachment_payloads:"
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

    The handler for itop://attachment/get_object_attachments builds a full
    types.ServerResult with one types.BlobResourceContents entry per prepared
    attachment, each carrying its own URI and MIME type.
    Base64 encoding happens here, not in build_prepared_attachment_payloads().
    """

    async def _build_get_object_attachments_result() -> types.ServerResult:
        payloads = await build_prepared_attachment_payloads(client)
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
        _RESOURCE_URI: _build_get_object_attachments_result,
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
        download and normalisation. A running sync for this object is reused; changing
        objects clears the previous cache and returns a warning.

        Returns each attachment's id, filename, mimetype, and source. Image attachments
        are always normalised to JPEG by the background sync task. The listing reports
        image/jpeg and the .jpg filename immediately so the values match what will
        actually be served via get_object_attachments.

        After listing, call Prepare_object_attachments with the IDs you want:
        pass all IDs to get every attachment, or a subset for specific files.
        Then read get_object_attachments to retrieve the binaries.
        """
        token = get_bearer_token()
        token_preview = (token[:8] + "...") if token and len(token) > 8 else (token or "(empty)")
        obj_id_str = str(obj_id)

        logger.debug(
            "[attachments] list_object_attachments: token=%s cls=%s id=%s",
            token_preview, obj_class, obj_id_str,
        )

        if is_sync_running(token, obj_class, obj_id):
            logger.debug(
                "[attachments] list_object_attachments: sync already running for"
                " cls=%s id=%s -- skipping store reset, returning no-op",
                obj_class, obj_id_str,
            )
            await start_sync(token, obj_class, obj_id, [])
            return (
                "Sync already running for " + obj_class + " " + obj_id_str + "."
                " Call Prepare_object_attachments with the IDs you want,"
                " then read get_object_attachments."
            )

        entries: list[dict] = []

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
            # Images are always converted to JPEG by the background sync task.
            # Report the effective stored MIME type and filename so the agent
            # sees values that match what get_object_attachments will serve.
            if _is_image_entry(e):
                disp_mime = _IMAGE_NORMALISED_MIME
                disp_filename = _normalised_filename(e["filename"])
            else:
                disp_mime = e["mimetype"]
                disp_filename = e["filename"]
            lines.append(
                "  id=" + e["id"]
                + " source=" + e["source"]
                + " filename=" + disp_filename
                + " mime=" + disp_mime
            )
        if sync_warning:
            lines.append(sync_warning)
        lines.append(
            "Call Prepare_object_attachments with the IDs you want"
            " (all IDs for everything, a subset for specific files),"
            " then read get_object_attachments."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool: Prepare_object_attachments
    # ------------------------------------------------------------------

    @mcp.tool(name="Prepare_object_attachments")
    async def prepare_object_attachments(
        obj_class: str,
        obj_id: int,
        ids: list[str],
    ) -> str:
        """Mark one or more attachments for retrieval via get_object_attachments.

        Call List_object_attachments first to populate the store and obtain
        the attachment IDs from the listing. Then pass the IDs you want:
          - Pass all listed IDs to retrieve every attachment.
          - Pass a subset to retrieve only specific files.

        A subsequent read of get_object_attachments serves exactly the
        prepared IDs and nothing else. Call this tool again to change the
        selection before re-reading.

        obj_class: iTop class of the parent object (e.g. UserRequest).
        obj_id:    Integer database ID of the parent object.
        ids:       One or more attachment IDs from List_object_attachments.
        """
        token = get_bearer_token()
        obj_id_str = str(obj_id)

        if not ids:
            return (
                "No IDs provided. Pass at least one attachment ID"
                " from List_object_attachments."
            )

        logger.debug(
            "[attachments] prepare_object_attachments: token=... cls=%s id=%s ids=%s",
            obj_class, obj_id_str, ids,
        )

        valid: list[str] = []
        invalid: list[str] = []
        for att_id in ids:
            entry = get_single_attachment_metadata(token, obj_class, obj_id, att_id)
            if entry is None:
                invalid.append(att_id)
            else:
                valid.append(att_id)

        if invalid:
            return (
                "Unknown attachment ID(s): " + ", ".join(invalid) + "."
                " Call List_object_attachments first to populate the store."
            )

        set_prepared(token, obj_class, obj_id, valid)

        lines = [
            "Prepared " + str(len(valid)) + " attachment(s) for "
            + obj_class + " " + obj_id_str + ":"
        ]
        for att_id in valid:
            entry = get_single_attachment_metadata(token, obj_class, obj_id, att_id)
            raw_mime = (entry or {}).get("mimetype", "")
            raw_filename = (entry or {}).get("filename", "")
            # Report the effective JPEG mime/filename for image entries.
            if entry and _is_image_entry(entry):
                disp_mime = _IMAGE_NORMALISED_MIME
                disp_filename = _normalised_filename(raw_filename)
            else:
                disp_mime = raw_mime
                disp_filename = raw_filename
            lines.append(
                "  id=" + att_id
                + " filename=" + disp_filename
                + " mime=" + disp_mime
            )
        lines.append("Read get_object_attachments to retrieve the binaries.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Resource: get_object_attachments (stub -- actual read via low-level router)
    # ------------------------------------------------------------------

    @mcp.resource(
        _RESOURCE_URI,
        name="Get prepared attachments",
        description=(
            "Returns the attachments that were prepared by Prepare_object_attachments. "
            "You MUST call List_object_attachments and then Prepare_object_attachments "
            "before reading this resource. "
            "Pass all listed IDs to Prepare_object_attachments to get every attachment; "
            "pass a subset to get only specific files. "
            "Each prepared attachment is returned as one entry in the contents array "
            "with its own uri (itop://attachment/<filename>), mimeType, and blob. "
            "Images are served as JPEG from the background-sync cache. "
            "Non-image attachments are fetched live via the iTop REST API. "
            "Only successfully returned attachments are marked as served; "
            "failed entries remain prepared and can be retried by reading again."
        ),
        mime_type="application/octet-stream",
    )
    async def get_object_attachments() -> list[dict]:
        """Stub: keeps the resource visible in resources/list.

        The actual multi-file response is produced by the low-level router
        installed in server.py. This stub is never called for a resources/read
        of itop://attachment/get_object_attachments because the router intercepts first.
        """
        return []
