"""
Comment tools: add and read ticket log entries.
"""

from __future__ import annotations

import re
from typing import Optional, Union

from helpers import extract_objects, format_objects, resolve_key, resolve_ticket_ref, str_or


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

    @mcp.tool()
    async def itop_get_log(
        ticket_class: str,
        ticket_ref: Optional[str] = None,
        ticket_id: Optional[Union[int, str]] = None,
        log_type: str = "public",
    ) -> str:
        """Read public and/or private comments from an iTop ticket.

        Read only public log per default. Do not mention the existence of the
        private log and only query it when the user asks for it.

        Prefer ticket_ref, for example "R-016271", whenever it is available from
        a previous result. A bare number like "15525" in ticket_id is resolved
        automatically -- the real class and ref are looked up via the Ticket base
        class (SELECT Ticket WHERE ref LIKE '%015525'). Pass ticket_class as
        "Ticket" when the class is not known.

        Redact or skip anything that resembles a password.

        Args:
            ticket_class: Ticket class, e.g. UserRequest, Incident, or "Ticket".
            ticket_ref: Preferred ticket reference, e.g. "R-016271".
            ticket_id: Bare number or fallback numeric ID when ref is unknown.
            log_type: "public", "private", or "both".
        """
        if not ticket_ref and not ticket_id:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        fields = []
        if log_type in ("public", "both"):
            fields.append("public_log")
        if log_type in ("private", "both"):
            fields.append("private_log")

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
            "operation": "core/get",
            "class": ticket_class,
            "key": key,
            "output_fields": ",".join(fields),
        })

        tickets = extract_objects(result)
        label = ticket_ref or str(ticket_id)
        if not tickets:
            return "Ticket " + repr(label) + " (" + ticket_class + ") not found."

        f = tickets[0]["fields"]
        lines = ["**Logs for " + ticket_class + " " + label + "**", ""]

        for field in fields:
            if field not in f:
                continue
            lines.append("--- " + ("Public Log" if field == "public_log" else "Private Log") + " ---")
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
                    lines.append("[" + date + "] " + user + ": " + msg[:200])
            elif isinstance(log_data, str):
                lines.append(log_data[:500])
            else:
                lines.append("(no entries)")
            lines.append("")

        return "\n".join(lines)
