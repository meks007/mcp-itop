"""
Attachment tools: fetch image metadata and download attachments as files.

Public API
----------
register(mcp, itop_request, get_token_fn)
    Registers the following MCP tools and resources:

    Tools:
        itop_get_ticket_images(obj_class, ticket_ref, key)
            List all image attachments for a ticket. Returns a plain text
            summary with filename, mimetype and download_uri per image.
            Also persists the image list in the SQLite attachment store so
            the static resource handler can serve them.
            After calling this tool, read the MCP resource
            itop://attachment/images to retrieve all images at once.

        itop_get_ticket_attachments(obj_class, ticket_ref, key)
            List all non-image file attachments for a ticket.
            Returns metadata and browser download links only.

    Resources:
        itop://attachment/images  (static)
            Returns all images stored by the most recent
            itop_get_ticket_images call for this client session as a
            multi-content ResourceResult (one ResourceContent per image).

iTop blob field notes
---------------------
The contents AttributeBlob is returned by the REST API as a dict:
  {"mimetype": "<mime>", "data": "<base64>", "filename": "<name>"}

Attachment  : may be any MIME type; mimetype is checked before including.
              Download via ?operation=download_document&id=<id>
InlineImage : always an image; has a secret field for the download URL.
              Download via ?operation=download_inlineimage&id=<id>&s=<secret>
              Has no filename field; friendlyname or fabricated name is used.
"""

from __future__ import annotations

import mimetypes

import httpx
from fastmcp.resources import ResourceResult, ResourceContent

from attachment_store import get_images, store_images
from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger
from helpers import resolve_key

_IMAGE_PREFIXES = ("image/",)


def _is_image(mimetype: str) -> bool:
    ct = mimetype.split(";")[0].strip().lower()
    return any(ct.startswith(p) for p in _IMAGE_PREFIXES)


