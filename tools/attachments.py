"""
Attachment tools: fetch and download images attached to iTop tickets.

Public API
----------
register(mcp, get_token, itop_request)
    Registers the following MCP tools:

    itop_get_ticket_images(obj_class, ticket_ref, key)
        Fetch all image attachments for a ticket via the iTop REST API.
        - Images <= 100 KB  -> inline base64 data URI + download link
        - Images >  100 KB  -> download link only, with a note to fetch separately
          using itop_get_attachment

    itop_get_attachment(url)
        Download a single attachment by its ajax.document.php URL and return
        it as a base64 data URI. Hard-capped at 5 MB. Behaviour unchanged.
"""

from __future__ import annotations

import base64

import httpx

from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger
from helpers import resolve_key

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

# Images at or below this raw byte count are returned with an inline base64
# data URI. Larger images get a link and a note.
B64_INLINE_LIMIT: int = 100 * 1024  # 100 KB

# Hard download cap for itop_get_attachment.
_MAX_DL_BYTES: int = 5 * 1024 * 1024  # 5 MB

# MIME prefixes recognised as images.
_IMAGE_PREFIXES = ("image/",)


# ---------------------------------------------------------------------------
# Shared helpers (not MCP tools)
# ---------------------------------------------------------------------------

def _get_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT)


def _is_image(content_type: str) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    return any(ct.startswith(p) for p in _IMAGE_PREFIXES)


def _download_url(attachment_id: str | int) -> str:
    """Build the ajax.document.php download URL for a given attachment ID."""
    return (
        f"{ITOP_URL}/webservices/ajax.document.php"
        f"?operation=download_document&id={attachment_id}"
    )


