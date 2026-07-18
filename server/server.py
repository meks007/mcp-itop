#!/usr/bin/env python3
"""
MCP server for iTop ITSM - analytics, tickets, KB, assets.

Provides AI assistants (Claude Desktop, opencode, etc.) with tools to:
  - Analyse SLA compliance, agent workload, service quality
  - Query and update tickets, CI, KB articles via iTop REST API
  - Apply lifecycle transitions (assign, resolve, close)

Module layout:
  config.py             - env vars, logging, constants
  cache.py              - class field registry, resolve_key cache, token cache
  auth.py               - ItopMiddleware, get_bearer_token(), token validation
  client.py             - iTop REST/JSON HTTP client, ItopClient, get_client()
  helpers/              - shared formatting and parsing utilities
  db/                   - backend-agnostic database layer (db.init, db.execute)
  attachment_store/     - SQLite store for image URIs and inline image refs
  background_tasks.py   - central housekeeping asyncio loop
  tools/
    analytics.py        - SLA, workload, idle agents, service/caller quality
    kb.py               - knowledge base search and retrieval
    crud.py             - generic CRUD + stimulus + impact tools
    comments.py         - ticket log read/write
    attachments.py      - image and file attachment tools + static image resource

Framework: fastmcp (PrefectHQ) >= 2.11.0

Startup sequence
----------------
  1. Preflight checks (ITOP_URL present)
  2. db.init()                 -- synchronous; before any async work
  3. asyncio.run(_serve())
       a. housekeeping task created
       b. uvicorn starts, binds, MCP session manager starts
       c. Server ready and accepting connections
"""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.providers.debug import DebugTokenVerifier

from auth import ItopMiddleware, _validate_itop_token
from client import ItopClient
from config import MCP_DEBUG, MCP_DEBUG_HEADERS, logger

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
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "iTop",
    instructions=(
        "MCP server for iTop IT Service Management with analytics. "
        "Provides SLA reports, agent workload analysis, service quality checks, "
        "ticket lifecycle, KB search, and CI impact analysis."
    ),
    auth=DebugTokenVerifier(validate=_validate_itop_token),
)

# ---------------------------------------------------------------------------
# ItopClient -- process-global, token resolved per-request via ContextVar
# ---------------------------------------------------------------------------

# Single shared ItopClient. The bearer token is resolved lazily on every
# request via auth.get_bearer_token() inside client.py -- server.py does
# not need to import or pass it down.
client = ItopClient()

# ---------------------------------------------------------------------------
# Register all tools and resources
# ---------------------------------------------------------------------------

_analytics.register(mcp, client)
_attachments.register(mcp, client)
_kb.register(mcp, client)
_crud.register(mcp, client)
_comments.register(mcp, client)

# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

app = mcp.http_app(transport="streamable-http")

# ItopMiddleware sets both the bearer token and the ItopClient instance into
# ContextVars so that get_bearer_token() and get_client() work in every
# tool and resource handler.
app.add_middleware(ItopMiddleware, itop_client=client)

# ---------------------------------------------------------------------------
# Optional debug logging middleware
# ---------------------------------------------------------------------------

_REDACTED_REQUEST_HEADERS = frozenset({"authorization", "cookie"})
_REDACTED_RESPONSE_HEADERS = frozenset({"set-cookie"})
_REDACTED_PLACEHOLDER = "<redacted>"


def _format_headers(headers, redacted: frozenset) -> str:
    """Return a compact single-line representation of HTTP headers."""
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
        """Log every HTTP request/response between MCP client and this server."""

        async def dispatch(self, request: StarletteRequest, call_next):
            body = await request.body()

            if MCP_DEBUG_HEADERS:
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

            if MCP_DEBUG_HEADERS:
                logger.debug(
                    "CLIENT <- MCP  %s %s  status=%s  headers=[%s]",
                    request.method,
                    request.url.path,
                    response.status_code,
                    _format_headers(response.headers, _REDACTED_RESPONSE_HEADERS),
                )
            else:
                logger.debug(
                    "CLIENT <- MCP  %s %s  status=%s",
                    request.method,
                    request.url.path,
                    response.status_code,
                )
            return response

    app.add_middleware(DebugLoggingMiddleware)
    logger.debug("Client<->MCP HTTP debug logging middleware attached.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _serve() -> None:
    from background_tasks import housekeeping_loop

    # Start housekeeping before uvicorn so it is running when the first
    # request arrives. The task runs for the lifetime of the process.
    asyncio.create_task(housekeeping_loop())
    logger.info("[server] housekeeping task started")

    config = uvicorn.Config(
        app,
        host=_MCP_HOST,
        port=_MCP_PORT,
        log_config=None,
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    """Run the iTop MCP server."""
    from config import ITOP_URL

    if not ITOP_URL:
        print("Error: ITOP_URL is not set.", file=sys.stderr)
        print("Create .env file with ITOP_URL (see .env.example)", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 1. Database -- synchronous init before any async work starts.
    #    Tool modules imported above (tools.attachments -> attachment_store
    #    -> session.py / refs.py) have already called db.register_schema()
    #    at import time, so all DDL is queued before this call runs it.
    # ------------------------------------------------------------------
    import db
    db.init()
    logger.info("[server] db backend ready")

    # ------------------------------------------------------------------
    # 2. Start uvicorn. _serve() creates the housekeeping task first,
    #    then hands control to uvicorn.
    # ------------------------------------------------------------------
    logger.info(
        "Starting iTop MCP server on %s:%d (debug=%s)",
        _MCP_HOST, _MCP_PORT, MCP_DEBUG,
    )
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
