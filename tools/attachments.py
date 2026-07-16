"""
Attachment tools: fetch image metadata and download attachments as files.

Public API
----------
register(mcp, itop_request, get_token_fn)
    Registers the following MCP tools and resources:

    Tools:
        itop_get_ticket_images(obj_class, ticket_ref, key)
            Fetches image attachments for a ticket, stores them in the
            SQLite attachment store, and returns only the image count plus
            the static MCP resource URI _STATIC_RESOURCE_URI.
            The client must read that resource to retrieve the actual images.

        itop_get_ticket_attachments(obj_class, ticket_ref, key)
            List all non-image file attachments for a ticket.
            Returns metadata and browser download links only.

    Resources:
        _STATIC_RESOURCE_URI  (static)
            Returns all images stored by the most recent
            itop_get_ticket_images call for this client session as a
            multi-content ResourceResult (one ResourceContent per image).

iTop blob field notes
---------------------
The contents AttributeBlob is returned by the REST API as a dict:
  {"mimetype": "<mime>", "data": "<base64>", "filename": "<name>"}

Attachment  : may be any MIME type; mimetype is checked before including.
              Download via ?operation=download_document&id=<id>
              NOTE: ajax.document.php requires an active iTop session cookie
              and does NOT accept the bearer token. Binary is therefore read
              directly from the REST API response (contents.data base64 field)
              and stored as a data: URI -- no second HTTP request needed.
InlineImage : always an image; has a secret field for the download URL.
              Download via ?operation=download_inlineimage&id=<id>&s=<secret>
              Has no filename field; friendlyname or fabricated name is used.
              InlineImage download works without a session cookie.
"""

from __future__ import annotations

import hashlib
import httpx
from fastmcp.resources import ResourceResult, ResourceContent

from attachment_store import get_images, store_images
from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger
from helpers import resolve_key

_IMAGE_PREFIXES = ("image/",)

_STATIC_RESOURCE_URI = "itop://attachment/image.png"


def _is_image(mimetype: str) -> bool:
    ct = mimetype.split(";")[0].strip().lower()
    return any(ct.startswith(p) for p in _IMAGE_PREFIXES)


def _attachment_url(attachment_id: str | int) -> str:
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_document&class=Attachment&field=contents&id={attachment_id}"
    )


def _inline_image_url(secret: str, record_id: str | int) -> str:
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_inlineimage&id={record_id}&s={secret}"
    )


def _unpack_contents(contents: object) -> tuple:
    """Unpack iTop contents blob into (mimetype, b64_data, filename).

    iTop serialises AttributeBlob as:
      {"mimetype": "image/png", "data": "<base64>", "filename": "foo.png"}
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
    async with httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT) as client:
        response = await client.get(url)
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


def _http_url_from_uri(uri: str) -> str:
    """Resolve an itop:// URI to an iTop HTTP download URL."""
    if uri.startswith("itop://inlineimage/"):
        rest = uri[len("itop://inlineimage/"):]
        secret, record_id = rest.split("/", 1)
        url = _inline_image_url(secret, record_id)
    else:
        attachment_id = uri[len("itop://attachment/"):]
        url = _attachment_url(attachment_id)
    logger.debug("[attachments] _http_url_from_uri: uri=%s -> url=%s", uri, url)
    return url


