"""
Authentication: bearer token and ItopClient per-request context.

ItopMiddleware (formerly BearerTokenMiddleware) stores both the raw bearer
token and the shared ItopClient instance in ContextVars so that every tool
handler and resource handler can reach them without an explicit parameter.

The token is validated by iTop on every REST call. fastmcp's DebugTokenVerifier
only checks that a non-empty token was presented; actual API-key validity is
enforced downstream.
"""

from __future__ import annotations

from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from client import ItopClient, _redact_secret, set_client
from config import logger

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


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------

class ItopMiddleware(BaseHTTPMiddleware):
    """Set bearer token and ItopClient in the ContextVar for each request.

    Replaces the old BearerTokenMiddleware. Now also binds the shared
    ItopClient instance so that get_client() works in any handler without
    an explicit parameter.

    Requests without a token are passed through unchanged; fastmcp's
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
            # ContextVar reset restores the previous value (None outside requests).
            from client import _current_client
            _current_client.reset(client_reset)

        return response