def _attachment_url(attachment_id: str | int) -> str:
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_document&id={attachment_id}"
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

    @mcp.tool()
    async def itop_get_ticket_images(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """List all image attachments for an iTop ticket.

        Queries both the Attachment class (image mimetype check) and the
        InlineImage class (always image). Results from both sources are
        combined with equal weight.

        Returns a plain text listing with filename, mimetype and download_uri
        per image. After calling this tool, read the MCP resource
        itop://attachment/images to retrieve all images at once as binary
        content.

        For ticket classes (UserRequest, Incident, etc.) prefer ticket_ref
        (e.g. R-016271); it is resolved automatically and takes priority
        over key. Use key (numeric ID or OQL) for non-ticket classes.

        Args:
            obj_class:  iTop class, e.g. UserRequest, Incident.
            ticket_ref: Preferred ticket reference, e.g. R-016271.
            key:        Fallback numeric ID or OQL query.
        """
        logger.debug(
            "[attachments] itop_get_ticket_images: called obj_class=%s ticket_ref=%r key=%r",
            obj_class, ticket_ref, key,
        )

        resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )
        logger.debug(
            "[attachments] itop_get_ticket_images: resolved key=%r", resolved
        )

        if resolved is None:
            logger.debug(
                "[attachments] itop_get_ticket_images: no resolved key, returning error"
            )
            return "Error: provide either ticket_ref or key to identify the ticket."

        images = []

        # -- Attachment (image types only) --
        logger.debug(
            "[attachments] itop_get_ticket_images: querying Attachment for "
            "item_class=%s item_id=%s",
            obj_class, resolved,
        )
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
            "[attachments] itop_get_ticket_images: Attachment query returned "
            "%d object(s), code=%s",
            len(att_objects),
            att_result.get("code"),
        )

        for obj_key, obj_data in att_objects.items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, _data, filename = _unpack_contents(fields.get("contents"))
            logger.debug(
                "[attachments] itop_get_ticket_images: Attachment record_id=%s "
                "mimetype=%s filename=%r is_image=%s",
                record_id, mimetype, filename, _is_image(mimetype),
            )
            if not _is_image(mimetype):
                logger.debug(
                    "[attachments] itop_get_ticket_images: skipping non-image "
                    "record_id=%s mimetype=%s",
                    record_id, mimetype,
                )
                continue
            if not filename:
                filename = "attachment_" + record_id
            uri = "itop://attachment/" + record_id
            images.append({
                "source": "Attachment",
                "filename": filename,
                "mimetype": mimetype,
                "uri": uri,
            })
            logger.debug(
                "[attachments] itop_get_ticket_images: added Attachment "
                "record_id=%s uri=%s mimetype=%s filename=%r",
                record_id, uri, mimetype, filename,
            )

        # -- InlineImage (always image) --
        logger.debug(
            "[attachments] itop_get_ticket_images: querying InlineImage for "
            "item_class=%s item_id=%s",
            obj_class, resolved,
        )
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
            "[attachments] itop_get_ticket_images: InlineImage query returned "
            "%d object(s), code=%s",
            len(ii_objects),
            ii_result.get("code"),
        )

        for obj_key, obj_data in ii_objects.items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, _data, filename = _unpack_contents(fields.get("contents"))
            secret = (fields.get("secret") or "").strip()
            logger.debug(
                "[attachments] itop_get_ticket_images: InlineImage record_id=%s "
                "mimetype=%s filename=%r secret_present=%s",
                record_id, mimetype, filename, bool(secret),
            )
            if not filename:
                filename = fields.get("friendlyname") or ("inlineimage_" + record_id)
            if not mimetype:
                mimetype = "image/unknown"
            uri = (
                "itop://inlineimage/" + secret + "/" + record_id
                if secret
                else "itop://attachment/" + record_id
            )
            images.append({
                "source": "InlineImage",
                "filename": filename,
                "mimetype": mimetype,
                "uri": uri,
            })
            logger.debug(
                "[attachments] itop_get_ticket_images: added InlineImage "
                "record_id=%s uri=%s mimetype=%s filename=%r",
                record_id, uri, mimetype, filename,
            )

        logger.debug(
            "[attachments] itop_get_ticket_images: total images collected=%d",
            len(images),
        )

        if not images:
            logger.debug(
                "[attachments] itop_get_ticket_images: no images found, returning empty result"
            )
            return (
                "No image attachments found for "
                + obj_class + " " + (ticket_ref or key) + "."
            )

        # Persist image list in the SQLite store so the static resource
        # handler itop://attachment/images can serve them for this session.
        try:
            token = get_token_fn()
            token_preview = (token[:8] + "...") if token and len(token) > 8 else (token or "(empty)")
            logger.debug(
                "[attachments] itop_get_ticket_images: get_token_fn returned "
                "token=%s (len=%d)",
                token_preview,
                len(token) if token else 0,
            )
            if token:
                logger.debug(
                    "[attachments] itop_get_ticket_images: writing %d image(s) "
                    "to attachment_store for token=%s",
                    len(images), token_preview,
                )
                store_images(token, images)
                logger.debug(
                    "[attachments] itop_get_ticket_images: attachment_store write complete"
                )
            else:
                logger.debug(
                    "[attachments] itop_get_ticket_images: empty token from get_token_fn, "
                    "skipping attachment_store write"
                )
        except Exception as exc:
            logger.warning(
                "[attachments] itop_get_ticket_images: attachment_store write failed: %s",
                exc,
            )

        label = ticket_ref or key or str(resolved)
        lines = [
            str(len(images)) + " image attachment(s) found for "
            + obj_class + " " + label + ".",
            "Read the MCP resource itop://attachment/images to retrieve all images at once.",
            "",
        ]
        for img in images:
            lines.append("--- " + img["filename"] + " (" + img["source"] + ") ---")
            lines.append("  mimetype     : " + img["mimetype"])
            lines.append("  download_uri : " + img["uri"])

        logger.debug(
            "[attachments] itop_get_ticket_images: returning text result with %d image(s)",
            len(images),
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool: itop_get_ticket_attachments
    # ------------------------------------------------------------------

    @mcp.tool()
    async def itop_get_ticket_attachments(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """List all non-image file attachments for an iTop ticket.

        Queries the Attachment class and returns entries whose MIME type is
        not an image type (e.g. PDF, DOCX, ZIP). For image attachments use
        itop_get_ticket_images instead.

        Returns metadata and browser download links only. No binary content
        is fetched or returned.

        Args:
            obj_class:  iTop class, e.g. UserRequest, Incident.
            ticket_ref: Preferred ticket reference, e.g. R-016271.
            key:        Fallback numeric ID or OQL query.
        """
        resolved = await resolve_key(
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
        "itop://attachment/images",
        name="TicketImages",
        description=(
            "All images from the most recent itop_get_ticket_images call "
            "for this client session. Returns one ResourceContent per image "
            "with the raw binary and its MIME type. Call itop_get_ticket_images "
            "first to populate this resource."
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
                "[attachments] serve_ticket_images: get_token_fn returned "
                "token=%s (len=%d)",
                token_preview,
                len(token) if token else 0,
            )
        except Exception as exc:
            logger.warning(
                "[attachments] serve_ticket_images: get_token_fn raised: %s", exc
            )
            token = ""
            token_preview = "(error)"

        if not token:
            logger.debug(
                "[attachments] serve_ticket_images: no token available, "
                "returning auth error response"
            )
            return ResourceResult(
                contents="No active session token. Connect with a valid iTop bearer token."
            )

        logger.debug(
            "[attachments] serve_ticket_images: querying attachment_store "
            "for token=%s", token_preview,
        )
        entries = get_images(token)
        logger.debug(
            "[attachments] serve_ticket_images: attachment_store returned "
            "%d entry/entries for token=%s",
            len(entries), token_preview,
        )

        if not entries:
            logger.debug(
                "[attachments] serve_ticket_images: no entries in store, "
                "returning empty result"
            )
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
                "[attachments] serve_ticket_images: downloading [%d/%d] uri=%s",
                i + 1, len(entries), uri,
            )
            try:
                http_url = _http_url_from_uri(uri)
                logger.debug(
                    "[attachments] serve_ticket_images: [%d] resolved http_url=%s",
                    i + 1, http_url,
                )
                content_bytes, detected_mime = await _download_binary(http_url)
                mime = detected_mime or entry.get("mimetype", "application/octet-stream")
                logger.debug(
                    "[attachments] serve_ticket_images: [%d] uri=%s "
                    "detected_mime=%s final_mime=%s bytes=%d",
                    i + 1, uri, detected_mime, mime, len(content_bytes),
                )
                resource_contents.append(
                    ResourceContent(content=content_bytes, mime_type=mime)
                )
            except Exception as exc:
                logger.warning(
                    "[attachments] serve_ticket_images: [%d] download failed "
                    "uri=%s exc=%s",
                    i + 1, uri, exc,
                )
                errors.append(uri + ": " + str(exc))

        logger.debug(
            "[attachments] serve_ticket_images: download phase complete "
            "success=%d errors=%d",
            len(resource_contents), len(errors),
        )

        if not resource_contents:
            error_detail = "; ".join(errors) if errors else "unknown error"
            logger.debug(
                "[attachments] serve_ticket_images: all downloads failed, "
                "returning error response: %s",
                error_detail,
            )
            return ResourceResult(
                contents="Failed to download all images. Errors: " + error_detail
            )

        logger.debug(
            "[attachments] serve_ticket_images: returning ResourceResult with "
            "%d ResourceContent(s), %d error(s) skipped",
            len(resource_contents), len(errors),
        )
        return ResourceResult(contents=resource_contents)
