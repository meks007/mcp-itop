"""
CRUD and utility tools: get, create, update, delete, apply_stimulus,
get_related, list_operations, describe_class.
"""

from __future__ import annotations

from typing import Union

from helpers import (
    apply_field_strip,
    ensure_ref_field,
    format_objects,
    is_bare_number,
    parse_json_arg,
    parse_key,
    resolve_key,
    resolve_output_fields,
    resolve_ticket_ref,
    str_or,
    CLASSES_WITH_REF,
)
from config import DEFAULT_COMMENT

# Fields stripped by itop_get when full=False.
_LEAN_STRIP: frozenset[str] = frozenset({"private_log"})


def register(mcp, itop_request):
    """Register all CRUD tools on the given mcp instance."""

    @mcp.tool()
    async def itop_get(
        obj_class: str,
        key: str,
        output_fields: str = "*",
        limit: int = 25,
        page: int = 0,
        full: bool = False,
    ) -> str:
        """Search iTop objects. If class or fields are unknown, use itop_describe_class.

        BATCHING -- always combine multiple lookups for the same class into one call:
        - Multiple refs:    key="SELECT UserRequest WHERE ref IN ('R-001', 'R-002', 'R-003')"
        - Multiple numbers: key="SELECT Ticket WHERE ref LIKE '%001' OR ref LIKE '%002'"
        - Mixed criteria:   key="SELECT UserRequest WHERE status = 'open' OR ref = 'R-005'"
        Never call itop_get in a loop once per ticket/object when an OQL WHERE ... IN (...)
        or OR clause can fetch them all in a single call. Pass obj_class="Ticket" when the
        concrete class is unknown and let the OQL do the filtering.

        full controls whether suppressed fields (e.g. private_log) are included.
        Set full=True when the user asks for full details, complete information,
        everything, all fields, or the full record -- not just when logs are
        explicitly mentioned. Keep full=False for brief or summary queries.
        Do not reveal private logs without an explicit user request even when full=True.

        READING TICKET LOGS: public_log and private_log are part of the full
        record. To read comments or log entries, call itop_get with full=True.
        There is no separate log tool -- do NOT attempt to call itop_get_log or
        any other log-specific tool; use itop_get with full=True instead.

        For tickets, a full ref (R-016271) is direct. A bare number (15525) is resolved through Ticket to the real class and ref; use obj_class="Ticket" if unknown. OQL and JSON criteria pass through unchanged.

        Redact passwords. Treat "closed" as closed and "solved" as resolved or proposed.
        """
        # Resolve bare numbers and unknown class via Ticket base class lookup
        obj_class, resolved_key = await resolve_ticket_ref(obj_class, key, itop_request)

        strip = frozenset() if full else _LEAN_STRIP
        fields_to_request, post_strip = await resolve_output_fields(
            obj_class, ensure_ref_field(obj_class, output_fields), strip, itop_request
        )

        op: dict = {
            "operation": "core/get",
            "class": obj_class,
            "key": resolved_key,
            "output_fields": fields_to_request,
        }
        if limit > 0:
            op["limit"] = str(limit)
            if page > 0:
                op["page"] = str(page)

        result = await itop_request(op)

        if post_strip:
            apply_field_strip(result, post_strip)

        return format_objects(result)

    @mcp.tool()
    async def itop_create(
        obj_class: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Create an iTop object.

        Use itop_describe_class first if the required fields are unknown.

        Args:
            obj_class: iTop class, e.g. UserRequest, Server, or Person.
            fields: JSON object containing field values.
            output_fields: Comma-separated fields to return.
            comment: Optional comment for change tracking.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        result = await itop_request({
            "operation": "core/create",
            "class": obj_class,
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool()
    async def itop_update(
        obj_class: str,
        fields: str,
        ticket_ref: str = "",
        key: Union[str, int] = "",
        output_fields: str = "ref, friendlyname, status",
        comment: str = "",
    ) -> str:
        """Update fields on an existing iTop object.

        Do not set status with this tool. Ticket state changes must use
        itop_apply_stimulus, for example ev_assign, ev_resolve, ev_reopen, or
        ev_pending.

        For ticket classes, prefer ticket_ref such as "R-016271". A bare number
        like "15525" in key is resolved automatically via a Ticket base class
        lookup -- the real class and ref are determined server-side.

        Args:
            obj_class: iTop class, e.g. UserRequest, Incident, or "Ticket" when unknown.
            fields: JSON object with fields to update; must not include status.
            ticket_ref: Preferred ticket reference, e.g. "R-016271".
            key: Bare number, OQL, or JSON criteria when ref is unknown.
            output_fields: Fields to return.
            comment: Optional comment for change tracking.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        if isinstance(parsed, dict) and "status" in parsed:
            return (
                "Error: 'status' cannot be set via itop_update. "
                "iTop enforces status transitions through its workflow engine. "
                "Use itop_apply_stimulus with the appropriate stimulus instead:\n"
                "  ev_assign   - assign ticket\n"
                "  ev_resolve  - resolve ticket (include solution in fields)\n"
                "  ev_reopen   - reopen ticket\n"
                "  ev_pending  - put ticket on hold"
            )

        resolved = await resolve_key(obj_class, ticket_ref or None, key or None, itop_request)

        result = await itop_request({
            "operation": "core/update",
            "class": obj_class,
            "key": resolved,
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool()
    async def itop_delete(
        obj_class: str,
        ticket_ref: str = "",
        key: Union[str, int] = "",
        comment: str = "",
        simulate: bool = True,
    ) -> str:
        """Delete iTop object(s).

        Deletion is disabled by policy. Do not use this tool.

        For ticket classes, ticket_ref such as "R-016271" takes priority over key
        and is resolved automatically. Do not invent numeric IDs.

        Args:
            obj_class: iTop class.
            ticket_ref: Preferred ticket reference.
            key: Fallback numeric ID, OQL query, or JSON criteria.
            comment: Optional change comment.
            simulate: If true, performs a dry run without deleting.
        """
        resolved = await resolve_key(obj_class, ticket_ref or None, key or None, itop_request)

        result = await itop_request({
            "operation": "core/delete",
            "class": obj_class,
            "key": resolved,
            "simulate": simulate,
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool()
    async def itop_apply_stimulus(
        obj_class: str,
        stimulus: str,
        ticket_ref: str = "",
        key: Union[str, int] = "",
        fields: str = "{}",
        output_fields: str = "ref, friendlyname, status",
        comment: str = "",
    ) -> str:
        """Apply a lifecycle transition to an iTop object.

        Use this tool, not itop_update, to change ticket status. To finish a
        ticket, use ev_resolve with a solution in fields; ev_close is not allowed.

        For UserRequest and Incident, common stimuli are:
        - ev_assign: assign, with agent_id and team_id
        - ev_reassign: assign to another agent
        - ev_propose: propose a solution
        - ev_resolve: resolve, with solution
        - ev_reopen: reopen
        - ev_pending: put on hold, with pending_reason

        Prefer ticket_ref, for example "R-016271". A bare number like "15525"
        in key is resolved automatically via a Ticket base class lookup.

        Args:
            obj_class: iTop class, e.g. UserRequest or Incident, or "Ticket" when unknown.
            stimulus: Transition code; never ev_close.
            ticket_ref: Preferred ticket reference.
            key: Bare number, OQL, or JSON criteria when ref is unknown.
            fields: JSON transition fields.
            output_fields: Fields to return.
            comment: Optional change comment.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        if stimulus == "ev_close":
            return (
                "Error: ev_close is not permitted in this workflow. "
                "To close a ticket, use ev_resolve with a solution in fields, "
                'e.g. fields={"solution": "..."}. Resolving is the final step.'
            )

        resolved = await resolve_key(obj_class, ticket_ref or None, key or None, itop_request)

        result = await itop_request({
            "operation": "core/apply_stimulus",
            "class": obj_class,
            "key": resolved,
            "stimulus": stimulus,
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool()
    async def itop_get_related(
        obj_class: str,
        key: str,
        relation: str = "impacts",
        depth: int = 4,
        direction: str = "down",
        redundancy: bool = True,
    ) -> str:
        """Find CIs related to a given object via impact/dependency relations.

        Args:
            obj_class: iTop class (e.g. Server, ApplicationSolution).
            key: Object ID or OQL.
            relation: "impacts" or "depends on".
            depth: Traversal depth (max 20).
            direction: "down" or "up".
            redundancy: Account for redundancy in impact analysis.
        """
        result = await itop_request({
            "operation": "core/get_related",
            "class": obj_class,
            "key": parse_key(key),
            "relation": relation,
            "depth": depth,
            "direction": direction,
            "redundancy": redundancy,
        })
        output = format_objects(result)
        relations = result.get("relations")
        if relations:
            output += "\n\n--- Relations ---"
            for origin, targets in relations.items():
                for target in targets:
                    output += "\n  " + origin + " -> " + str_or(target, "key", "?")
        return output

    @mcp.tool()
    async def itop_list_operations() -> str:
        """List all available REST/JSON operations on the iTop server."""
        result = await itop_request({"operation": "list_operations"})
        if result.get("code", -1) != 0:
            return "Error: " + str_or(result, "message", "Unknown error")
        ops = result.get("operations", [])
        lines = ["Available operations (" + str(len(ops)) + "):"]
        for op in ops:
            lines.append(
                "  - " + str_or(op, "verb", "?") + ": "
                + str_or(op, "description", "") + " ["
                + str_or(op, "extension", "") + "]"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def itop_describe_class(obj_class: str) -> str:
        """Discover fields for an iTop class by sampling an existing object.

        Args:
            obj_class: iTop class name (e.g. Server, UserRequest, Person).
        """
        result = await itop_request({
            "operation": "core/get",
            "class": obj_class,
            "key": "SELECT " + obj_class,
            "output_fields": "*",
            "limit": "1",
        })

        if result.get("code", -1) != 0:
            return (
                "Error (code " + str(result.get("code")) + "): "
                + str_or(result, "message", "Unknown error")
            )

        objects = result.get("objects") or {}
        if not objects:
            return (
                "Class '" + obj_class + "' has zero instances - cannot sample fields.\n"
                "Create a test object first with minimal fields; iTop will report missing required fields."
            )

        _obj_key, obj_data = next(iter(objects.items()))
        fields = obj_data.get("fields", {}) or {}

        lines = ["Class " + obj_class + " - attributes sampled from " + _obj_key + ":"]
        for name in sorted(fields.keys()):
            value = fields[name]
            if isinstance(value, list):
                kind = "list[" + str(len(value)) + "]"
            elif isinstance(value, dict):
                kind = "object"
            elif value is None or value == "":
                kind = "scalar (empty)"
            else:
                kind = "scalar (e.g. " + str(value)[:50] + ")"
            lines.append("  - " + name + ": " + kind)

        lines.append(
            "\nNote: this is best-effort, not authoritative schema. "
            "Missing attributes may still be valid."
        )
        return "\n".join(lines)