def register(mcp, itop_request, get_token_fn):
    """Register attachment tools and the static image resource.

    Args:
        mcp:           FastMCP server instance.
        itop_request:  Async callable that sends iTop REST requests.
        get_token_fn:  Zero-argument callable returning the current bearer
                       token string for the active MCP client session.
    """

    # ------------------------------------------------------------------
    # Tool: itop_get_ticket_images
    # ------------------------------------------------------------------

    @mcp.tool(
        name="get_ticket_images"
    )
    async def itop_get_ticket_images(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """Find image attachments for an iTop ticket and store them for the current session.

        Prefer ticket_ref for tickets; use key for a numeric ID or OQL query.
        After this tool returns, read the MCP resource itop://attachment/image.png
        to retrieve the actual image binaries."""
        logger.debug(
            "[attachments] itop_get_ticket_images: called obj_class=%s ticket_ref=%r key=%r",
            obj_class, ticket_ref, key,
        )

        # resolve_key now returns (resolved_class, numeric_key); override obj_class.
        obj_class, resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )
        logger.debug(
            "[attachments] itop_get_ticket_images: resolved class=%r key=%r",
            obj_class, resolved,
        )

        if resolved is None:
            return "Error: provide either ticket_ref or key to identify the ticket."

        images = []

        # -- Attachment (image types only) --
        att_result = await itop_request({
            "operation": "core/get",
            "class": "Attachment",
            "key": (
                "SELECT Attachment"
                " WHERE item_class = '" + obj_class + "'"
                " AND item_id = " + str(resolved)
            ),
            "output_fields": "contents",
        })
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
            # Use a data: URI so the resource handler can decode the binary
            # directly from the REST API response without a second HTTP request.
            # ajax.document.php requires an active iTop session (cookie) and
            # does not accept the bearer token -- a GET would return an HTML login page.
            if b64_data:
                uri = "data:" + mimetype + ";base64," + b64_data
            else:
                uri = "itop://attachment/" + record_id
            images.append({"source": "Attachment", "filename": filename, "mimetype": mimetype, "uri": uri, "b64": b64_data})
            logger.debug(
                "[attachments] itop_get_ticket_images: added Attachment record_id=%s"
                " uri_scheme=%s",
                record_id, uri.split(":")[0],
            )

        # -- InlineImage (always image) --
        ii_result = await itop_request({
            "operation": "core/get",
            "class": "InlineImage",
            "key": (
                "SELECT InlineImage"
                " WHERE item_class = '" + obj_class + "'"
                " AND item_id = " + str(resolved)
            ),
            "output_fields": "contents, secret, friendlyname",
        })
        ii_objects = ii_result.get("objects") or {}
        logger.debug(
            "[attachments] itop_get_ticket_images: InlineImage query returned %d object(s)",
            len(ii_objects),
        )

        for obj_key, obj_data in ii_objects.items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, b64_data, filename = _unpack_contents(fields.get("contents"))
            secret = (fields.get("secret") or "").strip()
            if not filename:
                filename = fields.get("friendlyname") or ("inlineimage_" + record_id)
            if not mimetype:
                mimetype = "image/unknown"
            # InlineImage download works without a session cookie -- keep http URI.
            uri = (
                "itop://inlineimage/" + secret + "/" + record_id
                if secret
                else "itop://attachment/" + record_id
            )
            images.append({"source": "InlineImage", "filename": filename, "mimetype": mimetype, "uri": uri, "b64": b64_data})
            logger.debug(
                "[attachments] itop_get_ticket_images: added InlineImage record_id=%s uri=%s",
                record_id, uri,
            )

        logger.debug(
            "[attachments] itop_get_ticket_images: total images collected=%d", len(images)
        )

        # Deduplicate by SHA-256 of the raw base64 content.
        # Attachments with no b64 data are kept unconditionally.
        seen_hashes = set()
        unique_images = []
        for img in images:
            b64 = img.get("b64") or ""
            if b64:
                digest = hashlib.sha256(b64.encode("ascii", errors="replace")).hexdigest()
                if digest in seen_hashes:
                    logger.debug(
                        "[attachments] itop_get_ticket_images: skipping duplicate"
                        " digest=%s filename=%s uri_scheme=%s",
                        digest[:12], img.get("filename"), img.get("uri", "").split(":")[0],
                    )
                    continue
                seen_hashes.add(digest)
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

        # Persist image list in the SQLite store so the static resource
        # handler itop://attachment/images can serve them for this session.
        # Strip the internal "b64" key -- it is not part of ImageEntry schema.
        try:
            token = get_token_fn()
            token_preview = (token[:8] + "...") if token and len(token) > 8 else (token or "(empty)")
            if token:
                logger.debug(
                    "[attachments] itop_get_ticket_images: writing %d image(s) "
                    "to attachment_store for token=%s",
                    len(images), token_preview,
                )
                store_images(token, [{k: v for k, v in img.items() if k != "b64"} for img in images])
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
        name="Get_ticket_attachments"
    )
    async def itop_get_ticket_attachments(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """List non-image file attachments for an iTop ticket, including MIME type and browser
        download link. Use itop_get_ticket_images for images. Returns metadata and links only,
        no file binaries. Prefer ticket_ref; use key for a numeric ID or OQL query."""
        # resolve_key now returns (resolved_class, numeric_key); override obj_class.
        obj_class, resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )
        if resolved is None:
            return "Error: provide either ticket_ref or key to identify the ticket."

        att_result = await itop_request({
            "operation": "core/get",
            "class": "Attachment",
            "key": (
                "SELECT Attachment"
                " WHERE item_class = '" + obj_class + "'"
                " AND item_id = " + str(resolved)
            ),
            "output_fields": "contents",
        })

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
    # Resource: itop://attachment/images  (static, no URI template)
    # ------------------------------------------------------------------

    @mcp.resource(
        _STATIC_RESOURCE_URI,
        name="Analyze_ticket_images",
        description=(
            "Returns all images stored by the most recent itop_get_ticket_images call "
            "for this session as one ResourceContent per image. "
            "Call itop_get_ticket_images first to populate this resource."
        ),
        mime_type="image/png",
    )
    async def serve_ticket_images() -> ResourceResult:
        """Serve all ticket images stored for the current bearer token session.

        Downloads each image binary from iTop and returns them as a list of
        ResourceContent objects inside a single ResourceResult. The MIME type
        of each item reflects the actual image format (image/png, image/jpeg,
        etc.).

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
            uri = entry["uri"]
            logger.debug(
                "[attachments] serve_ticket_images: processing [%d/%d] filename=%s uri_scheme=%s",
                i + 1, len(entries), entry.get("filename", "?"), uri.split(":")[0],
            )
            try:
                if uri.startswith("data:"):
                    # Attachment binary was embedded as a base64 data URI at
                    # collection time -- decode in-memory, no HTTP request needed.
                    import base64 as _base64
                    header, b64 = uri.split(",", 1)
                    mime = header.split(":")[1].split(";")[0]
                    content_bytes = _base64.b64decode(b64)
                    logger.debug(
                        "[attachments] serve_ticket_images: [%d] decoded data URI"
                        " mime=%s bytes=%d",
                        i + 1, mime, len(content_bytes),
                    )
                else:
                    # InlineImage (and any Attachment without b64) -- HTTP download.
                    http_url = _http_url_from_uri(uri)
                    content_bytes, mime = await _download_binary(http_url)
                    mime = mime or entry.get("mimetype", "application/octet-stream")
                    logger.debug(
                        "[attachments] serve_ticket_images: [%d] downloaded mime=%s bytes=%d",
                        i + 1, mime, len(content_bytes),
                    )
                resource_contents.append(
                    ResourceContent(content=content_bytes, mime_type=mime)
                )
            except Exception as exc:
                logger.warning(
                    "[attachments] serve_ticket_images: [%d] failed filename=%s exc=%s",
                    i + 1, entry.get("filename", "?"), exc,
                )
                errors.append(entry.get("filename", "?") + ": " + str(exc))

        if not resource_contents:
            error_detail = "; ".join(errors) if errors else "unknown error"
            return ResourceResult(
                contents="Failed to download all images. Errors: " + error_detail
            )

        logger.debug(
            "[attachments] serve_ticket_images: returning %d ResourceContent(s), %d error(s) skipped",
            len(resource_contents), len(errors),
        )
        return ResourceResult(contents=resource_contents)
