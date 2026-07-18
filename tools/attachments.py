"""
Attachment tools: fetch image metadata and download attachments as files.

Public API
----------
register(mcp, client, get_token_fn)
    Registers the following MCP tools and resources:

    Tools:
        itop_get_ticket_images(obj_class, ticket_ref, key)
            Fetches image attachments for a ticket, stores them in the
            SQLite attachment store, and returns only the image count plus
            the static MCP resource URI _STATIC_RESOURCE_URI.
            The client must read that resource to retrieve the actual images.

            Inline images are resolved exclusively from refs parsed out of
            the ticket HTML fields by format_and_cache() (via parse_objects).
            The core/get InlineImage approach is intentionally NOT used
            because iTop does not delete InlineImage records when the
            corresponding <img> tag is removed from a ticket text field,
            leading to stale/ghost images being returned.

            Cache behaviour:
              - Cache hit  (inline_image_refs table) : download and store.
              - Cache miss : call _fetch_and_cache_ticket() which runs
                             format_and_cache() and populates the cache,
                             then read the cache. If still empty the ticket
                             has no inline images.

        itop_get_ticket_attachments(obj_class, ticket_ref, key)
            List all non-image file attachments for a ticket.
            Returns metadata and browser download links only.

    Resources:
        _STATIC_RESOURCE_URI  (static)
            Returns all images stored by the most recent
            itop_get_ticket_images call for this client session as a
            multi-content ResourceResult (one ResourceContent per image).
            All images are always served as JPEG from the BLOB store.

iTop blob field notes
---------------------
The contents AttributeBlob is returned by the REST API as a dict:
  {"mimetype": "<mime>", "data": "<base64>", "filename": "<name>"}

Attachment  : may be any MIME type; mimetype is checked before including.
              The base64 payload is decoded to bytes immediately and stored
              as a BLOB in the attachment store. The uri column holds only
              the short itop://attachment/<id> reference.
              ajax.document.php requires an active iTop session (cookie) and
              does NOT accept the bearer token, so binary data is taken
              directly from the REST API response without a second HTTP call.
InlineImage : resolved from <img data-img-id data-img-secret> tags found in
              ticket HTML fields after format_and_cache() has run. Secret and
              id are read from the inline_image_refs SQLite cache. Download
              uses /webservices/ajax.document.php (no session cookie required).
              Content is downloaded eagerly so all entries in the store always
              carry a non-None BLOB.
"""

from __future__ import annotations

import base64 as _base64
import hashlib
import httpx
from fastmcp.resources import ResourceResult, ResourceContent

from attachment_store import (
    get_images,
    read_inline_image_refs,
    store_images,
    write_inline_image_refs,
)
from client import ItopClient
from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger
from helpers import coerce_ref, resolve_key
from tools.crud import _fetch_and_cache_ticket

_IMAGE_PREFIXES = ("image/",)

_STATIC_RESOURCE_URI = "itop://attachment/image.jpg"


def _is_image(mimetype: str) -> bool:
    ct = mimetype.split(";")[0].strip().lower()
    return any(ct.startswith(p) for p in _IMAGE_PREFIXES)


def _attachment_url(attachment_id: str | int) -> str:
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_document&class=Attachment&field=contents&id={attachment_id}"
    )


def _inline_image_url(img_id: str | int, secret: str) -> str:
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_inlineimage&id={img_id}&s={secret}"
    )


def _unpack_contents(contents: object) -> tuple:
    """Unpack iTop contents blob into (mimetype, b64_data, filename).

    iTop serialises AttributeBlob as:
      {"mimetype": "image/jpeg", "data": "<base64>", "filename": "foo.jpg"}
    Returns empty strings for any missing key.
    """
    if isinstance(contents, dict):
        return (
            (contents.get("mimetype") or "").strip(),
            (contents.get("data") or ""),
            (contents.get("filename") or ""),
        )
    return "", "", ""


async def _download_binary(url: str) -> tuple[bytes, str]:
    """Download binary content from url.

    Returns (content_bytes, mimetype).
    mimetype is taken from the Content-Type response header, falling back to
    application/octet-stream.
    """
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


