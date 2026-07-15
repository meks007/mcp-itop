"""
Attachment tools: fetch image metadata and serve images as MCP resources.

Public API
----------
register(mcp, itop_request)
    Registers the following MCP tools and resources:

    Tools:
        itop_get_ticket_images(obj_class, ticket_ref, key)
            List all image attachments for a ticket. Returns a plain text
            summary with filename, mimetype and resource_uri per image.
            The LLM reads each image via resources/read on the returned URI.

        itop_get_ticket_attachments(obj_class, ticket_ref, key)
            List all non-image file attachments for a ticket.
            Returns metadata and browser download links only.

    Resources:
        itop://attachment/{attachment_id}
            Download an Attachment by its numeric iTop ID.
            Returns bytes; FastMCP wraps as BlobResourceContents automatically.

        itop://inlineimage/{secret}/{record_id}
            Download an InlineImage by its secret and numeric ID.
            Returns bytes; FastMCP wraps as BlobResourceContents automatically.

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

import httpx

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
    async with httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        ct = response.headers.get("content-type", "application/octet-stream")
        mimetype = ct.split(";")[0].strip()
        return response.content, mimetype


def register(mcp, itop_request):
    """Register attachment tools and resources."""

    # ------------------------------------------------------------------
    # Resource: itop://attachment/{attachment_id}
    # ------------------------------------------------------------------

    @mcp.resource("itop://attachment/{attachment_id}")
    async def resource_attachment(attachment_id: str) -> bytes:
        """Download an iTop Attachment as a binary blob.

        URI scheme: itop://attachment/<attachment_id>
        FastMCP base64-encodes the returned bytes and wraps them as
        BlobResourceContents automatically. No data-URI prefix is added.
        """
        url = _attachment_url(attachment_id)
        try:
            content_bytes, mimetype = await _download_binary(url)
        except Exception as exc:
            if MCP_DEBUG:
                logger.debug("resource_attachment error: id=%s exc=%s", attachment_id, exc)
            raise

        if MCP_DEBUG:
            logger.debug(
                "resource_attachment: id=%s mime=%s bytes=%d",
                attachment_id, mimetype, len(content_bytes),
            )

        return content_bytes

    # ------------------------------------------------------------------
    # Resource: itop://inlineimage/{secret}/{record_id}
    # ------------------------------------------------------------------

    @mcp.resource("itop://inlineimage/{secret}/{record_id}")
    async def resource_inlineimage(secret: str, record_id: str) -> bytes:
        """Download an iTop InlineImage as a binary blob.

        URI scheme: itop://inlineimage/<secret>/<record_id>
        FastMCP base64-encodes the returned bytes and wraps them as
        BlobResourceContents automatically. No data-URI prefix is added.
        """
        url = _inline_image_url(secret, record_id)
        try:
            content_bytes, mimetype = await _download_binary(url)
        except Exception as exc:
            if MCP_DEBUG:
                logger.debug(
                    "resource_inlineimage error: id=%s exc=%s", record_id, exc
                )
            raise

        if MCP_DEBUG:
            logger.debug(
                "resource_inlineimage: id=%s mime=%s bytes=%d",
                record_id, mimetype, len(content_bytes),
            )

        return content_bytes

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

        Returns a plain text listing with filename, mimetype and resource_uri
        per image. Use resources/read on each resource_uri to retrieve the
        image as a blob via the registered MCP resource handlers.

        For ticket classes (UserRequest, Incident, etc.) prefer ticket_ref
        (e.g. R-016271); it is resolved automatically and takes priority
        over key. Use key (numeric ID or OQL) for non-ticket classes.

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

        for obj_key, obj_data in (att_result.get("objects") or {}).items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, _data, filename = _unpack_contents(fields.get("contents"))
            if not _is_image(mimetype):
                continue
            if not filename:
                filename = "attachment_" + record_id
            images.append({
                "source": "Attachment",
                "filename": filename,
                "mimetype": mimetype,
                "resource_uri": "itop://attachment/" + record_id,
            })

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

        for obj_key, obj_data in (ii_result.get("objects") or {}).items():
            fields = obj_data.get("fields") or {}
            record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
            mimetype, _data, filename = _unpack_contents(fields.get("contents"))
            secret = (fields.get("secret") or "").strip()
            if not filename:
                filename = fields.get("friendlyname") or ("inlineimage_" + record_id)
            if not mimetype:
                mimetype = "image/unknown"
            resource_uri = (
                "itop://inlineimage/" + secret + "/" + record_id
                if secret
                else "itop://attachment/" + record_id
            )
            images.append({
                "source": "InlineImage",
                "filename": filename,
                "mimetype": mimetype,
                "resource_uri": resource_uri,
            })

        if not images:
            return (
                "No image attachments found for "
                + obj_class + " " + (ticket_ref or key) + "."
            )

        label = ticket_ref or key or str(resolved)
        lines = [
            str(len(images)) + " image attachment(s) found for "
            + obj_class + " " + label + ".",
            "Use resources/read on each resource_uri to retrieve the image blob.",
            "",
        ]
        for img in images:
            lines.append("--- " + img["filename"] + " (" + img["source"] + ") ---")
            lines.append("  mimetype     : " + img["mimetype"])
            lines.append("  resource_uri : " + img["resource_uri"])

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
