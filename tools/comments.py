"""
Comment tools: add and read ticket log entries.
"""

from __future__ import annotations

import re
from typing import Optional

from helpers import extract_objects, format_objects, parse_key_for_ticket, str_or


def register(mcp, itop_request):
    """Register all comment tools on the given mcp instance."""

    @mcp.tool()
    async def itop_add_comment(
        ticket_class: str,
        text: str,
        ticket_ref: Optional[str] = None,
        ticket_id: Optional[str] = None,
        is_public: bool = True,
        format: str = "text",
    ) -> str:
        """Add a comment to a ticket (public or private log).

        Public comments are visible to end users on the portal.
        Private comments are visible only to agents.

        Always use ticket_ref with the ref value (e.g. "R-016271") from a
        previous tool result. Do NOT guess or invent a numeric ID.

        Args:
            ticket_class: Ticket class (UserRequest, Incident, Problem).
            ticket_ref: Ticket ref (e.g. "R-016271"). Preferred - use this
                        when available from a previous tool result.
            ticket_id: Numeric ID string fallback (deprecated, use ticket_ref).
            text: Comment text.
            is_public: True = public_log, False = private_log.
            format: "text" or "html" (default: text).
        """
        ref = ticket_ref or ticket_id
        if not ref:
            return "Error: ticket_ref is required (e.g. 'R-016271')."

        log_field = "public_log" if is_public else "private_log"
        key = parse_key_for_ticket(ticket_class, str(ref))

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
            "comment": f"MCP: added {'public' if is_public else 'private'} comment",
        })
        return format_objects(result)

    @mcp.tool()
    async def itop_get_log(
        ticket_class: str,
        ticket_ref: Optional[str] = None,
        ticket_id: Optional[str] = None,
        log_type: str = "both",
    ) -> str:
        """Read log entries (comments) from a ticket.

        Always use ticket_ref with the ref value (e.g. "R-016271") from a
        previous tool result. Do NOT guess or invent a numeric ID.

        Args:
            ticket_class: Ticket class (UserRequest, Incident, Problem).
            ticket_ref: Ticket ref (e.g. "R-016271"). Preferred - use this
                        when available from a previous tool result.
            ticket_id: Numeric ID string fallback (deprecated, use ticket_ref).
            log_type: "public", "private", or "both" (default: both).
        """
        ref = ticket_ref or ticket_id
        if not ref:
            return "Error: ticket_ref is required (e.g. 'R-016271')."

        fields = []
        if log_type in ("public", "both"):
            fields.append("public_log")
        if log_type in ("private", "both"):
            fields.append("private_log")

        key = parse_key_for_ticket(ticket_class, str(ref))

        result = await itop_request({
            "operation": "core/get",
            "class": ticket_class,
            "key": key,
            "output_fields": ",".join(fields),
        })

        tickets = extract_objects(result)
        if not tickets:
            return f"Ticket {ref!r} ({ticket_class}) not found."

        f = tickets[0]["fields"]
        lines = [f"**Logs for {ticket_class} {ref}**", ""]

        for field in fields:
            if field not in f:
                continue
            lines.append(f"--- {'Public Log' if field == 'public_log' else 'Private Log'} ---")
            log_data = f[field]
            if isinstance(log_data, dict):
                # iTop 3.2.1 uses 'entries', older versions use 'items'
                items = log_data.get("entries") or log_data.get("items") or []
                if not items:
                    lines.append("(empty)")
                for item in items:
                    date = item.get("date", "?")[:19]
                    user = item.get("user_login", "?")
                    msg = item.get("message", "")
                    # Strip HTML tags for readability
                    msg = re.sub(r'<[^>]+>', '', msg)
                    lines.append(f"[{date}] {user}: {msg[:200]}")
            elif isinstance(log_data, str):
                lines.append(log_data[:500])
            else:
                lines.append("(no entries)")
            lines.append("")

        return "\n".join(lines)
