"""
Comment tools: add ticket log entries.

Reading logs: use itop_get with full=True -- public_log and private_log are
included in the full record, so a separate log-fetch call is never needed.
"""

from __future__ import annotations

from typing import Optional, Union

from helpers import format_objects, resolve_key, resolve_ticket_ref


def register(mcp, itop_request):
    """Register all comment tools on the given mcp instance."""

    @mcp.tool()
    async def itop_add_comment(
        ticket_class: str,
        text: str,
        ticket_ref: Optional[str] = None,
        ticket_id: Optional[Union[int, str]] = None,
        is_public: bool = True,
        format: str = "text",
    ) -> str:
        """Add a public or private comment to an iTop ticket.
        
        Public comments are portal-visible; private comments are agent-only. If the
        user does not specify visibility, use the public log.
        
        Read comments with itop_get and full=True. The full record contains public_log
        and, when authorized, private_log. No separate log tool exists.
        
        Prefer ticket_ref, for example "R-016271". A bare ticket_id such as "15525"
        is resolved to its real class and reference through Ticket. Use
        ticket_class="Ticket" when the concrete class is unknown.
        
        Args:
            ticket_class: UserRequest, Incident, or "Ticket" when unknown.
            text: Comment text.
            ticket_ref: Preferred ticket reference, for example "R-016271".
            ticket_id: Bare number or numeric ID when the ref is unknown.
            is_public: True for public_log; False for private_log.
            format: "text" or "html"."""
        if not ticket_ref and not ticket_id:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        log_field = "public_log" if is_public else "private_log"

        # If a bare number is given without a ref, resolve class + ref first.
        if not ticket_ref and ticket_id:
            resolved_class, key = await resolve_ticket_ref(
                ticket_class, str(ticket_id), itop_request
            )
            ticket_class = resolved_class
        else:
            # resolve_key now returns (resolved_class, numeric_key); override class.
            ticket_class, key = await resolve_key(
                ticket_class,
                ticket_ref or None,
                str(ticket_id) if ticket_id else None,
                itop_request,
            )

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
        return format_objects(result)
