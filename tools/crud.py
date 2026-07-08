"""
CRUD and utility tools: get, create, update, delete, apply_stimulus,
get_related, list_operations, describe_class.
"""

from __future__ import annotations

from helpers import (
    ensure_ref_field,
    format_objects,
    parse_json_arg,
    parse_key,
    parse_key_for_ticket,
    str_or,
)
from config import DEFAULT_COMMENT


def register(mcp, itop_request):
    """Register all CRUD tools on the given mcp instance."""

    @mcp.tool()
    async def itop_get(
        obj_class: str,
        key: str,
        output_fields: str = "*",
        limit: int = 0,
        page: int = 0,
    ) -> str:
        """Search for objects in iTop.

        Args:
            obj_class: iTop class (e.g. Server, UserRequest, Person, Organization).
            key: OQL query (e.g. "SELECT Server WHERE name LIKE '%web%'"),
                 numeric ID, or JSON criteria.
            output_fields: Comma-separated fields, or "*" for all, or "*+" for subclass fields.
            limit: Max results (0 = no limit).
            page: Page number (starts at 1).
        """
        op: dict = {
            "operation": "core/get",
            "class": obj_class,
            "key": parse_key(key),
            "output_fields": ensure_ref_field(obj_class, output_fields),
        }
        if limit > 0:
            op["limit"] = str(limit)
            if page > 0:
                op["page"] = str(page)

        result = await itop_request(op)
        return format_objects(result)

    @mcp.tool()
    async def itop_create(
        obj_class: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Create a new object in iTop.

        Args:
            obj_class: iTop class (e.g. UserRequest, Server, Person).
            fields: JSON string of field values.
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
        key: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Update an existing object in iTop.

        Use this to modify fields on tickets, CI, etc.
        For lifecycle transitions (assign/resolve/close), use itop_apply_stimulus.

        For ticket classes (UserRequest, Incident, etc.) the key can be a
        ticket ref (e.g. "R-000123") and it will be resolved automatically.

        Args:
            obj_class: iTop class.
            key: Object ID, ref (e.g. "R-000123"), OQL, or JSON criteria.
            fields: JSON of fields to update.
            output_fields: Fields to return.
            comment: Optional comment for change tracking.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        result = await itop_request({
            "operation": "core/update",
            "class": obj_class,
            "key": parse_key_for_ticket(obj_class, key),
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool()
    async def itop_delete(
        obj_class: str,
        key: str,
        comment: str = "",
        simulate: bool = True,
    ) -> str:
        """Delete object(s) from iTop.

        For ticket classes (UserRequest, Incident, etc.) the key can be a
        ticket ref (e.g. "R-000123") and it will be resolved automatically.

        Args:
            obj_class: iTop class.
            key: Object ID, ref (e.g. "R-000123"), OQL, or JSON criteria.
            comment: Optional comment.
            simulate: If True, dry-run without deleting (default: True).
        """
        result = await itop_request({
            "operation": "core/delete",
            "class": obj_class,
            "key": parse_key_for_ticket(obj_class, key),
            "simulate": simulate,
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool()
    async def itop_apply_stimulus(
        obj_class: str,
        key: str,
        stimulus: str,
        fields: str = "{}",
        output_fields: str = "id, friendlyname, status",
        comment: str = "",
    ) -> str:
        """Apply a lifecycle stimulus to an iTop object (ticket state transition).

        Common stimuli for UserRequest/Incident:
          - ev_assign:   assign to agent (fields={"agent_id": <id>, "team_id": <id>})
          - ev_reassign: reassign to another agent
          - ev_resolve:  resolve ticket (fields={"solution": "..."})
          - ev_close:    close ticket
          - ev_reopen:   reopen ticket
          - ev_pending:  put on hold (fields={"pending_reason": "..."})

        The key can be a ticket ref (e.g. "R-000123") and it will be resolved
        automatically for ticket classes.

        Args:
            obj_class: iTop class (e.g. UserRequest, Incident).
            key: Object ID, ref (e.g. "R-000123"), OQL, or JSON criteria.
            stimulus: Stimulus code (e.g. ev_assign, ev_resolve).
            fields: JSON of fields required for the transition.
            output_fields: Fields to return.
            comment: Optional comment.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        result = await itop_request({
            "operation": "core/apply_stimulus",
            "class": obj_class,
            "key": parse_key_for_ticket(obj_class, key),
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
