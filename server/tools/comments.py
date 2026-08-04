"""
Comment tools: add ticket log entries.

Reading logs: use Load_object with full=True -- public_log and private_log are
included in the full record, so a separate log-fetch call is never needed.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from client import ItopClient
from helpers import coerce_ref, format_and_cache, resolve_key

_VALID_LOG_FIELDS = {"public_log", "private_log", "private_log_ai"}


def register(mcp, client: ItopClient):
    """Register all comment tools on the given mcp instance."""

    @mcp.tool(
        name="itop_add_comment"
    )
    async def itop_add_comment(
        ticket_class: str,
        text: str,
        ticket_ref: Optional[str] = None,
        ticket_id: Optional[Union[int, str]] = None,
        log_field: Literal["public_log", "private_log", "private_log_ai"] = "public_log",
    ) -> str:
        """Add an HTML log entry to an iTop ticket.

        Use log_field='public_log' to write a portal-visible comment or interim report (default).
        Use log_field='private_log' for an internal note.
        Prefer ticket_ref; bare ticket IDs are resolved automatically.
        To read existing comments, use Load_object with full=True."""
        if not ticket_ref and not ticket_id:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        if log_field not in _VALID_LOG_FIELDS:
            return "Error: log_field must be one of " + ", ".join(sorted(_VALID_LOG_FIELDS)) + "."

        ticket_class, key = await resolve_key(
            ticket_class, coerce_ref(ticket_ref or "", ticket_id or "")
        )

        result = await client.update(
            ticket_class,
            key,
            {
                log_field: {
                    "add_item": {
                        "message": text,
                        "format": "html",
                    }
                }
            },
            output_fields="id, ref, friendlyname",
            comment="MCP: added comment to " + log_field,
        )
        return format_and_cache(result)
