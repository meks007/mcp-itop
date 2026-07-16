#!/usr/bin/env python3
"""
MCP server for iTop ITSM - analytics, tickets, KB, assets.

Provides AI assistants (Claude Desktop, opencode, etc.) with tools to:
  - Analyse SLA compliance, agent workload, service quality
  - Query and update tickets, CI, KB articles via iTop REST API
  - Apply lifecycle transitions (assign, resolve, close)

Based on josephstreeter/mcp_itop (CRUD + stimulus) with extended analytics.

Module layout:
  config.py           - env vars, logging, constants
  auth.py             - BearerTokenMiddleware ContextVar + get_bearer_token()
  client.py           - iTop REST/JSON HTTP client
  helpers.py          - shared formatting and parsing utilities
  attachment_store.py - SQLite store for image URIs (session-keyed by token)
  tools/
    analytics.py      - SLA, workload, idle agents, service/caller quality
    kb.py             - knowledge base search and retrieval
    crud.py           - generic CRUD + stimulus + impact tools
    comments.py       - ticket log read/write
    attachments.py    - image and file attachment tools + static image resource

Framework: fastmcp (PrefectHQ) >= 2.11.0
  - ResourceResult / ResourceContent for multi-image resource responses
  - DebugTokenVerifier: accepts any non-empty bearer token (iTop validates
    the actual token on every REST call)
  - BearerTokenMiddleware (auth.py): stores the raw token in a ContextVar
    so get_bearer_token() works in both tool and resource handlers
"""

from __future__ import annotations

import os
import sys

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.providers.debug import DebugTokenVerifier

from auth import BearerTokenMiddleware, get_bearer_token
from client import itop_request as _raw_itop_request
from config import MCP_DEBUG, logger

import tools.analytics as _analytics
import tools.attachments as _attachments
import tools.comments as _comments
import tools.crud as _crud
import tools.kb as _kb

# ---------------------------------------------------------------------------
# Server config
# ---------------------------------------------------------------------------

_MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
_MCP_PORT = int(os.getenv("MCP_PORT", "8096"))

# ---------------------------------------------------------------------------
# FastMCP instance
# DebugTokenVerifier accepts any non-empty bearer token.
# iTop itself validates whether the token is a real/valid iTop API key.
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "iTop",
    instructions=(
        "MCP server for iTop IT Service Management with analytics. "
        "Provides SLA reports, agent workload analysis, service quality checks, "
        "ticket lifecycle, KB search, and CI impact analysis."
    ),
    auth=DebugTokenVerifier(),
)


# ---------------------------------------------------------------------------
# Bind bearer token to itop_request
# ---------------------------------------------------------------------------

async def itop_request(operation: dict) -> dict:
    """Wrapper that injects the per-request bearer token into the HTTP client."""
    return await _raw_itop_request(operation, get_bearer_token)


def _get_token() -> str:
    """Return the current per-request bearer token (for non-REST callers)."""
    return get_bearer_token()


# ---------------------------------------------------------------------------
# Register all tools and resources
# ---------------------------------------------------------------------------

_analytics.register(mcp, itop_request)
# Pass get_token_fn so attachments.py can write to the SQLite store and
# read it back inside the static itop://attachment/images resource handler.
_attachments.register(mcp, itop_request, _get_token)
_kb.register(mcp, itop_request)
_crud.register(mcp, itop_request)
_comments.register(mcp, itop_request)


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

app = mcp.http_app(transport="streamable-http")

# Inject BearerTokenMiddleware so get_bearer_token() works in resource
# handlers (which run outside the fastmcp tool-call context).
app.add_middleware(BearerTokenMiddleware)

# ---------------------------------------------------------------------------
# Optional debug logging middleware
# ---------------------------------------------------------------------------

# Headers that contain secrets and must never appear in logs in cleartext.
_REDACTED_REQUEST_HEADERS = frozenset({"authorization", "cookie"})
_REDACTED_RESPONSE_HEADERS = frozenset({"set-cookie"})
_REDACTED_PLACEHOLDER = "<redacted>"


def _format_headers(headers, redacted: frozenset[str]) -> str:
    """Return a compact single-line representation of HTTP headers.

    Headers whose lowercase name appears in the redacted set have their
    value replaced with <redacted> so secrets never appear in log output.
    """
    parts = []
    for name, value in headers.items():
        if name.lower() in redacted:
            parts.append(name + ": " + _REDACTED_PLACEHOLDER)
        else:
            parts.append(name + ": " + value)
    return " | ".join(parts) if parts else "(none)"


if MCP_DEBUG:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    class DebugLoggingMiddleware(BaseHTTPMiddleware):
        """Log every HTTP request/response between MCP client and this server.

        Headers and body are emitted as separate log lines per direction.
        Response body is not logged because the streamable-http transport
        uses chunked/SSE streaming that cannot be buffered here without
        breaking the connection.
        """

        async def dispatch(self, request: StarletteRequest, call_next):
            body = await request.body()

            logger.debug(
                "CLIENT -> MCP  %s %s  headers=[%s]",
                request.method,
                request.url.path,
                _format_headers(request.headers, _REDACTED_REQUEST_HEADERS),
            )
            logger.debug(
                "CLIENT -> MCP  %s %s  body=%s",
                request.method,
                request.url.path,
                body[:2000].decode(errors="replace") if body else "(empty)",
            )

            response = await call_next(request)

            logger.debug(
                "CLIENT <- MCP  %s %s  status=%s  headers=[%s]",
                request.method,
                request.url.path,
                response.status_code,
                _format_headers(response.headers, _REDACTED_RESPONSE_HEADERS),
            )
            return response

    app.add_middleware(DebugLoggingMiddleware)
    logger.debug("Client<->MCP HTTP debug logging middleware attached.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the iTop MCP server via uvicorn (Streamable HTTP).

    iTop authentication is supplied per-client via an
    'Authorization: Bearer <itop_token>' HTTP header. The token is
    forwarded to iTop on every REST call; actual validity is enforced
    by iTop. MCP_DEBUG=true enables verbose request/response logging.
    """
    from config import ITOP_URL

    if not ITOP_URL:
        print("Error: ITOP_URL is not set.", file=sys.stderr)
        print("Create .env file with ITOP_URL (see .env.example)", file=sys.stderr)
        sys.exit(1)

    # Open the SQLite attachment store eagerly so any permission or path
    # problem surfaces immediately at startup, not on the first tool call.
    import attachment_store
    attachment_store.init_db()

    logger.info(
        "Starting iTop MCP server on %s:%d (debug=%s)",
        _MCP_HOST, _MCP_PORT, MCP_DEBUG,
    )
    uvicorn.run(app, host=_MCP_HOST, port=_MCP_PORT)


if __name__ == "__main__":
    main()
