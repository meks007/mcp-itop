"""
Authentication: bearer token verifier and per-request token accessor.
"""

from __future__ import annotations

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier

from config import logger


class ItopBearerVerifier(TokenVerifier):
    """Validates presence of a bearer token at MCP handshake time.

    The token itself is the caller's iTop REST/JSON API auth_token. We do
    not (and cannot, without calling iTop) verify it is a valid iTop
    token here - only that the client presented a non-empty bearer value.
    An invalid/expired token will simply be rejected by iTop on the first
    real REST call made through itop_* tools.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not token.strip():
            return None  # causes MCP handshake to fail with 401
        return AccessToken(token=token, client_id="itop-client", scopes=[])


def get_bearer_token(mcp) -> str:
    """Return the iTop auth_token supplied by the connected client.

    Each client authenticates to this MCP server with its own iTop REST
    API token via "Authorization: Bearer <itop_token>". The SDK's
    AuthContextMiddleware stores the verified token in a contextvar;
    get_access_token() retrieves it for the current request.
    """
    access_token = get_access_token()
    if access_token is None or not access_token.token:
        raise ValueError(
            "No iTop auth token found on this connection. Connect with an "
            "'Authorization: Bearer <itop_token>' header."
        )
    return access_token.token
