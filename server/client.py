"""
iTop REST/JSON API HTTP client.

Public context API
------------------
get_client()      Return the ItopClient bound to the current async context.
set_client(c)     Bind an ItopClient to the current async context.

Both are called by ItopMiddleware (auth.py) so that every request handler
and helper function can reach the client without an explicit parameter.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

import httpx

from config import (
    ITOP_TIMEOUT,
    ITOP_URL,
    ITOP_VERSION,
    ITOP_VERIFY_SSL,
    MCP_DEBUG,
    MCP_DEBUG_HEADERS,
    logger,
)

# ---------------------------------------------------------------------------
# Module-level HTTP client (shared, lazy-init)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT)
    return _http_client


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

def _redact_secret(value: object, visible_chars: int = 7) -> str:
    """Mask a secret while retaining its first characters for identification."""
    secret = str(value)
    if len(secret) <= visible_chars:
        return "***REDACTED***"
    return f"{secret[:visible_chars]}***REDACTED***"


def _redact_form_data(data: dict) -> dict:
    """Return a copy of the iTop form-data dict with auth secrets masked."""
    redacted = dict(data)
    for key in ("auth_token", "auth_pwd"):
        if key in redacted and redacted[key]:
            redacted[key] = _redact_secret(redacted[key])
    return redacted


def _redact_headers(headers: httpx.Headers) -> dict:
    """Return a plain dict of headers with the Authorization value redacted."""
    redacted = {}
    for name, value in headers.items():
        if name.lower() == "authorization":
            if value.lower().startswith("bearer "):
                token_part = value[len("bearer "):]
                redacted[name] = f"Bearer {_redact_secret(token_part)}"
            else:
                redacted[name] = _redact_secret(value)
        else:
            redacted[name] = value
    return redacted


# ---------------------------------------------------------------------------
# Low-level request function
# ---------------------------------------------------------------------------

async def itop_request(operation: dict, get_bearer_token: Callable[[], str]) -> dict:
    """Send a raw operation dict to the iTop REST/JSON API.

    When iTop returns code==1 (UNAUTH), the token is immediately evicted
    from the validation cache so the next request is forced to re-validate.

    Args:
        operation:        iTop operation dict.
        get_bearer_token: Zero-argument callable returning the bearer token.
    """
    if not ITOP_URL:
        raise ValueError("ITOP_URL is not configured. Set it in .env or environment.")

    token = get_bearer_token()

    url = f"{ITOP_URL}/webservices/rest.php"
    data: dict[str, str] = {
        "version": ITOP_VERSION,
        "json_data": json.dumps(operation),
        "auth_token": token,
    }

    if MCP_DEBUG:
        logger.debug("MCP -> iTop  POST %s  data=%s", url, _redact_form_data(data))

    try:
        resp = await _get_http_client().post(url, data=data)

        if MCP_DEBUG and MCP_DEBUG_HEADERS:
            logger.debug(
                "MCP -> iTop  request headers=%s",
                _redact_headers(resp.request.headers),
            )
            logger.debug(
                "MCP <- iTop  response headers=%s",
                dict(resp.headers),
            )

        resp.raise_for_status()
        result: dict = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning(
            "iTop HTTP %s for op=%s", e.response.status_code, operation.get("operation")
        )
        if MCP_DEBUG:
            logger.debug(
                "MCP <- iTop  HTTP %s  body=%s",
                e.response.status_code,
                e.response.text[:2000],
            )
        return {
            "code": e.response.status_code,
            "message": f"HTTP {e.response.status_code}: {e.response.text[:300]}",
        }
    except httpx.HTTPError as e:
        logger.warning("iTop network error: %s", e)
        if MCP_DEBUG:
            logger.debug("MCP <- iTop  network error: %s", e)
        return {"code": -1, "message": f"Network error: {e}"}

    if result.get("code", 0) != 0:
        logger.warning(
            "iTop error code=%s op=%s msg=%s",
            result.get("code"),
            operation.get("operation"),
            result.get("message"),
        )

    # UNAUTH eviction: if iTop signals code==1 the token is no longer valid.
    # Import lazily to avoid the circular import (client <- auth <- client).
    if result.get("code") == 1:
        from auth import evict_token  # noqa: PLC0415
        asyncio.ensure_future(evict_token(token))

    if MCP_DEBUG:
        logger.debug(
            "MCP <- iTop  status=%s  response=%s",
            resp.status_code,
            json.dumps(result, ensure_ascii=False)[:4000],
        )

    return result


# ---------------------------------------------------------------------------
# ContextVar: current ItopClient for the active async context
# ---------------------------------------------------------------------------

_current_client: ContextVar["ItopClient | None"] = ContextVar(
    "_current_client", default=None
)


def get_client() -> "ItopClient":
    """Return the ItopClient bound to the current async context.

    Raises RuntimeError when no client has been set (i.e. the request did not
    pass through ItopMiddleware).
    """
    client = _current_client.get()
    if client is None:
        raise RuntimeError(
            "No ItopClient is bound to the current context. "
            "Ensure ItopMiddleware is installed and the request carries a bearer token."
        )
    return client


def set_client(client: "ItopClient") -> Token:
    """Bind an ItopClient to the current async context.

    Returns the reset Token so the caller can restore the previous value.
    """
    return _current_client.set(client)


# ---------------------------------------------------------------------------
# ItopClient
# ---------------------------------------------------------------------------

class ItopClient:
    """High-level async client for the iTop REST/JSON API.

    Args:
        get_bearer_token: Zero-argument callable returning the current bearer
                          token string for every outgoing request.
        on_request:       Optional async hook called at the start of every
                          request() invocation. Receives the ItopClient
                          instance. Used by server.py to trigger preheat_once()
                          without monkey-patching.
    """

    def __init__(
        self,
        get_bearer_token: Callable[[], str],
        *,
        on_request: Callable[["ItopClient"], Awaitable[None]] | None = None,
    ) -> None:
        self._get_bearer_token = get_bearer_token
        self._on_request = on_request

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------

    async def request(self, op: dict) -> dict:
        """Send a raw iTop REST/JSON operation dict.

        Runs the on_request hook (if set) before every call.
        """
        if self._on_request is not None:
            await self._on_request(self)
        return await itop_request(op, self._get_bearer_token)

    # ------------------------------------------------------------------
    # core/get
    # ------------------------------------------------------------------

    async def get(
        self,
        cls: str,
        key: str | int,
        fields: str = "*",
        limit: int | None = None,
        page: int | None = None,
    ) -> dict:
        """Wrapper for iTop core/get operations.

        Args:
            cls:    iTop class name, e.g. 'UserRequest'.
            key:    Numeric ID, OQL string, or ticket ref.
            fields: Comma-separated field names or '*' / '*+'.
            limit:  Max objects to return.
            page:   Page number for paginated results.
        """
        op: dict = {
            "operation": "core/get",
            "class": cls,
            "key": key,
            "output_fields": fields,
        }
        if limit is not None:
            op["limit"] = str(limit)
        if page is not None:
            op["page"] = str(page)
        return await self.request(op)

    # ------------------------------------------------------------------
    # core/create
    # ------------------------------------------------------------------

    async def create(
        self,
        cls: str,
        fields: dict,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> dict:
        """Create an iTop object via core/create."""
        return await self.request({
            "operation": "core/create",
            "class": cls,
            "fields": fields,
            "output_fields": output_fields,
            "comment": comment,
        })

    # ------------------------------------------------------------------
    # core/update
    # ------------------------------------------------------------------

    async def update(
        self,
        cls: str,
        key: str | int,
        fields: dict,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> dict:
        """Update fields on an existing iTop object via core/update."""
        return await self.request({
            "operation": "core/update",
            "class": cls,
            "key": key,
            "fields": fields,
            "output_fields": output_fields,
            "comment": comment,
        })

    # ------------------------------------------------------------------
    # core/delete
    # ------------------------------------------------------------------

    async def delete(
        self,
        cls: str,
        key: str | int,
        comment: str = "",
        simulate: bool = True,
    ) -> dict:
        """Delete an iTop object via core/delete.

        Args:
            cls:      iTop class name, e.g. 'UserRequest'.
            key:      Numeric ID or OQL string identifying the object(s).
            comment:  Audit comment recorded on the operation.
            simulate: When True (default) the deletion is only simulated;
                      no data is removed. Set to False only for real deletions.
        """
        return await self.request({
            "operation": "core/delete",
            "class": cls,
            "key": key,
            "simulate": simulate,
            "comment": comment,
        })

    # ------------------------------------------------------------------
    # core/apply_stimulus
    # ------------------------------------------------------------------

    async def apply_stimulus(
        self,
        cls: str,
        key: str | int,
        stimulus: str,
        fields: dict | None = None,
        output_fields: str = "ref, friendlyname, status",
        comment: str = "",
    ) -> dict:
        """Apply a lifecycle stimulus to an iTop object via core/apply_stimulus."""
        return await self.request({
            "operation": "core/apply_stimulus",
            "class": cls,
            "key": key,
            "stimulus": stimulus,
            "fields": fields or {},
            "output_fields": output_fields,
            "comment": comment,
        })

    # ------------------------------------------------------------------
    # list_operations
    # ------------------------------------------------------------------

    async def operations(self) -> dict:
        """List all available REST/JSON operations on the iTop server."""
        return await self.request({"operation": "list_operations"})