async def _fetch_image_attachments(
    obj_class: str,
    obj_id: int | str,
    itop_request,
) -> list[dict]:
    """Return image attachments for a ticket as a list of dicts.

    Each dict has:
      id        - iTop attachment key (str)
      filename  - original filename (str)
      mimetype  - MIME type (str)
      raw_bytes - decoded size in bytes (int)
      b64       - base64 string when size <= B64_INLINE_LIMIT, else None
      url       - ajax.document.php download link (str)
      note      - non-empty message when b64 is suppressed (str)

    Non-image attachments are silently skipped.
    Errors from iTop (e.g. Attachment class not installed) return an empty list.
    """
    result = await itop_request({
        "operation": "core/get",
        "class": "Attachment",
        "key": (
            f"SELECT Attachment"
            f" WHERE item_class = '{obj_class}'"
            f" AND item_id = {obj_id}"
        ),
        "output_fields": "filename, mimetype, contents",
    })

    if result.get("code", -1) != 0:
        if MCP_DEBUG:
            logger.debug(
                "_fetch_image_attachments error: code=%s msg=%s",
                result.get("code"),
                result.get("message"),
            )
        return []

    images: list[dict] = []

    for obj_key, obj_data in (result.get("objects") or {}).items():
        fields = obj_data.get("fields") or {}
        mimetype = (fields.get("mimetype") or "").strip()

        if not _is_image(mimetype):
            continue

        attachment_id = obj_data.get("key") or obj_key.split("::")[-1]
        filename = fields.get("filename") or f"attachment_{attachment_id}"

        # iTop returns file contents as a compound {"mimetype": ..., "data": "<b64>"}
        # or occasionally as a plain base64 string.
        contents = fields.get("contents") or {}
        b64_raw: str = (
            contents.get("data", "") if isinstance(contents, dict) else str(contents)
        )

        try:
            raw_bytes = len(base64.b64decode(b64_raw, validate=False)) if b64_raw else 0
        except Exception:
            raw_bytes = 0

        if 0 < raw_bytes <= B64_INLINE_LIMIT:
            b64_out: str | None = b64_raw
            note = ""
        else:
            b64_out = None
            if raw_bytes > B64_INLINE_LIMIT:
                note = (
                    f"Image size ({raw_bytes // 1024} KB) exceeds the "
                    f"{B64_INLINE_LIMIT // 1024} KB inline limit. "
                    "Use itop_get_attachment with the link to fetch it separately."
                )
            else:
                note = "Image data not available inline from the REST API."

        images.append({
            "id": str(attachment_id),
            "filename": filename,
            "mimetype": mimetype,
            "raw_bytes": raw_bytes,
            "b64": b64_out,
            "url": _download_url(attachment_id),
            "note": note,
        })

        if MCP_DEBUG:
            logger.debug(
                "_fetch_image_attachments: id=%s file=%s mime=%s bytes=%d inline=%s",
                attachment_id, filename, mimetype, raw_bytes, b64_out is not None,
            )

    return images


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, get_token, itop_request):
    """Register attachment tools. Signature extended: now also accepts itop_request."""

    @mcp.tool()
    async def itop_get_ticket_images(
        obj_class: str,
        ticket_ref: str = "",
        key: str = "",
    ) -> str:
        """Fetch all image attachments for an iTop ticket.

        Queries the iTop Attachment class for every attachment linked to the
        given ticket, filters for image/* MIME types, and returns for each:

          - filename, MIME type, size
          - a download link (always present)
          - an inline base64 data URI when the image is <= 100 KB
          - a note directing to itop_get_attachment when the image is > 100 KB

        For ticket classes (UserRequest, Incident, etc.) prefer ticket_ref
        (e.g. "R-016271"); it is resolved automatically and takes priority
        over key. Use key (numeric ID or OQL) for non-ticket classes.

        Args:
            obj_class:  iTop class, e.g. UserRequest, Incident.
            ticket_ref: Preferred ticket reference, e.g. "R-016271".
            key:        Fallback numeric ID or OQL query.
        """
        resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )
        if resolved is None:
            return "Error: provide either ticket_ref or key to identify the ticket."

        images = await _fetch_image_attachments(obj_class, resolved, itop_request)

        if not images:
            return f"No image attachments found for {obj_class} {ticket_ref or key}."

        label = ticket_ref or key or str(resolved)
        lines: list[str] = [
            f"Image attachments for {obj_class} {label} ({len(images)} found):"
        ]

        for img in images:
            size_str = (
                f"{img['raw_bytes'] // 1024} KB"
                if img["raw_bytes"] >= 1024
                else f"{img['raw_bytes']} B"
            )
            lines.append(f"\n--- {img['filename']} ---")
            lines.append(f"  mimetype : {img['mimetype']}")
            lines.append(f"  size     : {size_str}")
            lines.append(f"  link     : {img['url']}")
            if img["b64"]:
                lines.append(f"  data_uri : data:{img['mimetype']};base64,{img['b64']}")
            else:
                lines.append(f"  note     : {img['note']}")

        return "\n".join(lines)

    @mcp.tool()
    async def itop_get_attachment(url: str) -> str:
        """Download a single image attachment from iTop and return it as a base64 data URI.

        Accepts an iTop ajax.document.php URL of the form:
          .../webservices/ajax.document.php?operation=download_inlineimage&...
          .../webservices/ajax.document.php?operation=download_document&...

        A HEAD request is sent first to verify the Content-Type without
        downloading the full body. Only image/* responses are downloaded.
        Downloads are capped at 5 MB. Non-image attachments are rejected
        with an informative message.

        Args:
            url: Full iTop ajax.document.php URL for the attachment.
        """
        if not url or "ajax.document.php" not in url:
            return "Error: url must be an iTop ajax.document.php attachment URL."

        token = get_token()
        sep = "&" if "?" in url else "?"
        auth_url = f"{url}{sep}auth_token={token}"

        if MCP_DEBUG:
            logger.debug("itop_get_attachment HEAD %s", url)

        async with _get_http_client() as client:
            try:
                head_resp = await client.head(auth_url, follow_redirects=True)
                head_resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                return f"Error: HEAD request failed with HTTP {e.response.status_code}."
            except httpx.HTTPError as e:
                return f"Error: HEAD request failed: {e}"

            content_type = head_resp.headers.get("content-type", "")
            if MCP_DEBUG:
                logger.debug("itop_get_attachment HEAD content-type=%s", content_type)

            if not _is_image(content_type):
                ct_clean = content_type.split(";")[0].strip() or "unknown"
                return (
                    f"Skipped: attachment is not an image (content-type: {ct_clean}). "
                    "Only image/* attachments are downloaded by this tool."
                )

            try:
                async with client.stream("GET", auth_url, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > _MAX_DL_BYTES:
                            return (
                                f"Error: attachment exceeds the "
                                f"{_MAX_DL_BYTES // (1024 * 1024)} MB size limit "
                                "and was not downloaded."
                            )
                        chunks.append(chunk)
            except httpx.HTTPStatusError as e:
                return f"Error: GET request failed with HTTP {e.response.status_code}."
            except httpx.HTTPError as e:
                return f"Error: GET request failed: {e}"

        raw = b"".join(chunks)
        mime = content_type.split(";")[0].strip()
        b64 = base64.b64encode(raw).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"

        if MCP_DEBUG:
            logger.debug(
                "itop_get_attachment downloaded %d bytes mime=%s", len(raw), mime
            )

        return data_uri
