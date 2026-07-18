#!/usr/bin/env python3
"""
Configuration and logging setup for mcp-itop.
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

# Load config: global (~/.config/mcp-itop/.env) overrides project (.env)
_GLOBAL_ENV = os.path.expanduser("~/.config/mcp-itop/.env")
if os.path.isfile(_GLOBAL_ENV):
    load_dotenv(_GLOBAL_ENV, override=True)
load_dotenv()  # project .env (lower priority)

# -- Debug flag -----------------------------------------------------------
# Set MCP_DEBUG=true to log full request/response payloads for:
#   - every MCP tool call between client <-> mcp (via FastMCP middleware)
#   - every iTop REST/JSON API call between mcp <-> iTop
# Auth credentials (token, password) are always redacted from log output.
MCP_DEBUG = os.getenv("MCP_DEBUG", "false").lower() in ("true", "1", "yes")

# -- Header debug flag ----------------------------------------------------
# Set MCP_DEBUG_HEADERS=true to additionally log HTTP request and response
# headers for every iTop REST/JSON API call.  Only takes effect when
# MCP_DEBUG is also enabled.  Auth secrets in headers are always redacted.
MCP_DEBUG_HEADERS = os.getenv("MCP_DEBUG_HEADERS", "false").lower() in ("true", "1", "yes")

# -- All-loggers debug flag -----------------------------------------------
# Set MCP_DEBUG_ALL=true to additionally promote the following normally-
# suppressed third-party loggers to DEBUG level. Only takes effect when
# MCP_DEBUG is also enabled. Disabled by default because these loggers
# produce extremely large or high-frequency output:
#
#   sse_starlette / sse_starlette.sse
#       Raw SSE chunks. When base64-encoded data (e.g. attachments) is
#       transmitted, every chunk line can be hundreds of KB long.
#
#   httpcore / httpcore.connection / httpcore.http11 / httpcore.http2
#       Per-send/receive TCP and TLS lifecycle events (~8 lines per
#       iTop request). httpx already logs a clean one-liner at INFO.
#
#   PIL / PIL.TiffImagePlugin / PIL.PngImagePlugin
#       EXIF tag details and PNG stream offsets logged during image
#       normalisation.
#
# Leave this off unless you need very low-level transport debugging.
MCP_DEBUG_ALL = os.getenv("MCP_DEBUG_ALL", "false").lower() in ("true", "1", "yes")

# -- Logging --------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if MCP_DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-itop")

if MCP_DEBUG:
    # MCP session lifecycle loggers -- always enabled together with MCP_DEBUG
    # so that streamable-http session events appear alongside our own lines.
    for _lib_logger in (
        "mcp.server.streamable_http",
        "mcp.server",
    ):
        logging.getLogger(_lib_logger).setLevel(logging.DEBUG)

    # Low-level / high-volume loggers -- opt-in only via MCP_DEBUG_ALL.
    # When MCP_DEBUG_ALL is false we pin them to WARNING explicitly so they
    # stay quiet even though basicConfig set the root logger to DEBUG.
    _VERBOSE_LOGGERS = (
        "sse_starlette",
        "sse_starlette.sse",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "PIL",
        "PIL.TiffImagePlugin",
        "PIL.PngImagePlugin",
    )
    _verbose_level = logging.DEBUG if MCP_DEBUG_ALL else logging.WARNING
    for _lib_logger in _VERBOSE_LOGGERS:
        logging.getLogger(_lib_logger).setLevel(_verbose_level)

    logger.debug(
        "MCP_DEBUG is enabled - request/response payloads will be logged (secrets redacted)."
    )
    logger.debug(
        "Transport loggers set to DEBUG: mcp.server.streamable_http, mcp.server"
    )
    if MCP_DEBUG_ALL:
        logger.debug(
            "MCP_DEBUG_ALL is enabled - sse_starlette, httpcore and PIL loggers set to DEBUG."
        )
    else:
        logger.debug(
            "Low-level loggers suppressed (MCP_DEBUG_ALL not set): "
            "sse_starlette, httpcore, PIL. Set MCP_DEBUG_ALL=true to enable."
        )

    if MCP_DEBUG_HEADERS:
        logger.debug(
            "MCP_DEBUG_HEADERS is enabled - HTTP request/response headers will be logged."
        )

# -- Config ---------------------------------------------------------------
# NOTE: iTop authentication is no longer configured via environment
# variables. Each client must supply its own iTop REST API token as an
# HTTP "Authorization: Bearer <itop_token>" header when connecting to
# this MCP server. The server validates only that a non-empty bearer
# token was presented at connection time (MCP "initialize" handshake);
# the token's actual validity is enforced by iTop itself on every
# REST call (see client.py / auth.py).
ITOP_URL = os.getenv("ITOP_URL", "").rstrip("/")
ITOP_VERSION = os.getenv("ITOP_VERSION", "1.3")
ITOP_VERIFY_SSL = os.getenv("ITOP_VERIFY_SSL", "true").lower() not in ("false", "0", "no")
ITOP_TIMEOUT = float(os.getenv("ITOP_TIMEOUT", "30"))

DEFAULT_COMMENT = "Modified via MCP"

# -- Token validation cache -----------------------------------------------
# TTL in seconds for the bearer token validation cache (sliding window).
# The window resets on every request that hits the cache, so a token
# actively used within this interval is never re-validated against iTop.
# Set to 0 to disable caching (validate on every request).
# Default: 300 s (5 min).
TOKEN_CACHE_TTL: float = float(os.getenv("TOKEN_CACHE_TTL", "300"))

# -- resolve_key cache ----------------------------------------------------
# TTL in seconds for the resolve_key lookup cache.
# Entries older than this value are evicted by the housekeeping loop.
# Set to 0 to disable caching entirely.
RESOLVE_KEY_CACHE_TTL = int(os.getenv("RESOLVE_KEY_CACHE_TTL", "86400"))

# -- Housekeeping ---------------------------------------------------------
# Interval in seconds between background cleanup cycles.
# All periodic cleanup activities (resolve_key cache, inline image ref
# cache, SQLite expired rows) share this single interval.
# Default: 300 s (5 min).
CLEANUP_INTERVAL: int = int(os.getenv("CLEANUP_INTERVAL", "300"))

# -- Inline image ref cache -----------------------------------------------
# TTL in seconds for inline image ref entries written to SQLite by
# format_and_cache(). After this period the entry is treated as a cache
# miss and refreshed on the next itop_get_ticket_images call.
# Default: 3600 s (1 h).
INLINE_IMAGE_REF_TTL: int = int(os.getenv("INLINE_IMAGE_REF_TTL", "3600"))

# -- Attachment session TTL -----------------------------------------------
# How long (seconds) image entries stored by store_images() remain valid.
# Env var: IMAGE_STORE_TTL_SECONDS. Default: 3600 s (1 h).
IMAGE_STORE_TTL_SECONDS: int = int(os.getenv("IMAGE_STORE_TTL_SECONDS", "3600"))

# -- Image normalization --------------------------------------------------
# Maximum size in bytes for a stored image. Images exceeding this limit are
# first compressed via quality reduction, then scaled down if still too large.
# Default: 1 MB. Set to 0 to disable size capping entirely.
IMAGE_MAX_BYTES: int = int(os.getenv("IMAGE_MAX_BYTES", str(1 * 1024 * 1024)))

# Starting JPEG quality (1-95). Reduced in steps (75, 60, 45, 30) when the
# encoded size exceeds IMAGE_MAX_BYTES before falling back to downscaling.
# Default: 85.
IMAGE_JPEG_QUALITY: int = max(1, min(95, int(os.getenv("IMAGE_JPEG_QUALITY", "85"))))
