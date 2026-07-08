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

# -- Logging --------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if MCP_DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-itop")

if MCP_DEBUG:
    logger.debug(
        "MCP_DEBUG is enabled - request/response payloads will be logged (secrets redacted)."
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
