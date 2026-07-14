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

        Public comments are visible in the end-user portal. Private comments are
        visible only to agents. When the user does not specify it, always assume
        the public log.

        To READ log entries, use itop_get with full=True instead -- the full
        record already contains public_log (and private_log when authorised).
        Do NOT call a separate log tool; none exists.

        Prefer ticket_ref, for example "R-016271", whenever it is available from
        a previous result. A bare number like "15525" in ticket_id is resolved
        automatically -- the real class and ref are looked up via the Ticket base
        class (SELECT Ticket WHERE ref LIKE '%015525'). Pass ticket_class as
        "Ticket" when the class is not known.

        Args:
            ticket_class: Ticket class, e.g. UserRequest, Incident, or "Ticket".
            text: Comment text.
            ticket_ref: Preferred ticket reference, e.g. "R-016271".
            ticket_id: Bare number or fallback numeric ID when ref is unknown.
            is_public: True for public_log; False for private_log.
            format: Comment format: "text" or "html".
        """
        if not ticket_ref and not ticket_id:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        log_field = "public_log" if is_public else "private_log"

        # If a bare number is given without a ref, resolve class + ref first
        if not ticket_ref and ticket_id:
            resolved_class, resolved_key = await resolve_ticket_ref(
                ticket_class, str(ticket_id), itop_request
            )
            ticket_class = resolved_class
            key = resolved_key
        else:
            key = await resolve_key(
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
