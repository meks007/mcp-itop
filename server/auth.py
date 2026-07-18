"""
Authentication: bearer token and ItopClient per-request context.

ItopMiddleware stores both the raw bearer token and the shared ItopClient
instance in ContextVars so that every tool handler and resource handler can
reach them without an explicit parameter.

Token validation is performed by _validate_itop_token() which caches results
server-side with a sliding TTL to avoid redundant iTop round-trips. A token
is evicted immediately when iTop returns code==1 (UNAUTH) on any REST call.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from client import ItopClient, _redact_secret, set_client
from config import logger

# ---------------------------------------------------------------------------
# Token validation cache
# ---------------------------------------------------------------------------

TOKEN_CACHE_TTL: float = 60.0  # seconds, sliding window


@dataclass
class _TokenEntry:
    valid: bool        # True = iTop accepted the token
    last_seen: float   # time.monotonic() of last hit


# Keyed by SHA-256 hex digest of the raw token -- never store the token itself.
_TOKEN_CACHE: dict[str, _TokenEntry] = {}

# Per-token locks: prevent concurrent first-time probes for the same token.
_TOKEN_LOCKS: dict[str, asyncio.Lock] = {}
_CACHE_LOCK = asyncio.Lock()  # guards _TOKEN_CACHE and _TOKEN_LOCKS


async def _validate_itop_token(token: str) -> bool:
    """Validate a bearer token against iTop, using a sliding-TTL cache.

    FastMCP calls this before routing any MCP message (initialize,
    tools/list, tools/call, ...). Returns False to yield HTTP 401.
    """
    from client import itop_request  # local import avoids circular reference

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()

    # Fast path: cache hit -- no lock needed for a plain dict read.
    entry = _TOKEN_CACHE.get(token_hash)
    if entry is not None and (now - entry.last_seen) < TOKEN_CACHE_TTL:
        entry.last_seen = now  # slide the window
        return entry.valid

    # Slow path: probe iTop. Acquire a per-token lock so concurrent
    # first-time requests for the same token do not fire parallel probes.
    async with _CACHE_LOCK:
        if token_hash not in _TOKEN_LOCKS:
            _TOKEN_LOCKS[token_hash] = asyncio.Lock()
        token_lock = _TOKEN_LOCKS[token_hash]

    async with token_lock:
        # Re-check after acquiring the lock -- another coroutine may have
        # already completed the probe while we were waiting.
        now = time.monotonic()
        entry = _TOKEN_CACHE.get(token_hash)
        if entry is not None and (now - entry.last_seen) < TOKEN_CACHE_TTL:
            entry.last_seen = now
            return entry.valid

        # Probe iTop with the cheapest available operation.
        # itop_request will not trigger evict_token here because no cache
        # entry exists yet for this token (evict is a no-op in that case).
        try:
            result = await itop_request(
                {"operation": "list_operations"},
                get_bearer_token=lambda: token,
            )
            valid = result.get("code", -1) == 0
        except Exception:
            valid = False

        _TOKEN_CACHE[token_hash] = _TokenEntry(
            valid=valid, last_seen=time.monotonic()
        )
        logger.debug(
            "[auth] token validated: valid=%s prefix=%s",
            valid,
            _redact_secret(token),
        )
        return valid


async def evict_token(token: str) -> None:
    """Remove a token from the validation cache.

    Called by itop_request() whenever iTop returns code==1 (UNAUTH).
    Safe to call even when the token is not cached (no-op in that case).
    Cleans up the corresponding asyncio.Lock to prevent memory leaks.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    async with _CACHE_LOCK:
        removed = _TOKEN_CACHE.pop(token_hash, None)
        _TOKEN_LOCKS.pop(token_hash, None)
    if removed is not None:
        logger.warning(
            "[auth] token evicted from cache (UNAUTH code=1): prefix=%s",
            _redact_secret(token),
        )


async def evict_stale_token_cache() -> None:
    """Remove all token cache entries that have exceeded TOKEN_CACHE_TTL.

    Called periodically by housekeeping_loop() in background_tasks.py.
    """
    now = time.monotonic()
    async with _CACHE_LOCK:
        stale = [
            h for h, e in _TOKEN_CACHE.items()
            if now - e.last_seen >= TOKEN_CACHE_TTL
        ]
        for h in stale:
            _TOKEN_CACHE.pop(h, None)
            _TOKEN_LOCKS.pop(h, None)
    if stale:
        logger.debug(
            "[auth] evict_stale_token_cache: removed %d stale entry/entries",
            len(stale),
        )


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
