"""
Attachment tools: fetch and download images attached to iTop tickets.

Public API
----------
register(mcp, get_token, itop_request)
    Registers the following MCP tools:

    itop_get_ticket_images(obj_class, ticket_ref, key)
        Fetch all image attachments for a ticket via the iTop REST API.
        Queries both Attachment and InlineImage classes.
        - Images <= 100 KB -> inline base64 data URI + download link
        - Images >  100 KB -> download link only + note to fetch separately

    itop_get_attachment(url)
        Download a single attachment by its ajax.document.php URL and return
        it as a base64 data URI. Hard-capped at 5 MB.

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
The auth_token is appended to every download URL unconditionally so that
both the LLM (calling itop_get_attachment) and the user (clicking the link
in chat) can access the image without a separate browser session.
"""

from __future__ import annotations

import base64

import httpx

from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger
from helpers import resolve_key

B64_INLINE_LIMIT: int = 100 * 1024
_MAX_DL_BYTES: int = 5 * 1024 * 1024
_IMAGE_PREFIXES = ("image/",)


def _get_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT)


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


def _build_image_dict(source, record_id, filename, mimetype, b64_raw, url):
    """Build the standard image result dict applying the inline/note decision."""
    try:
        raw_bytes = len(base64.b64decode(b64_raw, validate=False)) if b64_raw else 0
    except Exception:
        raw_bytes = 0

    if 0 < raw_bytes <= B64_INLINE_LIMIT:
        b64_out = b64_raw
        note = ""
    elif raw_bytes > B64_INLINE_LIMIT:
        b64_out = None
        note = (
            "Image size (" + str(raw_bytes // 1024) + " KB) exceeds the "
            + str(B64_INLINE_LIMIT // 1024) + " KB inline limit. "
            "Use itop_get_attachment with the link to fetch it separately."
        )
    else:
        b64_out = None
        note = "Image data not available inline from the REST API."

    return {
        "source": source,
        "id": record_id,
        "filename": filename,
        "mimetype": mimetype,
        "raw_bytes": raw_bytes,
        "b64": b64_out,
        "url": url,
        "note": note,
    }


async def _fetch_attachments(obj_class, obj_id, token, itop_request):
    """Fetch Attachment records for a ticket, returning only image types.

    Attachment may be any MIME type. The mimetype lives inside the contents
    blob so contents is always fetched. Non-image records are discarded after
    unpacking. auth_token is embedded in the download URL.
    """
    result = await itop_request({
        "operation": "core/get",
        "class": "Attachment",
        "key": (
            "SELECT Attachment"
            " WHERE item_class = '" + obj_class + "'"
            " AND item_id = " + str(obj_id)
        ),
        "output_fields": "contents",
    })

    if result.get("code", -1) != 0:
        if MCP_DEBUG:
            logger.debug(
                "_fetch_attachments error: code=%s msg=%s",
                result.get("code"), result.get("message"),
            )
        return []

    images = []

    for obj_key, obj_data in (result.get("objects") or {}).items():
        fields = obj_data.get("fields") or {}
        record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
        mimetype, b64_raw, filename = _unpack_contents(fields.get("contents"))

        if not _is_image(mimetype):
            continue

        if not filename:
            filename = "attachment_" + record_id

        img = _build_image_dict(
            "Attachment", record_id, filename, mimetype, b64_raw,
            _attachment_url(record_id, token),
        )
        images.append(img)

        if MCP_DEBUG:
            logger.debug(
                "_fetch_attachments: id=%s file=%s mime=%s bytes=%d inline=%s",
                record_id, filename, mimetype, img["raw_bytes"], img["b64"] is not None,
            )

    return images


async def _fetch_inline_images(obj_class, obj_id, token, itop_request):
    """Fetch InlineImage records for a ticket.

    InlineImage is always an image type; no mimetype check needed.
    The download URL requires both the record id and the secret field
    as the s query parameter: ?operation=download_inlineimage&id=<id>&s=<secret>
    auth_token is embedded in the download URL.
    InlineImage has no filename field; contents.filename, friendlyname,
    or a fabricated name is used as fallback.
    """
    result = await itop_request({
        "operation": "core/get",
        "class": "InlineImage",
        "key": (
            "SELECT InlineImage"
            " WHERE item_class = '" + obj_class + "'"
            " AND item_id = " + str(obj_id)
        ),
        "output_fields": "contents, secret, friendlyname",
    })

    if result.get("code", -1) != 0:
        if MCP_DEBUG:
            logger.debug(
                "_fetch_inline_images error: code=%s msg=%s",
                result.get("code"), result.get("message"),
            )
        return []

    images = []

    for obj_key, obj_data in (result.get("objects") or {}).items():
        fields = obj_data.get("fields") or {}
        record_id = str(obj_data.get("key") or obj_key.split("::")[-1])
        mimetype, b64_raw, filename = _unpack_contents(fields.get("contents"))
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

        img = _build_image_dict(
            "InlineImage", record_id, filename, mimetype, b64_raw, url,
        )
        images.append(img)

        if MCP_DEBUG:
            logger.debug(
                "_fetch_inline_images: id=%s file=%s mime=%s bytes=%d inline=%s",
                record_id, filename, mimetype, img["raw_bytes"], img["b64"] is not None,
            )

    return images


async def _fetch_image_attachments(obj_class, obj_id, token, itop_request):
    """Return all images for a ticket from both Attachment and InlineImage."""
    attachments = await _fetch_attachments(obj_class, obj_id, token, itop_request)
    inline_images = await _fetch_inline_images(obj_class, obj_id, token, itop_request)
    return attachments + inline_images


def register(mcp, get_token, itop_request):
    """Register attachment tools."""

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
          - filename, MIME type, size
          - a download link with auth_token embedded (works for both LLM
            download via itop_get_attachment and direct user access in browser)
          - an inline base64 data URI when the image is at or below 100 KB
          - a note directing to itop_get_attachment when above 100 KB

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

        token = get_token()
        images = await _fetch_image_attachments(obj_class, resolved, token, itop_request)

        if not images:
            return "No image attachments found for " + obj_class + " " + (ticket_ref or key) + "."

        label = ticket_ref or key or str(resolved)
        lines = [
            "Image attachments for " + obj_class + " " + label
            + " (" + str(len(images)) + " found):"
        ]

        for img in images:
            size_str = (
                str(img["raw_bytes"] // 1024) + " KB"
                if img["raw_bytes"] >= 1024
                else str(img["raw_bytes"]) + " B"
            )
            lines.append("\n--- " + img["filename"] + " (" + img["source"] + ") ---")
            lines.append("  mimetype : " + img["mimetype"])
            lines.append("  size     : " + size_str)
            lines.append("  link     : " + img["url"])
            if img["b64"]:
                lines.append("  data_uri : data:" + img["mimetype"] + ";base64," + img["b64"])
            else:
                lines.append("  note     : " + img["note"])

        return "\n".join(lines)

    @mcp.tool()
    async def itop_get_attachment(url: str) -> str:
        """Download a single image attachment from iTop and return it as a base64 data URI.

        The URL must use the /webservices/ajax.document.php path, not /pages/.
        Use the link provided by itop_get_ticket_images which already contains
        the correct /webservices/ endpoint and auth_token parameter.

        If the URL does not already contain auth_token it will be appended
        automatically. /pages/ is silently rewritten to /webservices/.

        Accepts URLs of the form:
          .../webservices/ajax.document.php?operation=download_document&id=...
          .../webservices/ajax.document.php?operation=download_inlineimage&id=...&s=...

        A HEAD request is sent first to verify the Content-Type without
        downloading the full body. Only image/* responses are downloaded.
        Downloads are capped at 5 MB. Non-image attachments are rejected.

        Args:
            url: Full iTop ajax.document.php URL for the attachment.
        """
        if not url or "ajax.document.php" not in url:
            return "Error: url must be an iTop ajax.document.php attachment URL."

        url = url.replace("/pages/ajax.document.php", "/webservices/ajax.document.php")

        if "auth_token=" not in url:
            token = get_token()
            sep = "&" if "?" in url else "?"
            url = url + sep + "auth_token=" + token

        if MCP_DEBUG:
            logger.debug("itop_get_attachment HEAD %s", url)

        async with _get_http_client() as client:
            try:
                head_resp = await client.head(url, follow_redirects=True)
                head_resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                return "Error: HEAD request failed with HTTP " + str(e.response.status_code) + "."
            except httpx.HTTPError as e:
                return "Error: HEAD request failed: " + str(e)

            content_type = head_resp.headers.get("content-type", "")
            if MCP_DEBUG:
                logger.debug("itop_get_attachment HEAD content-type=%s", content_type)

            if not _is_image(content_type):
                ct_clean = content_type.split(";")[0].strip() or "unknown"
                return (
                    "Skipped: attachment is not an image (content-type: " + ct_clean + "). "
                    "Only image/* attachments are downloaded by this tool."
                )

            try:
                async with client.stream("GET", url, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    chunks = []
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > _MAX_DL_BYTES:
                            return (
                                "Error: attachment exceeds the "
                                + str(_MAX_DL_BYTES // (1024 * 1024))
                                + " MB size limit and was not downloaded."
                            )
                        chunks.append(chunk)
            except httpx.HTTPStatusError as e:
                return "Error: GET request failed with HTTP " + str(e.response.status_code) + "."
            except httpx.HTTPError as e:
                return "Error: GET request failed: " + str(e)

        raw = b"".join(chunks)
        mime = content_type.split(";")[0].strip()
        b64 = base64.b64encode(raw).decode("ascii")
        data_uri = "data:" + mime + ";base64," + b64

        if MCP_DEBUG:
            logger.debug(
                "itop_get_attachment downloaded %d bytes mime=%s", len(raw), mime
            )

        return data_uri
