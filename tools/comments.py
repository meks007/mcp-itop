"""
Comment tools: add ticket log entries.

Reading logs: use itop_get with full=True -- public_log and private_log are
included in the full record, so a separate log-fetch call is never needed.
"""

from __future__ import annotations

from typing import Optional, Union

from helpers import format_and_cache, resolve_key


def register(mcp, itop_request):
    """Register all comment tools on the given mcp instance."""

    @mcp.tool(
        name="Add_comment_to_ticket"
    )
    async def itop_add_comment(
        ticket_class: str,
        text: str,
        ticket_ref: Optional[str] = None,
        ticket_id: Optional[Union[int, str]] = None,
        is_public: bool = True,
        format: str = "text",
    ) -> str:
        """Add a public or private log entry to an iTop ticket.

        Public comments are portal-visible; use private comments only when explicitly
        required. Prefer ticket_ref; bare ticket IDs are resolved automatically.
        To read existing comments, use itop_get with full=True."""
        if not ticket_ref and not ticket_id:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        log_field = "public_log" if is_public else "private_log"

        ref = str(ticket_ref or ticket_id or "").strip() or None
        ticket_class, key = await resolve_key(ticket_class, ref, itop_request)

        result = await itop_request({
            "operation": "core/update",
            "class": ticket_class,
            "key": key,
            "fields": {
                log_field: {
                    "add_item": {
                        "message": text,
                        "format": format,
                    }
                }
            },
            "output_fields": "id, ref, friendlyname",
            "comment": "MCP: added " + ("public" if is_public else "private") + " comment",
        })
        return format_and_cache(result)
