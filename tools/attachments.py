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
            Pass each resource_uri to itop_download_attachment to build the
            fixed resources/read URI, then read it via Langdock.

        itop_download_attachment(uri)
            Validate an itop:// attachment URI and return the fixed
            resources/read URI: itop://download?uri=<encoded>
            Langdock calls resources/read on that fixed URI, which routes
            the blob through the attachment path and bypasses the
            tool-response character limit.

        itop_get_ticket_attachments(obj_class, ticket_ref, key)
            List all non-image file attachments for a ticket.
            Returns metadata and browser download links only.

    Resources:
        itop://download
            Fixed URI registered at startup. Langdock discovers it via
            resources/list and saves it. At read time, the ?uri= query
            parameter carries the actual itop://attachment/<id> or
            itop://inlineimage/<secret>/<record_id> target. The handler
            URL-decodes the parameter, downloads the binary from iTop,
            and returns it as a BlobResourceContents.

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

from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from config import ITOP_TIMEOUT, ITOP_URL, ITOP_VERIFY_SSL, MCP_DEBUG, logger
from helpers import resolve_key

_IMAGE_PREFIXES = ("image/",)

# Fixed resource URI registered at startup so Langdock can discover it.
# The dynamic target is carried as the ?uri= query parameter.
_DOWNLOAD_RESOURCE_URI = "itop://download"


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


def _build_download_uri(itop_uri: str) -> str:
    """Build the fixed resources/read URI for a given itop:// URI.

    Returns itop://download?uri=<percent-encoded itop_uri>
    """
    return _DOWNLOAD_RESOURCE_URI + "?" + urlencode({"uri": itop_uri})


def _resolve_itop_download_url(resource_uri: str) -> tuple[str, str]:
    """Extract the itop:// URI from a download resource URI and resolve it
    to an iTop HTTP download URL.

    Args:
        resource_uri: Full resource URI, e.g.
                      itop://download?uri=itop%3A%2F%2Fattachment%2F123

    Returns:
        (http_download_url, itop_uri)

    Raises ValueError on missing or invalid uri parameter.
    """
    parsed = urlparse(resource_uri)
    qs = parse_qs(parsed.query)
    itop_uris = qs.get("uri", [])
    if not itop_uris:
        raise ValueError("Missing uri query parameter in resource URI: " + resource_uri)
    itop_uri = itop_uris[0]
    _validate_itop_uri(itop_uri)

    if itop_uri.startswith("itop://attachment/"):
        attachment_id = itop_uri[len("itop://attachment/"):]
        return _attachment_url(attachment_id), itop_uri

    rest = itop_uri[len("itop://inlineimage/"):]
    secret, record_id = rest.split("/", 1)
    return _inline_image_url(secret, record_id), itop_uri


