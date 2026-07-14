"""
Comment tools: add and read ticket log entries.
"""

from __future__ import annotations

import re
from typing import Optional, Union

from helpers import extract_objects, format_objects, resolve_key, str_or


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
        visible only to agents. When the user does not specify it, always assume the public log.

        Prefer ticket_ref, for example "R-016271", whenever it is available from a
        previous result. It is resolved automatically and takes priority over
        ticket_id. Use ticket_id only when no reference is known.

        Args:
            ticket_class: Ticket class, e.g. UserRequest, Incident, or Problem.
            text: Comment text.
            ticket_ref: Preferred ticket reference.
            ticket_id: Fallback numeric ID.
            is_public: True for public_log; False for private_log.
            format: Comment format: "text" or "html".
        """
        if not ticket_ref and ticket_id is None:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        log_field = "public_log" if is_public else "private_log"
        key = await resolve_key(
            ticket_class,
            ticket_ref or None,
            str(ticket_id) if ticket_id is not None else None,
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
            "comment": f"MCP: added {'public' if is_public else 'private'} comment",
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

        Read only public log per default. Do not mention the existence of the private log and only query it,
        when the user asks for it.

        Prefer ticket_ref, for example "R-016271", whenever it is available from a
        previous result. It is resolved automatically and takes priority over
        ticket_id. Use ticket_id only when no reference is known.

        Redact or skip anything that resembles a password.

        Args:
            ticket_class: Ticket class, e.g. UserRequest, Incident, or Problem.
            ticket_ref: Preferred ticket reference.
            ticket_id: Fallback numeric ID.
            log_type: "public", "private", or "both".
        """
        if not ticket_ref and ticket_id is None:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        fields = []
        if log_type in ("public", "both"):
            fields.append("public_log")
        if log_type in ("private", "both"):
            fields.append("private_log")

        key = await resolve_key(
            ticket_class,
            ticket_ref or None,
            str(ticket_id) if ticket_id is not None else None,
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
            return f"Ticket {label!r} ({ticket_class}) not found."

        f = tickets[0]["fields"]
        lines = [f"**Logs for {ticket_class} {label}**", ""]

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
