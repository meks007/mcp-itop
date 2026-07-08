#!/usr/bin/env python3
"""
MCP server for iTop ITSM - analytics, tickets, KB, assets.

Provides AI assistants (Claude Desktop, opencode, etc.) with tools to:
  - Analyse SLA compliance, agent workload, service quality
  - Query and update tickets, CI, KB articles via iTop REST API
  - Apply lifecycle transitions (assign, resolve, close)

Based on josephstreeter/mcp_itop (CRUD + stimulus) with extended analytics.

Module layout:
  config.py        - env vars, logging, constants
  auth.py          - bearer token verifier and accessor
  client.py        - iTop REST/JSON HTTP client
  helpers.py       - shared formatting and parsing utilities
  tools/
    analytics.py   - SLA, workload, idle agents, service/caller quality
    kb.py          - knowledge base search and retrieval
    crud.py        - generic CRUD + stimulus + impact tools
    comments.py    - ticket log read/write
"""

from __future__ import annotations

import os
import sys

import uvicorn
from pydantic import AnyHttpUrl
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from auth import ItopBearerVerifier, get_bearer_token
from client import itop_request as _raw_itop_request
from config import MCP_DEBUG, logger

import tools.analytics as _analytics
import tools.comments as _comments
import tools.crud as _crud
import tools.kb as _kb

# -- MCP server URL (used for AuthSettings) --------------------------------
# FastMCP requires issuer_url + resource_server_url when a token_verifier
# is supplied. We use the server's own listen address for both since this
# server is not a real OAuth issuer - it only validates that a non-empty
# bearer token was presented (the token's validity is enforced by iTop).
_MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
_MCP_PORT = int(os.getenv("MCP_PORT", "8096"))
# Use localhost for the URL even when binding to 0.0.0.0
_SERVER_URL = os.getenv(
    "MCP_SERVER_URL",
    f"http://{'localhost' if _MCP_HOST == '0.0.0.0' else _MCP_HOST}:{_MCP_PORT}",
)

# -- MCP instance ---------------------------------------------------------
mcp = FastMCP(
    "iTop",
    instructions=(
        "MCP server for iTop IT Service Management with analytics. "
        "Provides SLA reports, agent workload analysis, service quality checks, "
        "ticket lifecycle, KB search, and CI impact analysis."
    ),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(_SERVER_URL),
        resource_server_url=AnyHttpUrl(_SERVER_URL),
    ),
    token_verifier=ItopBearerVerifier(),
)


# -- Bind bearer token to itop_request ------------------------------------
async def itop_request(operation: dict) -> dict:
    """Wrapper that injects the per-request bearer token into the HTTP client."""
    return await _raw_itop_request(operation, lambda: get_bearer_token(mcp))


# -- Register all tools ---------------------------------------------------
_analytics.register(mcp, itop_request)
_kb.register(mcp, itop_request)
_crud.register(mcp, itop_request)
_comments.register(mcp, itop_request)


# -- ASGI app (for uvicorn) -----------------------------------------------
app = mcp.streamable_http_app()


# -- Entry point ----------------------------------------------------------
def main():
    """Run the iTop MCP server.

    Runs as a network-reachable Streamable HTTP server via uvicorn. iTop
    authentication is supplied per-client via an "Authorization: Bearer
    <itop_token>" header (see auth.py) - no ITOP_TOKEN / ITOP_USER /
    ITOP_PASSWORD environment variables are read for authentication
    purposes anymore.

    MCP_DEBUG=true enables verbose logging of all iTop REST/JSON API
    request/response payloads (auth secrets are always redacted).
    """
    from config import ITOP_URL

    if not ITOP_URL:
        print("Error: ITOP_URL is not set.", file=sys.stderr)
        print("Create .env file with ITOP_URL (see .env.example)", file=sys.stderr)
        sys.exit(1)

    uvicorn.run(app, host=_MCP_HOST, port=_MCP_PORT)


if __name__ == "__main__":
    main()
