"""
Attachment tools: fetch image metadata and serve images as MCP resources.

Public API
----------
register(mcp, itop_request)
    Registers the following MCP tools and resources:

    Tools:
        itop_get_ticket_images(obj_class, ticket_ref, key)
            Fetch all image attachments for a ticket via the iTop REST API.
            Queries both Attachment and InlineImage classes.
            Returns metadata, download links, and resource URIs for each image.

        itop_get_ticket_attachments(obj_class, ticket_ref, key)
            Fetch all non-image file attachments for a ticket.
            Returns metadata and download links.

    Resources:
        itop://attachment/{attachment_id}
            Download an Attachment by its numeric iTop ID.
            Returns a pure Base64 blob (no data-URI prefix) with mimeType.

        itop://inlineimage/{record_id}/{secret}
            Download an InlineImage by its numeric ID and secret.
            Returns a pure Base64 blob (no data-URI prefix) with mimeType.

iTop blob field notes
---------------------
The contents AttributeBlob is returned by the REST API as a dict:
  {"mimetype": "<mime>", "data": "<base64>", "filename": "<name>"}

Attachment  : may be any MIME type; mimetype is checked before including.
              Download via ?operation=download_document&id=<id>
InlineImage : always an image; has a secret field for the download URL.
              Download via ?operation=download_inlineimage&id=<id>&s=<secret>
              Has no filename field; friendlyname or fabricated name is used.

Auth token note
---------------
auth_token is appended to every download URL so the link works for both
direct browser access and programmatic use without a separate session.
The resource handler fetches the binary at request time using the bearer
token from the MCP request context.
"""

from __future__ import annotations

import base64

import httpx

from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger
from helpers import resolve_key

_IMAGE_PREFIXES = ("image/",)


def _is_image(mimetype: str) -> bool:
    ct = mimetype.split(";")[0].strip().lower()
    return any(ct.startswith(p) for p in _IMAGE_PREFIXES)


def _attachment_url(attachment_id: str | int, token: str) -> str:
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_document&id={attachment_id}&auth_token={token}"
    )


