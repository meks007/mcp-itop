"""
auth.py - Authentication: bearer token and ItopClient per-request context.

ItopMiddleware stores both the raw bearer token and the shared ItopClient
instance in ContextVars so that every tool handler and resource handler can
reach them without an explicit parameter.

Token validation is delegated to token_cache.validate() in cache.py which
caches results server-side with a sliding TTL to avoid redundant iTop
round-trips. A token is evicted immediately when iTop returns code==1
(UNAUTH) on any REST call.

Hashing
-------
get_bearer_token_hash() is the single authoritative place where the raw
bearer token is converted to a SHA-256 hex digest. cache.py stores and
looks up entries exclusively by this hash, so the raw token never enters
the cache layer.
"""

from __future__ import annotations

import hashlib
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from cache import token_cache
from client import ItopClient, _redact_secret, set_client
from config import logger


# ---------------------------------------------------------------------------
# Token validation -- delegated to token_cache
# ---------------------------------------------------------------------------

async def _validate_itop_token(token: str) -> bool:
    """Validate a bearer token against iTop, using the token_cache.

    FastMCP calls this before routing any MCP message (initialize,
    tools/list, tools/call, ...). Returns False to yield HTTP 401.

    The token is set into the ContextVar for the duration of the probe so
    that itop_request() can read it via get_bearer_token() as normal.
    """
    from client import itop_request  # local import avoids circular reference

    async def probe_fn() -> bool:
        token_reset = _bearer_token_var.set(token)
        try:
            result = await itop_request({"operation": "list_operations"})
            return result.get("code", -1) == 0
        except Exception:
            return False
        finally:
            _bearer_token_var.reset(token_reset)

    return await token_cache.validate(get_bearer_token_hash(token), probe_fn)


async def evict_token(token: str) -> None:
    """Remove a token from the validation cache.

    Called by itop_request() whenever iTop returns code==1 (UNAUTH).
    Safe to call even when the token is not cached (no-op in that case).
    """
    await token_cache.evict_by_token(get_bearer_token_hash(token))


# ---------------------------------------------------------------------------
# ContextVar: raw bearer token for the current request
# ---------------------------------------------------------------------------

_bearer_token_var: ContextVar[str] = ContextVar("bearer_token", default="")


def get_bearer_token() -> str:
    """Return the iTop auth_token for the current request.

    Reads from the ContextVar populated by ItopMiddleware.
    Raises ValueError when no token is present.
    """
    token = _bearer_token_var.get()
    logger.debug(
        "[auth] get_bearer_token: present=%s len=%d prefix=%s",
        bool(token),
        len(token),
        _redact_secret(token) if token else "n/a",
    )
    if not token:
        raise ValueError(
            "No iTop auth token found on this request. Connect with an "
            "'Authorization: Bearer <itop_token>' HTTP header."
        )
    return token


def get_bearer_token_hash(token: str | None = None) -> str:
    """Return the SHA-256 hex digest of the bearer token.

    When called without an argument the token is read from the current
    request context via get_bearer_token(). Pass an explicit token string
    when the raw value is already available (e.g. during token validation
    before the ContextVar has been set by ItopMiddleware).

    This is the single place in the codebase responsible for hashing the
    bearer token. cache.py accepts and stores only pre-computed hashes.
    """
    raw = token if token is not None else get_bearer_token()
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------

class ItopMiddleware(BaseHTTPMiddleware):
    """Set bearer token and ItopClient in the ContextVar for each request.

    Requests without a token are passed through unchanged; FastMCP's
    DebugTokenVerifier rejects them with 401 before any tool handler runs.
    """

    def __init__(self, app, itop_client: ItopClient) -> None:
        super().__init__(app)
        self._itop_client = itop_client

    async def dispatch(self, request: Request, call_next) -> Response:
        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[len("bearer "):].strip()

        logger.debug(
            "[auth] ItopMiddleware: path=%s token_present=%s len=%d prefix=%s",
            request.url.path,
            bool(token),
            len(token),
            _redact_secret(token) if token else "n/a",
        )

        token_reset = _bearer_token_var.set(token)
        client_reset = set_client(self._itop_client)
        try:
            response = await call_next(request)
        finally:
            _bearer_token_var.reset(token_reset)
            from client import _current_client
            _current_client.reset(client_reset)

        return response
