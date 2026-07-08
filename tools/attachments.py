"""
Attachment tools: download images attached to iTop tickets.
"""

from __future__ import annotations

import base64

import httpx

from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger

# Maximum bytes to download for an attachment (5 MB)
_MAX_BYTES = 5 * 1024 * 1024

# Content-type prefixes accepted as images
_IMAGE_PREFIXES = ("image/",)


def _get_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT)


def _is_image(content_type: str) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    return any(ct.startswith(p) for p in _IMAGE_PREFIXES)


def register(mcp, get_token):
    """Register all attachment tools on the given mcp instance."""

    @mcp.tool()
    async def itop_get_attachment(url: str) -> str:
        """Download an image attachment from iTop and return it as a base64 data URI.

        Accepts an iTop ajax.document.php URL of the form:
          .../pages/ajax.document.php?operation=download_inlineimage&...
          .../pages/ajax.document.php?operation=download_document&...

        A HEAD request is sent first to check the Content-Type without
        downloading the full body. Only image/* responses are downloaded.
        Downloads are capped at 5 MB. Non-image attachments are rejected
        with an informative message.

        Args:
            url: Full iTop ajax.document.php URL for the attachment.
        """
        if not url or "ajax.document.php" not in url:
            return "Error: url must be an iTop ajax.document.php attachment URL."

        token = get_token()
        headers = {"Authorization": f"Bearer {token}"}

        if MCP_DEBUG:
            logger.debug("itop_get_attachment HEAD %s", url)

        async with _get_http_client() as client:
            # Step 1: HEAD to check content-type without downloading body
            try:
                head_resp = await client.head(url, headers=headers, follow_redirects=True)
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

            # Step 2: GET with size cap
            try:
                async with client.stream(
                    "GET", url, headers=headers, follow_redirects=True
                ) as resp:
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            return (
                                f"Error: attachment exceeds the {_MAX_BYTES // (1024*1024)} MB "
                                "size limit and was not downloaded."
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