def _inline_image_url(record_id: str | int, secret: str, token: str) -> str:
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_inlineimage&id={record_id}&s={secret}&auth_token={token}"
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
    async def resource_attachment(attachment_id: str) -> dict:
        """Download an iTop Attachment as a Base64 blob.

        URI scheme: itop://attachment/<attachment_id>
        The filename is derived from the last URI segment (the attachment_id).
        Returns a pure Base64 string without any data-URI prefix.
        """
        from auth import get_bearer_token
        from server import mcp as _mcp
        token = get_bearer_token(_mcp)

        url = _attachment_url(attachment_id, token)
        try:
            content_bytes, mimetype = await _download_binary(url)
        except Exception as exc:
            if MCP_DEBUG:
                logger.debug("resource_attachment error: id=%s exc=%s", attachment_id, exc)
            raise

        blob = base64.b64encode(content_bytes).decode("ascii")
        uri = "itop://attachment/" + attachment_id

        if MCP_DEBUG:
            logger.debug(
                "resource_attachment: id=%s mime=%s bytes=%d",
                attachment_id, mimetype, len(content_bytes),
            )

        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": mimetype,
                    "blob": blob,
                }
            ]
        }

    # ------------------------------------------------------------------
    # Resource: itop://inlineimage/{record_id}/{secret}
    # ------------------------------------------------------------------

    @mcp.resource("itop://inlineimage/{record_id}/{secret}")
    async def resource_inlineimage(record_id: str, secret: str) -> dict:
        """Download an iTop InlineImage as a Base64 blob.

        URI scheme: itop://inlineimage/<record_id>/<secret>
        The filename is derived from the last URI segment (the secret).
        Returns a pure Base64 string without any data-URI prefix.
        """
        from auth import get_bearer_token
        from server import mcp as _mcp
        token = get_bearer_token(_mcp)

        url = _inline_image_url(record_id, secret, token)
        try:
            content_bytes, mimetype = await _download_binary(url)
        except Exception as exc:
            if MCP_DEBUG:
                logger.debug(
                    "resource_inlineimage error: id=%s exc=%s", record_id, exc
                )
            raise

        blob = base64.b64encode(content_bytes).decode("ascii")
        uri = "itop://inlineimage/" + record_id + "/" + secret

        if MCP_DEBUG:
            logger.debug(
                "resource_inlineimage: id=%s mime=%s bytes=%d",
                record_id, mimetype, len(content_bytes),
            )

        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": mimetype,
                    "blob": blob,
                }
            ]
        }

    # ------------------------------------------------------------------
    # Tool: itop_get_ticket_images
    # ------------------------------------------------------------------

    @mcp.tool()
    async def itop_get_ticket_images(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """Fetch all image attachments for an iTop ticket.

        Queries both the Attachment class (file attachments, image types only)
        and the InlineImage class (images embedded in ticket text fields).
        Returns for each image:

          - source class (Attachment or InlineImage)
          - filename, MIME type
          - a browser download link (auth_token appended)
          - a resource URI for use with resources/read to retrieve the image
            as a Base64 blob directly through the MCP protocol

        To display or process an image, call resources/read with the resource_uri.

        For ticket classes (UserRequest, Incident, etc.) prefer ticket_ref
        (e.g. R-016271); it is resolved automatically and takes priority
        over key. Use key (numeric ID or OQL) for non-ticket classes.

        Args:
            obj_class:  iTop class, e.g. UserRequest, Incident.
            ticket_ref: Preferred ticket reference, e.g. R-016271.
            key:        Fallback numeric ID or OQL query.
        """
        from auth import get_bearer_token
        from server import mcp as _mcp
        token = get_bearer_token(_mcp)

        resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )
        if resolved is None:
            return "Error: provide either ticket_ref or key to identify the ticket."

        # -- Attachments (image types only) --
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

        images = []

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
                "id": record_id,
                "filename": filename,
                "mimetype": mimetype,
                "url": _attachment_url(record_id, token),
                "resource_uri": "itop://attachment/" + record_id,
            })

        # -- InlineImages --
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
            url = (
                _inline_image_url(record_id, secret, token)
                if secret
                else _attachment_url(record_id, token)
            )
            resource_uri = (
                "itop://inlineimage/" + record_id + "/" + secret
                if secret
                else "itop://attachment/" + record_id
            )
            images.append({
                "source": "InlineImage",
                "id": record_id,
                "filename": filename,
                "mimetype": mimetype,
                "url": url,
                "resource_uri": resource_uri,
            })

        if not images:
            return "No image attachments found for " + obj_class + " " + (ticket_ref or key) + "."

        label = ticket_ref or key or str(resolved)
        lines = [
            "Image attachments for " + obj_class + " " + label
            + " (" + str(len(images)) + " found):"
        ]

        for img in images:
            lines.append("\n--- " + img["filename"] + " (" + img["source"] + ") ---")
            lines.append("  mimetype     : " + img["mimetype"])
            lines.append("  browser_link : " + img["url"])
            lines.append("  resource_uri : " + img["resource_uri"])
            lines.append("  (call resources/read with resource_uri to retrieve as Base64 blob)")

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
        """Fetch all non-image file attachments for an iTop ticket.

        Queries the Attachment class and returns entries whose MIME type is
        not an image type (e.g. PDF, DOCX, ZIP). For image attachments use
        itop_get_ticket_images instead.

        Args:
            obj_class:  iTop class, e.g. UserRequest, Incident.
            ticket_ref: Preferred ticket reference, e.g. R-016271.
            key:        Fallback numeric ID or OQL query.
        """
        from auth import get_bearer_token
        from server import mcp as _mcp
        token = get_bearer_token(_mcp)

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
                "id": record_id,
                "filename": filename,
                "mimetype": mimetype,
                "url": _attachment_url(record_id, token),
            })

        if not files:
            return "No file attachments found for " + obj_class + " " + (ticket_ref or key) + "."

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
