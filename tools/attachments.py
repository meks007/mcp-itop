"""
Attachment tools: fetch image metadata and download attachments as files.

Public API
----------
register(mcp, itop_request)
    Registers the following MCP tools:

    Tools:
        itop_get_ticket_images(obj_class, ticket_ref, key)
            List all image attachments for a ticket. Returns a plain text
            summary with filename, mimetype and resource_uri per image.

        itop_download_attachment(uri, name)
            Download an iTop image attachment and return it directly as a
            base64-encoded file inside structuredContent.files so Langdock
            can process it as an attachment before the character limit is
            applied.

        itop_get_ticket_attachments(obj_class, ticket_ref, key)
            List all non-image file attachments for a ticket.
            Returns metadata and browser download links only.

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

import base64
import mimetypes

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


def _validate_itop_uri(uri: str) -> str:
    """Validate an itop:// attachment URI and return it unchanged.

    Supported schemes:
      itop://attachment/<attachment_id>
      itop://inlineimage/<secret>/<record_id>

    Raises ValueError on unrecognised or malformed URIs.
    """
    if uri.startswith("itop://attachment/"):
        attachment_id = uri[len("itop://attachment/"):]
        if not attachment_id:
            raise ValueError("Missing attachment_id in URI: " + uri)
        return uri

    if uri.startswith("itop://inlineimage/"):
        rest = uri[len("itop://inlineimage/"):]
        parts = rest.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                "Malformed inlineimage URI, expected "
                "itop://inlineimage/<secret>/<record_id>. Got: " + uri
            )
        return uri

    raise ValueError(
        "Unrecognised URI scheme. Expected itop://attachment/<id>"
        " or itop://inlineimage/<secret>/<record_id>. Got: " + uri
    )


def _filename_from_uri(uri: str, mimetype: str) -> str:
    """Derive a sensible filename from an itop:// URI and MIME type."""
    ext = mimetypes.guess_extension(mimetype.split(";")[0].strip()) or ""
    if ext == ".jpe":
        ext = ".jpg"

    if uri.startswith("itop://inlineimage/"):
        rest = uri[len("itop://inlineimage/"):]
        record_id = rest.split("/", 1)[1]
        return "inlineimage_" + record_id + ext
    else:
        attachment_id = uri[len("itop://attachment/"):]
        return "attachment_" + attachment_id + ext


def register(mcp, itop_request):
    """Register attachment tools."""

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
        per image. Pass each resource_uri to itop_download_attachment to
        download the image directly as a file attachment.

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
            "Pass each resource_uri to itop_download_attachment to download the image.",
            "",
        ]
        for img in images:
            lines.append("--- " + img["filename"] + " (" + img["source"] + ") ---")
            lines.append("  mimetype     : " + img["mimetype"])
            lines.append("  resource_uri : " + img["resource_uri"])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool: itop_download_attachment
    # ------------------------------------------------------------------

    @mcp.tool()
    async def itop_download_attachment(
        uri: str,
        name: str = "",
    ) -> dict:
        """Download an iTop image attachment and return it as a file object.

        Fetches the binary directly from iTop and returns the image inside
        structuredContent.files so Langdock registers it as an attachment
        before the character limit is applied.

        The previous ResourceLink approach required Langdock to resolve the
        itop:// URI via resources/read, which only works for statically
        registered resource URIs. Because attachment URIs are dynamic
        (per-ticket), that path did not work. This tool fetches the blob
        directly and encodes it as plain base64 (no data: prefix).

        Supported URI schemes:
          itop://attachment/<attachment_id>
          itop://inlineimage/<secret>/<record_id>

        Args:
            uri:  Resource URI as returned by itop_get_ticket_images.
            name: Optional filename override.
        """
        validated = _validate_itop_uri(uri)

        if validated.startswith("itop://inlineimage/"):
            rest = validated[len("itop://inlineimage/"):]
            secret, record_id = rest.split("/", 1)
            http_url = _inline_image_url(secret, record_id)
        else:
            attachment_id = validated[len("itop://attachment/"):]
            http_url = _attachment_url(attachment_id)

        try:
            content_bytes, mimetype = await _download_binary(http_url)
        except Exception as exc:
            logger.debug("itop_download_attachment error: uri=%s exc=%s", validated, exc)
            raise

        if MCP_DEBUG:
            logger.debug(
                "itop_download_attachment: uri=%s mime=%s bytes=%d",
                validated, mimetype, len(content_bytes),
            )

        file_name = name or _filename_from_uri(validated, mimetype)
        b64 = base64.b64encode(content_bytes).decode("ascii")

        return {
            "content": [{"type": "text", "text": "Attachment downloaded: " + file_name}],
            "structuredContent": {
                "files": [
                    {
                        "fileName": file_name,
                        "mimeType": mimetype,
                        "base64": b64,
                    }
                ]
            },
        }

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
