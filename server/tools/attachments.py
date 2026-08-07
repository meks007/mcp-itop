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
            (waits for background sync). Non-images are downloaded live.
            Only successfully returned attachments are marked served;
            failed entries remain served=0 and are retried on next call.

        itop://attachment/get_single_attachment
            Downloads and returns the single attachment marked by
            Prepare_single_attachment. Images served from cache,
            non-images downloaded live. Marks the record as served.

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

import httpx
from fastmcp.resources import ResourceResult, ResourceContent
from mcp.types import BlobResourceContents

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
from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, logger


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _attachment_url(attachment_id: "str | int") -> str:
    return (
        ITOP_URL + "/webservices/ajax.document.php"
        "?operation=download_document&class=Attachment&field=contents&id="
        + str(attachment_id)
    )


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


async def _download_binary(url: str) -> "tuple[bytes, str]":
    """Download binary content from url. Returns (content_bytes, mimetype)."""
    logger.debug("[attachments] _download_binary: GET %s", url)
    async with httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT) as http:
        response = await http.get(url)
        logger.debug(
            "[attachments] _download_binary: status=%d content-type=%s",
            response.status_code,
            response.headers.get("content-type", "(none)"),
        )
        response.raise_for_status()
        ct = response.headers.get("content-type", "application/octet-stream")
        mimetype = ct.split(";")[0].strip()
        logger.debug(
            "[attachments] _download_binary: done url=%s mime=%s bytes=%d",
            url, mimetype, len(response.content),
        )
        return response.content, mimetype


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
        """List all attachments and inline images for an iTop object.

        Use Resolve_object first to obtain the confirmed obj_class and obj_id.
        Works for any iTop class that supports attachments:
        UserRequest, Incident, Change, FAQ, FunctionalCI, etc.

        Fetches Attachment records via OQL and resolves inline image refs from
        the HTML field cache. Writes metadata to the session store; no binaries
        are downloaded at this stage. Starts a background task that downloads
        and normalizes all image attachments into the cache.

        If a sync for the same object is already running, the store is not
        reset and start_sync() returns as a no-op.

        If called for a different object while a previous sync is still running,
        the previous cache is cleared and a warning is included in the response.

        Returns one entry per attachment with: id, filename, mimetype, source.

        To retrieve all attachments at once: read resource get_attachments.
        To retrieve a single attachment: call Prepare_single_attachment with the
        desired id, then read resource get_single_attachment.
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
            warning = await start_sync(token, obj_class, obj_id, [])
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
            img_id = ref["id"]
            secret = ref["secret"]
            entries.append({
                "id":           img_id,
                "source":       "InlineImage",
                "filename":     "inlineimage_" + img_id + ".jpg",
                "mimetype":     "image/jpeg",
                "inline_secret": secret,
            })

        # -- Persist metadata and start background sync --
        store_attachment_metadata(token, obj_class, obj_id, entries)
        warning = await start_sync(token, obj_class, obj_id, entries)

        # -- Build response --
        if not entries:
            return (
                "No attachments found for " + obj_class + " " + obj_id_str + "."
            )

        lines = [
            "Attachments for " + obj_class + " " + obj_id_str
            + " (" + str(len(entries)) + " found):"
        ]
        for e in entries:
            lines.append("\n--- " + e["filename"] + " ---")
            lines.append("  id      : " + e["id"])
            lines.append("  source  : " + e["source"])
            lines.append("  mimetype: " + e["mimetype"])

        lines.append(
            "\nTo retrieve all attachments: read resource get_attachments."
        )
        lines.append(
            "To retrieve a single attachment: call Prepare_single_attachment(id=<id>),"
            " then read resource get_single_attachment."
        )

        if warning:
            lines.append("\nWarning: " + warning)

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

        Call List_object_attachments first to populate the metadata store and
        obtain the attachment id from the listing.

        Sets the selected flag on the record matching id so that
        get_single_attachment knows which attachment to download and return.
        No binary is downloaded by this tool.

        After this tool returns, read resource get_single_attachment.
        """
        token = get_bearer_token()
        entry = get_single_attachment_metadata(token, obj_class, obj_id, id)
        if entry is None:
            return (
                "No attachment found for id=" + id + " on "
                + obj_class + " obj_id=" + str(obj_id)
                + ". Call List_object_attachments first."
            )
        set_selected(token, obj_class, obj_id, id)
        return (
            "Attachment " + entry["filename"] + " (id=" + id
            + ") selected. Read resource get_single_attachment to retrieve it."
        )

    # ------------------------------------------------------------------
    # Resource: get_attachments
    # ------------------------------------------------------------------

    @mcp.resource(
        "itop://attachment/get_attachments",
        name="Get all attachments",
        description=(
            "Downloads and returns all unserved attachments for the object "
            "prepared by List_object_attachments. "
            "Call List_object_attachments first. "
            "Images are served from cache (waits for background sync if still running). "
            "Non-image attachments are downloaded live from iTop. "
            "Each attachment is one entry in the contents array with its own "
            "uri (itop://attachment/<filename>), mimeType and blob. "
            "Previously served attachments (via get_single_attachment) are excluded. "
            "Only successfully returned attachments are marked as served; "
            "failed entries remain available for retry on the next call."
        ),
        mime_type="application/octet-stream",
    )
    async def get_attachments() -> ResourceResult:
        """Serve all unserved attachments for the current bearer token session."""
        logger.debug("[attachments] get_attachments: resource handler invoked")

        try:
            token = get_bearer_token()
        except Exception as exc:
            logger.warning("[attachments] get_attachments: get_bearer_token raised: %s", exc)
            return ResourceResult(contents=[])

        if not token:
            return ResourceResult(contents=[])

        current = get_current_object_for_token(token)
        if current is None:
            logger.debug("[attachments] get_attachments: no active object for token")
            return ResourceResult(contents=[])

        obj_class, obj_id = current

        # Wait for background image sync to finish
        await wait_for_all(token)

        entries = get_unserved_attachment_metadata(token, obj_class, obj_id)
        if not entries:
            logger.debug("[attachments] get_attachments: no unserved entries")
            return ResourceResult(contents=[])

        contents = []
        served_ids: list[str] = []

        for entry in entries:
            entry_id = entry["id"]
            filename = entry["filename"]
            mime = entry["mimetype"]
            source = entry.get("source", "")
            is_image = (source == "InlineImage" or mime.startswith("image/"))

            if is_image:
                # Re-fetch to get content written by sync task
                fresh = get_single_attachment_metadata(token, obj_class, obj_id, entry_id)
                content_bytes = (fresh or {}).get("content")
                if content_bytes is None:
                    logger.warning(
                        "[attachments] get_attachments: no content for image id=%s"
                        " -- skipping, will remain unserved",
                        entry_id,
                    )
                    continue
                if fresh:
                    mime = fresh.get("mimetype") or mime
            else:
                try:
                    url = _attachment_url(entry_id)
                    content_bytes, dl_mime = await _download_binary(url)
                    if dl_mime:
                        mime = dl_mime
                except Exception as exc:
                    logger.warning(
                        "[attachments] get_attachments: download failed id=%s: %s"
                        " -- skipping, will remain unserved",
                        entry_id, exc,
                    )
                    continue

            contents.append(
                ResourceContent(
                    BlobResourceContents(
                        uri="itop://attachment/" + filename,
                        blob=base64.b64encode(content_bytes).decode(),
                        mimeType=mime,
                    )
                )
            )
            served_ids.append(entry_id)
            logger.debug(
                "[attachments] get_attachments: serving id=%s filename=%s mime=%s bytes=%d",
                entry_id, filename, mime, len(content_bytes),
            )

        # Mark only successfully returned attachments as served
        for sid in served_ids:
            set_served(token, obj_class, obj_id, sid)

        logger.debug(
            "[attachments] get_attachments: returning %d content(s), %d skipped",
            len(contents), len(entries) - len(served_ids),
        )
        return ResourceResult(contents=contents)

    # ------------------------------------------------------------------
    # Resource: get_single_attachment
    # ------------------------------------------------------------------

    @mcp.resource(
        "itop://attachment/get_single_attachment",
        name="Get single attachment",
        description=(
            "Downloads and returns the single attachment marked by Prepare_single_attachment. "
            "You MUST call List_object_attachments and then Prepare_single_attachment "
            "before reading this resource. "
            "Image is served from cache (waits for background sync if still running). "
            "Non-image attachment is downloaded live from iTop. "
            "Marks the returned attachment as served."
        ),
        mime_type="application/octet-stream",
    )
    async def get_single_attachment() -> ResourceResult:
        """Serve the attachment marked as selected for the current bearer token session."""
        logger.debug("[attachments] get_single_attachment: resource handler invoked")

        try:
            token = get_bearer_token()
        except Exception as exc:
            logger.warning(
                "[attachments] get_single_attachment: get_bearer_token raised: %s", exc
            )
            return ResourceResult(contents=[])

        if not token:
            return ResourceResult(contents=[])

        current = get_current_object_for_token(token)
        if current is None:
            logger.debug("[attachments] get_single_attachment: no active object for token")
            return ResourceResult(contents=[])

        obj_class, obj_id = current
        entry = get_selected_attachment_metadata(token, obj_class, obj_id)
        if entry is None:
            logger.debug("[attachments] get_single_attachment: no selected entry")
            return ResourceResult(contents=[])

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
                return ResourceResult(contents=[])
            if fresh:
                mime = fresh.get("mimetype") or mime
        else:
            try:
                url = _attachment_url(entry_id)
                content_bytes, dl_mime = await _download_binary(url)
                if dl_mime:
                    mime = dl_mime
            except Exception as exc:
                logger.warning(
                    "[attachments] get_single_attachment: download failed id=%s: %s",
                    entry_id, exc,
                )
                return ResourceResult(contents=[])

        set_served(token, obj_class, obj_id, entry_id)
        logger.debug(
            "[attachments] get_single_attachment: serving id=%s filename=%s mime=%s bytes=%d",
            entry_id, filename, mime, len(content_bytes),
        )
        return ResourceResult(
            contents=[
                ResourceContent(
                    BlobResourceContents(
                        uri="itop://attachment/" + filename,
                        blob=base64.b64encode(content_bytes).decode(),
                        mimeType=mime,
                    )
                )
            ]
        )
