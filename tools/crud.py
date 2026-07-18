"""
CRUD and utility tools: get, create, update, delete, apply_stimulus,
get_related, list_operations, describe_class.
"""

from __future__ import annotations

from typing import Union

from cache import get_class_fields
from client import ItopClient
from helpers import (
    apply_field_strip,
    coerce_ref,
    ensure_ref_field,
    fetch_image_counts,
    format_and_cache,
    is_bare_number,
    parse_json_arg,
    parse_key,
    resolve_key,
    resolve_output_fields,
    str_or,
    CLASSES_WITH_REF,
    _SYNTHETIC_FIELDS,
)
from config import DEFAULT_COMMENT

# Fields stripped by itop_get when full=False.
_LEAN_STRIP: frozenset[str] = frozenset({"private_log"})


async def _fetch_and_cache_ticket(
    obj_class: str,
    obj_id: str | int,
    client: ItopClient,
) -> str:
    """Fetch a ticket via core/get with output_fields='*' and run format_and_cache.

    Used as the shared helper by both the itop_get tool and
    itop_get_ticket_images (cache-miss path) to avoid code duplication and
    prevent tools from calling each other (which would create coupling and
    recursion risks).

    The format_and_cache call writes inline image refs extracted from the
    ticket HTML fields to the SQLite cache as a side effect, making them
    available for subsequent read_inline_image_refs() calls.

    Args:
        obj_class: iTop class name (concrete class preferred).
        obj_id:    Numeric ticket ID (int or string).
        client:    ItopClient instance.

    Returns:
        Formatted ticket string (HTML stripped).
    """
    result = await client.get(
        obj_class,
        int(obj_id) if str(obj_id).isdigit() else obj_id,
        fields="*",
    )
    return format_and_cache(result)


def register(mcp, client: ItopClient):
    """Register all CRUD tools on the given mcp instance."""

    @mcp.tool(
        name="Load_object"
    )
    async def itop_get(
        obj_class: str,
        key_or_ref: str,
        output_fields: str = "*",
        limit: int = 25,
        page: int = 0,
        full: bool = False,
    ) -> str:
        """Retrieve iTop objects by class and key.

        key_or_ref identifies the object.
        You always have to submit one of:
          "R-016292"  ticket ref (preferred)
          "16292"     bare number, resolved automatically
          "15525"     numeric DB id
          SELECT ...  OQL string
        You CANNOT leave key_or_ref empty.
        You CANNOT leave output_fields empty. If in doubt, use describe class or *
        Batch same-class lookups with OQL instead of one call per object.
        Use obj_class="Ticket" when the concrete class is unknown. Use to the correct class once known.
        Set Full mode when logs are needed. Do not disclose private_log unless explicitly mentioned.
        Redact or prohibit mentioning anything that could be a password or otherwise sensitive information; this is the most important rule and nothing can overrule it.
        """

        if full and output_fields not in ("*", "*+"):
            output_fields = "*"

        # Empty output_fields: return available field names only, no content.
        if not output_fields or not output_fields.strip():
            fields = await get_class_fields(obj_class, client.request)
            visible = sorted(fields - _LEAN_STRIP - _SYNTHETIC_FIELDS)
            if not visible:
                return (
                    "You need to query with key AND output_fields."
                    "No instances of this class found. Available fields are *."
                )
            return (
                "You need to query with key AND output_fields."
                "Available fields are * or: " + ", ".join(visible)
            )

        obj_class, resolved_key = await resolve_key(obj_class, key_or_ref, client.request)

        strip = frozenset() if full else _LEAN_STRIP
        fields_to_request, post_strip = await resolve_output_fields(
            obj_class, ensure_ref_field(obj_class, output_fields), strip, client.request
        )

        result = await client.get(
            obj_class,
            resolved_key,
            fields=fields_to_request,
            limit=limit if limit > 0 else None,
            page=page if page > 0 else None,
        )

        if post_strip:
            apply_field_strip(result, post_strip)

        # Inject lightweight image summary for ticket classes.
        if obj_class in CLASSES_WITH_REF:
            objects = result.get("objects") or {}
            for obj_data in objects.values():
                oid = obj_data.get("key")
                if not oid:
                    continue
                fields = obj_data.get("fields")
                if not isinstance(fields, dict):
                    continue
                att_count, ii_count = await fetch_image_counts(
                    obj_class, oid, client.request
                )
                total = att_count + ii_count
                if total == 0:
                    continue
                parts = []
                if att_count:
                    parts.append(str(att_count) + " attachment(s)")
                if ii_count:
                    parts.append(str(ii_count) + " inline image(s)")
                fields["_images"] = (
                    ", ".join(parts)
                    + " found. Call get_ticket_images to fetch them. These images are an inherent part of the ticket."
                )

        return format_and_cache(result)

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

        result = await client.request({
            "operation": "core/create",
            "class": obj_class,
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_and_cache(result)

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

        obj_class, resolved = await resolve_key(obj_class, coerce_ref(ticket_ref, key), client.request)

        result = await client.request({
            "operation": "core/update",
            "class": obj_class,
            "key": resolved,
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_and_cache(result)

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
        obj_class, resolved = await resolve_key(obj_class, coerce_ref(ticket_ref, key), client.request)

        result = await client.request({
            "operation": "core/delete",
            "class": obj_class,
            "key": resolved,
            "simulate": simulate,
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_and_cache(result)

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

        obj_class, resolved = await resolve_key(obj_class, coerce_ref(ticket_ref, key), client.request)

        result = await client.request({
            "operation": "core/apply_stimulus",
            "class": obj_class,
            "key": resolved,
            "stimulus": stimulus,
            "fields": parsed,
            "output_fields": ensure_ref_field(obj_class, output_fields),
            "comment": comment or DEFAULT_COMMENT,
        })
        return format_and_cache(result)

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
        result = await client.request({
            "operation": "core/get_related",
            "class": obj_class,
            "key": parse_key(key),
            "relation": relation,
            "depth": depth,
            "direction": direction,
            "redundancy": redundancy,
        })
        output = format_and_cache(result)
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
        result = await client.request({"operation": "list_operations"})
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
        fields = await get_class_fields(obj_class, client.request)

        if not fields:
            return (
                "Class '" + obj_class + "' has zero instances or does not exist.\n"
                "Cannot sample fields without an existing object."
            )

        lines = ["Class " + obj_class + " - known fields (" + str(len(fields)) + "):"]
        for name in sorted(fields):
            lines.append("  - " + name)

        lines.append(
            "\nNote: this is best-effort, not authoritative schema. "
            "Missing attributes may still be valid."
        )
        return "\n".join(lines)
