"""
iTop REST/JSON API HTTP client.
"""

from __future__ import annotations

import json

import httpx

from config import (
    ITOP_TIMEOUT,
    ITOP_URL,
    ITOP_VERSION,
    ITOP_VERIFY_SSL,
    MCP_DEBUG,
    logger,
)

_http_client: httpx.AsyncClient | None = None


def _redact_secret(value: object, visible_chars: int = 7) -> str:
    """Mask a secret while retaining its first characters for identification."""
    secret = str(value)
    if len(secret) <= visible_chars:
        return "***REDACTED***"
    return f"{secret[:visible_chars]}***REDACTED***"


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT)
    return _http_client


def _redact_form_data(data: dict) -> dict:
    """Return a copy of the iTop form-data dict with auth secrets masked, for safe logging."""
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
            # Redact the token part after 'Bearer '
            if value.lower().startswith("bearer "):
                token_part = value[len("bearer "):]
                redacted[name] = f"Bearer {_redact_secret(token_part)}"
            else:
                redacted[name] = _redact_secret(value)
        else:
            redacted[name] = value
    return redacted


async def itop_request(operation: dict, get_bearer_token) -> dict:
    """Send request to iTop REST/JSON API.

    Args:
        operation: The iTop operation dict.
        get_bearer_token: Callable that returns the current bearer token string.
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

        if MCP_DEBUG:
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

    if MCP_DEBUG:
        logger.debug(
            "MCP <- iTop  status=%s  response=%s",
            resp.status_code,
            json.dumps(result, ensure_ascii=False)[:4000],
        )

    return result
