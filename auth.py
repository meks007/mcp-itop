"""
Authentication: bearer token verifier and per-request token accessor.

Uses fastmcp (PrefectHQ) DebugTokenVerifier which accepts any non-empty
bearer token. The token itself is the caller's iTop REST/JSON API auth_token.
Actual validity is enforced by iTop on every REST call - we only check that
a non-empty token was presented.

The raw token string is stored in a module-level ContextVar by the Starlette
middleware defined here (BearerTokenMiddleware) and read back via
get_bearer_token() from tool and resource handlers.
"""

from __future__ import annotations

from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from client import _redact_secret
from config import logger

# ---------------------------------------------------------------------------
# ContextVar holding the raw bearer token for the current request
# ---------------------------------------------------------------------------

_bearer_token_var: ContextVar[str] = ContextVar("bearer_token", default="")


def get_bearer_token() -> str:
    """Return the iTop auth_token for the current request.

    Reads from the ContextVar populated by BearerTokenMiddleware.
    Raises ValueError when no token is present (unauthenticated request
    that somehow bypassed the fastmcp auth layer).
    """
    token = _bearer_token_var.get()
    logger.debug(
        "[auth] get_bearer_token: token present=%s len=%d token_prefix=%s",
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


# ---------------------------------------------------------------------------
# Starlette middleware: extract and store bearer token per request
# ---------------------------------------------------------------------------

class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Extract the Authorization: Bearer token and store it in a ContextVar.

    This runs on every HTTP request so that get_bearer_token() works in both
    tool handlers and resource handlers regardless of how fastmcp routes them.
    Requests without a token are passed through unchanged - fastmcp's own
    auth layer (DebugTokenVerifier) will reject them with 401 before any
    tool or resource handler is reached.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[len("bearer "):].strip()

        logger.debug(
            "[auth] BearerTokenMiddleware: path=%s token_present=%s len=%d token_prefix=%s",
            request.url.path,
            bool(token),
            len(token),
            _redact_secret(token) if token else "n/a",
        )

        # Store token in ContextVar for this request's async context.
        _token_reset = _bearer_token_var.set(token)
        try:
            response = await call_next(request)
        finally:
            _bearer_token_var.reset(_token_reset)

        return response