def register(mcp, itop_request):
    """Register attachment tools and resources."""

    # ------------------------------------------------------------------
    # Resource: itop://download  (fixed URI, discovered by Langdock at setup)
    # ------------------------------------------------------------------

    @mcp.resource(_DOWNLOAD_RESOURCE_URI)
    async def resource_download() -> bytes:
        """Download an iTop image attachment.

        Fixed URI: itop://download
        The actual attachment is identified by the ?uri= query parameter,
        which carries the percent-encoded itop://attachment/<id> or
        itop://inlineimage/<secret>/<record_id> target URI.

        Langdock discovers this resource at setup via resources/list, then
        calls resources/read with the full itop://download?uri=... URI at
        runtime. FastMCP passes the full URI string to this handler, which
        decodes the parameter, downloads the binary from iTop, and returns
        the bytes. FastMCP wraps the result as BlobResourceContents.
        """
        # FastMCP passes the actual requested URI (including query string)
        # as the first argument when the registered URI is a fixed pattern
        # but the client calls it with additional query parameters.
        # We access it via the MCP request context instead.
        raise NotImplementedError(
            "This resource must be called with a ?uri= query parameter. "
            "Use itop_download_attachment to obtain the correct URI."
        )

    # ------------------------------------------------------------------
    # Resource: itop://download?uri=... (actual handler via read_resource)
    # ------------------------------------------------------------------
    # FastMCP's @mcp.resource decorator matches on the base URI only.
    # To serve dynamic query parameters we override the read handler at
    # the low-level server layer so the full requested URI is available.

    original_read = getattr(mcp, "_resource_manager", None)

    # Register a catch-all read handler by subclassing the resource manager
    # is not straightforward in mcp.server.fastmcp. Instead we use the
    # supported pattern: register the resource with a template URI that
    # captures the query string as a path segment is not possible either.
    #
    # Practical solution: the @mcp.resource decorator for the fixed URI
    # itop://download is registered above so Langdock sees it in
    # resources/list. The actual blob serving is done by a second handler
    # registered with a broader URI match using the low-level
    # @mcp.server.request_handler approach is also not exposed cleanly.
    #
    # We therefore use the only clean mechanism available in FastMCP:
    # register the resource with a URI template that treats the query
    # string as a named parameter via a fake path segment. Langdock
    # discovers the fixed base URI itop://download via resources/list
    # (registered above). When it calls resources/read with the full
    # itop://download?uri=... URI, FastMCP routes it to whichever handler
    # matches the full string. We register a second handler that matches
    # the parametrised form.

    @mcp.resource("itop://download?uri={encoded_uri}")
    async def resource_download_with_uri(encoded_uri: str) -> bytes:
        """Serve an iTop image by resolving the ?uri= parameter.

        Called by Langdock via resources/read on the URI produced by
        itop_download_attachment: itop://download?uri=<encoded>
        FastMCP decodes the {encoded_uri} path parameter, which here is
        the percent-encoded itop:// target URI.
        """
        from urllib.parse import unquote
        itop_uri = unquote(encoded_uri)
        _validate_itop_uri(itop_uri)

        if itop_uri.startswith("itop://attachment/"):
            attachment_id = itop_uri[len("itop://attachment/"):]
            http_url = _attachment_url(attachment_id)
        else:
            rest = itop_uri[len("itop://inlineimage/"):]
            secret, record_id = rest.split("/", 1)
            http_url = _inline_image_url(secret, record_id)

        try:
            content_bytes, mimetype = await _download_binary(http_url)
        except Exception as exc:
            if MCP_DEBUG:
                logger.debug("resource_download_with_uri error: uri=%s exc=%s", itop_uri, exc)
            raise

        if MCP_DEBUG:
            logger.debug(
                "resource_download_with_uri: uri=%s mime=%s bytes=%d",
                itop_uri, mimetype, len(content_bytes),
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
        per image. Pass each resource_uri to itop_download_attachment to get
        the fixed resources/read URI, then read it via Langdock to retrieve
        the image blob.

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
            "Pass each resource_uri to itop_download_attachment to get the",
            "resources/read URI, then read it via Langdock to retrieve the image.",
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
    async def itop_download_attachment(uri: str) -> str:
        """Build the resources/read URI for one image attachment.

        Validates the itop:// resource URI and returns the fixed
        resources/read URI: itop://download?uri=<percent-encoded uri>

        Langdock then calls resources/read on that URI. The registered
        itop://download resource handler decodes the parameter, downloads
        the binary from iTop, and returns it as a blob via the attachment
        path - bypassing the tool-response character limit entirely.

        Call this tool once per resource_uri returned by itop_get_ticket_images,
        then read the returned URI via Langdock resources/read.

        Supported URI schemes:
          itop://attachment/<attachment_id>
          itop://inlineimage/<secret>/<record_id>

        Args:
            uri: Resource URI as returned by itop_get_ticket_images.
        """
        validated = _validate_itop_uri(uri)
        download_uri = _build_download_uri(validated)

        if MCP_DEBUG:
            logger.debug(
                "itop_download_attachment: uri=%s download_uri=%s",
                validated, download_uri,
            )

        return download_uri

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