def register(mcp, client: ItopClient, get_token_fn):
    """Register attachment tools and the static image resource.

    Args:
        mcp:          FastMCP server instance.
        client:       ItopClient instance for REST requests.
        get_token_fn: Zero-argument callable returning the current bearer
                      token string for the active MCP client session.
    """

    # ------------------------------------------------------------------
    # Tool: itop_get_ticket_images
    # ------------------------------------------------------------------

    @mcp.tool(
        name="List_ticket_images"
    )
    async def itop_get_ticket_images(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """Find image attachments for an iTop ticket and store them for the current session.

        Prefer ticket_ref for tickets; use key for a numeric ID or OQL query.
        After this tool returns, read the MCP resource itop://attachment/image.jpg
        to retrieve the actual image binaries."""
        logger.debug(
            "[attachments] itop_get_ticket_images: called obj_class=%s ticket_ref=%r key=%r",
            obj_class, ticket_ref, key,
        )

        ref = coerce_ref(ticket_ref, key)
        obj_class, resolved = await resolve_key(obj_class, ref, client.request)
        logger.debug(
            "[attachments] itop_get_ticket_images: resolved class=%r key=%r",
            obj_class, resolved,
        )

        if resolved is None:
            return "Error: provide either ticket_ref or key to identify the ticket."

        obj_id = str(resolved)
        images = []

        # -- Attachment (image types only) --
        att_oql = (
            "SELECT Attachment"
            " WHERE item_class = '" + obj_class + "'"
            " AND item_id = " + obj_id
        )
        att_result = await client.get("Attachment", att_oql, fields="contents")
        att_objects = att_result.get("objects") or {}
        logger.debug(
            "[attachments] itop_get_ticket_images: Attachment query returned %d object(s)",
            len(att_objects),
        )

        for obj_key, obj_data in att_objects.items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, b64_data, filename = _unpack_contents(fields.get("contents"))
            if not _is_image(mimetype):
                continue
            if not filename:
                filename = "attachment_" + record_id
            uri = "itop://attachment/" + record_id
            content: bytes | None = None
            if b64_data:
                try:
                    content = _base64.b64decode(b64_data)
                    logger.debug(
                        "[attachments] itop_get_ticket_images: decoded Attachment"
                        " record_id=%s mime=%s bytes=%d",
                        record_id, mimetype, len(content),
                    )
                except Exception as exc:
                    logger.warning(
                        "[attachments] itop_get_ticket_images: base64 decode failed"
                        " record_id=%s exc=%s",
                        record_id, exc,
                    )
            images.append({
                "source": "Attachment",
                "filename": filename,
                "mimetype": mimetype,
                "uri": uri,
                "content": content,
                "_b64": b64_data,
            })
            logger.debug(
                "[attachments] itop_get_ticket_images: added Attachment"
                " record_id=%s uri=%s content=%s",
                record_id, uri,
                ("%d bytes" % len(content)) if content is not None else "None",
            )

        # -- InlineImage via HTML-parsed refs cache --
        inline_refs = read_inline_image_refs(obj_class, obj_id)
        logger.debug(
            "[attachments] itop_get_ticket_images: inline_image_refs cache %s for cls=%r id=%r",
            "hit" if inline_refs is not None else "miss",
            obj_class, obj_id,
        )

        if inline_refs is None:
            logger.debug(
                "[attachments] itop_get_ticket_images: fetching ticket cls=%r id=%r"
                " to populate inline image ref cache",
                obj_class, obj_id,
            )
            await _fetch_and_cache_ticket(obj_class, obj_id, client)
            inline_refs = read_inline_image_refs(obj_class, obj_id)
            if inline_refs is None:
                inline_refs = []
                write_inline_image_refs(obj_class, obj_id, [])

        logger.debug(
            "[attachments] itop_get_ticket_images: %d inline image ref(s) for cls=%r id=%r",
            len(inline_refs), obj_class, obj_id,
        )

        for ref_entry in inline_refs:
            img_id = ref_entry["id"]
            secret = ref_entry["secret"]
            filename = "inlineimage_" + img_id + ".jpg"
            uri = "itop://inlineimage/" + secret + "/" + img_id
            mimetype = "image/jpeg"
            content = None
            try:
                url = _inline_image_url(img_id, secret)
                content, dl_mime = await _download_binary(url)
                if dl_mime and dl_mime != "application/octet-stream":
                    mimetype = dl_mime
                logger.debug(
                    "[attachments] itop_get_ticket_images: downloaded InlineImage"
                    " img_id=%s mime=%s bytes=%d",
                    img_id, mimetype, len(content),
                )
            except Exception as exc:
                logger.warning(
                    "[attachments] itop_get_ticket_images: InlineImage download failed"
                    " img_id=%s exc=%s",
                    img_id, exc,
                )
            images.append({
                "source": "InlineImage",
                "filename": filename,
                "mimetype": mimetype,
                "uri": uri,
                "content": content,
                "_b64": "",
            })
            logger.debug(
                "[attachments] itop_get_ticket_images: added InlineImage"
                " img_id=%s uri=%s content=%s",
                img_id, uri,
                ("%d bytes" % len(content)) if content is not None else "None",
            )

        logger.debug(
            "[attachments] itop_get_ticket_images: total images collected=%d", len(images)
        )

        # Deduplicate by img_id for InlineImages and by SHA-256 of base64 for Attachments.
        seen_hashes: set[str] = set()
        seen_inline_ids: set[str] = set()
        unique_images = []
        for img in images:
            if img.get("source") == "InlineImage":
                img_id_str = img.get("uri", "").split("/")[-1]
                if img_id_str in seen_inline_ids:
                    logger.debug(
                        "[attachments] itop_get_ticket_images: skipping duplicate InlineImage"
                        " img_id=%s", img_id_str,
                    )
                    continue
                seen_inline_ids.add(img_id_str)
            else:
                b64 = img.get("_b64") or ""
                if b64:
                    digest = hashlib.sha256(b64.encode("ascii", errors="replace")).hexdigest()
                    if digest in seen_hashes:
                        logger.debug(
                            "[attachments] itop_get_ticket_images: skipping duplicate"
                            " digest=%s filename=%s uri=%s",
                            digest[:12], img.get("filename"), img.get("uri"),
                        )
                        continue
                    seen_hashes.add(digest)

            content_bytes = img.get("content")
            if content_bytes:
                content_digest = hashlib.sha256(content_bytes).hexdigest()
                if content_digest in seen_hashes:
                    logger.debug(
                        "[attachments] itop_get_ticket_images: skipping duplicate by content"
                        " filename=%s uri=%s",
                        img.get("filename"), img.get("uri"),
                    )
                    continue
                seen_hashes.add(content_digest)

            unique_images.append(img)

        duplicates_removed = len(images) - len(unique_images)
        if duplicates_removed:
            logger.debug(
                "[attachments] itop_get_ticket_images: removed %d duplicate(s),"
                " %d unique image(s) remain",
                duplicates_removed, len(unique_images),
            )
        images = unique_images

        if not images:
            return (
                "No image attachments found for "
                + obj_class + " " + (ticket_ref or key) + "."
            )

        store_entries = [
            {k: v for k, v in img.items() if k not in ("source", "_b64")}
            for img in images
        ]
        try:
            token = get_token_fn()
            token_preview = (token[:8] + "...") if token and len(token) > 8 else (token or "(empty)")
            if token:
                logger.debug(
                    "[attachments] itop_get_ticket_images: writing %d image(s) "
                    "to attachment_store for token=%s",
                    len(store_entries), token_preview,
                )
                store_images(token, store_entries)
                logger.debug("[attachments] itop_get_ticket_images: attachment_store write complete")
            else:
                logger.debug(
                    "[attachments] itop_get_ticket_images: empty token, skipping attachment_store write"
                )
        except Exception as exc:
            logger.warning(
                "[attachments] itop_get_ticket_images: attachment_store write failed: %s", exc
            )

        label = ticket_ref or key or str(resolved)
        dedup_note = (
            " (" + str(duplicates_removed) + " duplicate(s) removed)"
            if duplicates_removed
            else ""
        )
        return (
            str(len(images)) + " image attachment(s) found for "
            + obj_class + " " + label + dedup_note + ".\n"
            + "Read the MCP resource " + _STATIC_RESOURCE_URI + " to retrieve all images at once."
        )

    # ------------------------------------------------------------------
    # Tool: itop_get_ticket_attachments
    # ------------------------------------------------------------------

    @mcp.tool(
        name="List_ticket_attachments"
    )
    async def itop_get_ticket_attachments(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """List non-image file attachments for an iTop ticket, including MIME type and browser
        download link. Use itop_get_ticket_images for images. Returns metadata and links only,
        no file binaries. Prefer ticket_ref; use key for a numeric ID or OQL query."""
        ref = coerce_ref(ticket_ref, key)
        obj_class, resolved = await resolve_key(obj_class, ref, client.request)
        if resolved is None:
            return "Error: provide either ticket_ref or key to identify the ticket."

        att_oql = (
            "SELECT Attachment"
            " WHERE item_class = '" + obj_class + "'"
            " AND item_id = " + str(resolved)
        )
        att_result = await client.get("Attachment", att_oql, fields="contents")

        files = []

        for obj_key, obj_data in (att_result.get("objects") or {}).items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, _data, filename = _unpack_contents(fields.get("contents"))
            if _is_image(mimetype):
                continue
            if not filename:
                filename = "attachment_" + record_id
            if not mimetype:
                mimetype = "application/octet-stream"
            files.append({
                "filename": filename,
                "mimetype": mimetype,
                "url": _attachment_url(record_id),
            })

        if not files:
            return (
                "No file attachments found for "
                + obj_class + " " + (ticket_ref or key) + "."
            )

        label = ticket_ref or key or str(resolved)
        lines = [
            "File attachments for " + obj_class + " " + label
            + " (" + str(len(files)) + " found):"
        ]
        for f in files:
            lines.append("\n--- " + f["filename"] + " ---")
            lines.append("  mimetype     : " + f["mimetype"])
            lines.append("  browser_link : " + f["url"])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Resource: itop://attachment/image.jpg  (static, no URI template)
    # ------------------------------------------------------------------

    @mcp.resource(
        _STATIC_RESOURCE_URI,
        name="Download ticket images",
        description=(
            "Returns all images stored by the most recent itop_get_ticket_images call "
            "for this session as one ResourceContent per image. "
            "All images are served as JPEG directly from the BLOB store."
            "Call itop_get_ticket_images first to populate this resource."
            "Only call this once, it serves ALL images for a ticket in one call."
        ),
        mime_type="image/jpeg",
    )
    async def serve_ticket_images() -> ResourceResult:
        """Serve all ticket images stored for the current bearer token session.

        All entries are stored as JPEG BLOBs (normalization happens at write time
        in attachment_store.store_images). No HTTP download is needed here.

        Returns a plain-text ResourceResult when no images are available
        (not yet stored, token mismatch, or TTL expired).
        """
        logger.debug("[attachments] serve_ticket_images: resource handler invoked")

        try:
            token = get_token_fn()
            token_preview = (token[:8] + "...") if token and len(token) > 8 else (token or "(empty)")
            logger.debug(
                "[attachments] serve_ticket_images: token=%s (len=%d)",
                token_preview,
                len(token) if token else 0,
            )
        except Exception as exc:
            logger.warning(
                "[attachments] serve_ticket_images: get_token_fn raised: %s", exc
            )
            token = ""

        if not token:
            return ResourceResult(
                contents="No active session token. Connect with a valid iTop bearer token."
            )

        entries = get_images(token)
        logger.debug(
            "[attachments] serve_ticket_images: attachment_store returned %d entry/entries",
            len(entries),
        )

        if not entries:
            return ResourceResult(
                contents=(
                    "No images available for this session. "
                    "Call itop_get_ticket_images first."
                )
            )

        resource_contents: list[ResourceContent] = []
        errors: list[str] = []

        for i, entry in enumerate(entries):
            content_bytes: bytes | None = entry.get("content")
            mime: str = entry.get("mimetype", "image/jpeg")
            logger.debug(
                "[attachments] serve_ticket_images: [%d/%d] filename=%s uri=%s content=%s",
                i + 1, len(entries),
                entry.get("filename", "?"),
                entry.get("uri", "?"),
                ("%d bytes" % len(content_bytes)) if content_bytes is not None else "None",
            )

            if content_bytes is None:
                msg = entry.get("filename", "?") + ": no content in store"
                logger.warning("[attachments] serve_ticket_images: [%d] %s", i + 1, msg)
                errors.append(msg)
                continue

            resource_contents.append(
                ResourceContent(content=content_bytes, mime_type=mime)
            )

        if not resource_contents:
            error_detail = "; ".join(errors) if errors else "unknown error"
            return ResourceResult(
                contents="Failed to serve all images. Errors: " + error_detail
            )

        logger.debug(
            "[attachments] serve_ticket_images: returning %d ResourceContent(s),"
            " %d error(s) skipped",
            len(resource_contents), len(errors),
        )
        return ResourceResult(contents=resource_contents)
