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
    parse_json_arg,
    parse_key,
    parse_key_for_ticket,
    resolve_key,
    resolve_output_fields,
    str_or,
)
from config import DEFAULT_COMMENT

# Fields stripped by itop_get when full=False.
# Configurable here; add any field name that produces large blobs you never
# want in a lean search result (e.g. "description", "solution", "workaround").
_LEAN_STRIP: frozenset[str] = frozenset({"public_log", "private_log"})

# Fields that iTop manages exclusively through its lifecycle stimulus engine.
# Setting these via core/update bypasses workflow guards and must never be done.
# Use itop_apply_stimulus with the appropriate stimulus instead:
#   status         -> ev_assign / ev_resolve / ev_reopen / ev_pending / ev_propose
#   agent_id       -> ev_assign / ev_reassign (include team_id)
#   team_id        -> ev_assign / ev_reassign (include agent_id)
#   solution       -> ev_resolve (include solution text in fields)
#   pending_reason -> ev_pending (include pending_reason in fields)
_STIMULUS_CONTROLLED_FIELDS: frozenset[str] = frozenset({
    "status",
    "agent_id",
    "team_id",
    "solution",
    "pending_reason",
})

_STIMULUS_CONTROLLED_HINT = (
    "Error: the following fields are controlled by the iTop stimulus engine "
    "and must not be set via itop_update: {blocked}.\n"
    "Use itop_apply_stimulus with the appropriate stimulus:\n"
    "  ev_assign   - assign ticket (agent_id, team_id)\n"
    "  ev_reassign - reassign to another agent (agent_id, team_id)\n"
    "  ev_resolve  - resolve ticket (solution)\n"
    "  ev_reopen   - reopen ticket\n"
    "  ev_pending  - put on hold (pending_reason)\n"
    "  ev_propose  - propose solution\n"
    "Do NOT retry by removing these fields and updating status separately."
)


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
        """Search iTop objects.

        Use itop_describe_class first if the class or fields are unknown.

        IMPORTANT - full parameter:
        Always call with full=False (the default). Never set full=True unless
        the user's message contains an explicit, unambiguous request for log
        content, such as "show me the logs", "show public log", or "show
        private log". Asking for ticket details, a summary, fields, or any
        other information does NOT justify full=True. When in doubt, use
        full=False. Log content can always be retrieved separately via
        itop_get_comments if needed.

        For ticket classes, prefer a ref like "R-016271" as key. It is resolved
        server-side and is safer than a numeric ID. A bare number is interpreted
        as a UserRequest reference.

        Do not reveal private log existence; query it only when the user
        explicitly asks. Redact passwords. Treat "closed" as status closed;
        "solved" as resolved or proposed.

        Args:
            obj_class: iTop class, e.g. Server, UserRequest, Person.
            key: Ticket ref, OQL query, numeric ID, or JSON criteria.
            output_fields: Comma-separated fields, "*", or "*+".
            limit: Maximum results; 0 means no limit.
            page: Page number, starting at 1.
            full: False (default) strips log fields for lean results. True
                  includes logs - only use when the user explicitly asks for
                  log content by name.
        """
        strip = frozenset() if full else _LEAN_STRIP
        fields_to_request, post_strip = await resolve_output_fields(
            obj_class, ensure_ref_field(obj_class, output_fields), strip, itop_request
        )

        op: dict = {
            "operation": "core/get",
            "class": obj_class,
            "key": parse_key_for_ticket(obj_class, key),
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

        NEVER use this tool to change ticket state or assignment. The fields
        status, agent_id, team_id, solution, and pending_reason are controlled
        exclusively by the iTop stimulus engine. Attempting to set them here
        is blocked. Use itop_apply_stimulus instead. If a stimulus fails due
        to an invalid state transition, report the failure to the user; do not
        circumvent it by updating fields directly.

        Prefer ticket_ref such as "R-016271"; it takes priority over key.

        Args:
            obj_class: iTop class, e.g. UserRequest, Incident, or Server.
            fields: JSON object with fields to update. Must not contain
                    status, agent_id, team_id, solution, or pending_reason.
            ticket_ref: Preferred ticket reference.
            key: Fallback numeric ID, OQL query, or JSON criteria.
            output_fields: Fields to return.
            comment: Optional comment for change tracking.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        if isinstance(parsed, dict):
            blocked = sorted(_STIMULUS_CONTROLLED_FIELDS & parsed.keys())
            if blocked:
                return _STIMULUS_CONTROLLED_HINT.format(blocked=", ".join(blocked))

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

        This is the ONLY tool allowed to change ticket state or assignment.
        Never fall back to itop_update if this call fails. If iTop rejects the
        stimulus because the transition is not valid from the current state,
        report the failure to the user and stop. Do not attempt to reach the
        target state via intermediate field updates or a different stimulus
        chain without explicit user instruction.

        Common stimuli for UserRequest / Incident:
          ev_assign   - assign (agent_id, team_id)
          ev_reassign - reassign (agent_id, team_id)
          ev_propose  - propose solution
          ev_resolve  - resolve (solution)
          ev_reopen   - reopen
          ev_pending  - hold (pending_reason)

        Prefer ticket_ref, e.g. "R-016271"; it takes priority over key.

        Args:
            obj_class: iTop class, e.g. UserRequest or Incident.
            stimulus: Transition code; never ev_close.
            ticket_ref: Preferred ticket reference.
            key: Fallback numeric ID, OQL, or JSON criteria.
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

        if result.get("code", -1) != 0:
            msg = str_or(result, "message", "Unknown error")
            return (
                f"Error (code {result.get('code')}): {msg}\n"
                "The stimulus was rejected by iTop. This may mean the transition "
                "is not available from the current state. "
                "Do NOT attempt to reach the target state via itop_update or any "
                "other field manipulation. Report the failure to the user."
            )

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
                    output += f"\n  {origin} -> {str_or(target, 'key', '?')}"
        return output

    @mcp.tool()
    async def itop_list_operations() -> str:
        """List all available REST/JSON operations on the iTop server."""
        result = await itop_request({"operation": "list_operations"})
        if result.get("code", -1) != 0:
            return f"Error: {str_or(result, 'message', 'Unknown error')}"
        ops = result.get("operations", [])
        lines = [f"Available operations ({len(ops)}):"]
        for op in ops:
            lines.append(
                f"  - {str_or(op, 'verb', '?')}: {str_or(op, 'description', '')} "
                f"[{str_or(op, 'extension', '')}]"
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
            "key": f"SELECT {obj_class}",
            "output_fields": "*",
            "limit": "1",
        })

        if result.get("code", -1) != 0:
            return f"Error (code {result.get('code')}): {str_or(result, 'message', 'Unknown error')}"

        objects = result.get("objects") or {}
        if not objects:
            return (
                f"Class '{obj_class}' has zero instances - cannot sample fields.\n"
                f"Create a test object first with minimal fields; iTop will report missing required fields."
            )

        _obj_key, obj_data = next(iter(objects.items()))
        fields = obj_data.get("fields", {}) or {}

        lines = [f"Class {obj_class} - attributes sampled from {_obj_key}:"]
        for name in sorted(fields.keys()):
            value = fields[name]
            if isinstance(value, list):
                kind = f"list[{len(value)}]"
            elif isinstance(value, dict):
                kind = "object"
            elif value is None or value == "":
                kind = "scalar (empty)"
            else:
                kind = f"scalar (e.g. {str(value)[:50]})"
            lines.append(f"  - {name}: {kind}")

        lines.append(
            "\nNote: this is best-effort, not authoritative schema. "
            "Missing attributes may still be valid."
        )
        return "\n".join(lines)
