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

    @mcp.tool(
        name="Load_object"
    )
    async def itop_get(
        obj_class: str,
        key: str,
        output_fields: str = "*",
        limit: int = 25,
        page: int = 0,
        full: bool = False,
    ) -> str:
        """Retrieve iTop objects by ID, reference, OQL, or JSON criteria.

        Use obj_class="Ticket" when the ticket class is unknown, then switch to the
        real class once it is known. Bare ticket numbers are resolved automatically.
        Set full=True only when complete fields or ticket logs are needed. Do not
        disclose private_log unless the user explicitly requests it. Batch same-class
        lookups with OQL rather than calling once per object."""
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

    @mcp.tool(
        name="Create_object"
    )
    async def itop_create(
        obj_class: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Create an iTop object. Use itop_describe_class first if the required fields are unknown."""
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

    @mcp.tool(
        name="Update_object"
    )
    async def itop_update(
        obj_class: str,
        fields: str,
        ticket_ref: str = "",
        key: Union[str, int] = "",
        output_fields: str = "ref, friendlyname, status",
        comment: str = "",
    ) -> str:
        """Update fields on an existing iTop object.

        For tickets, prefer ticket_ref; bare ticket numbers are resolved automatically.
        Do not update status with this tool -- use itop_apply_stimulus for lifecycle
        transitions such as assignment, resolution, reopening, or pending status."""
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

        obj_class, resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )

        result = await itop_request({
            "operation": "core/update",
            "class": obj_class,
            "key": resolved,
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool(
        name="Delete_object"
    )
    async def itop_delete(
        obj_class: str,
        ticket_ref: str = "",
        key: Union[str, int] = "",
        comment: str = "",
        simulate: bool = True,
    ) -> str:
        """Deletion is disabled by policy. Do not use this tool to remove iTop objects.

        It runs in simulation mode by default and is retained only for controlled
        dry-run checks."""
        obj_class, resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )

        result = await itop_request({
            "operation": "core/delete",
            "class": obj_class,
            "key": resolved,
            "simulate": simulate,
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_objects(result)

    @mcp.tool(
        name="Apply_stimulus_to_object"
    )
    async def itop_apply_stimulus(
        obj_class: str,
        stimulus: str,
        ticket_ref: str = "",
        key: Union[str, int] = "",
        fields: str = "{}",
        output_fields: str = "ref, friendlyname, status",
        comment: str = "",
    ) -> str:
        """Apply a ticket lifecycle transition such as assignment, resolution, reopening,
        or pending status. Use this tool -- not itop_update -- for status changes.
        Resolve tickets with ev_resolve and include a solution in fields; never use
        ev_close. Prefer ticket_ref; bare ticket numbers are resolved automatically."""
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        if stimulus == "ev_close":
            return (
                "Error: ev_close is not permitted in this workflow. "
                "To close a ticket, use ev_resolve with a solution in fields, "
                'e.g. fields={"solution": "..."}. Resolving is the final step.'
            )

        obj_class, resolved = await resolve_key(
            obj_class, ticket_ref or None, key or None, itop_request
        )

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

    @mcp.tool(
        name="Get_object_relations"
    )
    async def itop_get_related(
        obj_class: str,
        key: str,
        relation: str = "impacts",
        depth: int = 4,
        direction: str = "down",
        redundancy: bool = True,
    ) -> str:
        """Find CIs related to a given object via impact or dependency relations."""
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

    @mcp.tool(
        name="List_object_operations"
    )
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

    @mcp.tool(
        name="Describe_class"
    )
    async def itop_describe_class(obj_class: str) -> str:
        """Discover available fields for an iTop class by sampling an existing object."""
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
