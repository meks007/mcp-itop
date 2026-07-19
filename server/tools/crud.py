"""
CRUD and utility tools: get, create, update, delete, apply_stimulus,
get_related, list_operations, describe_class.
"""

from __future__ import annotations

from typing import Union

from client import ItopClient
from helpers import (
    coerce_ref,
    ensure_ref_field,
    fetch_image_counts,
    format_and_cache,
    parse_json_arg,
    parse_key,
    resolve_key,
    str_or,
    CLASSES_WITH_REF,
    _SYNTHETIC_FIELDS,
)
from config import DEFAULT_COMMENT


async def _fetch_and_cache_ticket(
    obj_class: str,
    obj_id: str | int,
    client: ItopClient,
    *,
    full: bool = False,
) -> str:
    """Fetch an object via core/get, apply stripping, and run format_and_cache.

    Used by Load_object and the attachments cache-miss path. The format_and_cache
    call writes inline image refs to the SQLite cache as a side effect.

    Stripping follows the same rules as client.get: _LEAN_STRIP is applied
    unless full=True. Content stripped for privacy must not reach the image
    cache either, so full=False is the correct default.

    Args:
        obj_class: iTop class name (concrete class preferred).
        obj_id:    Numeric object ID (int or string).
        client:    ItopClient instance.
        full:      When True, skip field stripping.
    """
    result = await client.get(
        obj_class,
        int(obj_id) if str(obj_id).isdigit() else obj_id,
        fields="*",
        full=full,
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
    ):
        """Retrieve iTop objects by class and key.

        key_or_ref identifies the object.
        You always have to submit one of:
          "R-016292"  ticket ref (preferred)
          "16292"     bare number, resolved automatically
          "15525"     numeric DB id
          SELECT ...  OQL string
        You CANNOT leave key_or_ref empty.
        You CANNOT leave output_fields empty. If in doubt, use Describe_class or *
        Batch same-class lookups with OQL instead of one call per object.
        Use obj_class="Ticket" when the concrete class is unknown.
        Set full=True only when private_log is explicitly asked for by the user.
        output_fields is always honoured as-is; use output_fields=* together with
        full=True to fetch all fields including private ones.
        public_log is always included without full=True.
        Do not disclose private_log unless explicitly requested by the user.
        Redact or prohibit mentioning anything that could be a password or otherwise sensitive.
        """

        if not output_fields or not output_fields.strip():
            visible = sorted(
                await client.get_class_fields(obj_class) - _SYNTHETIC_FIELDS
            )
            if not visible:
                return (
                    "You need to query with key AND output_fields. "
                    "No instances of this class found. Available fields are *."
                )
            return (
                "You need to query with key AND output_fields. "
                "Available fields are * or: " + ", ".join(visible)
            )

        obj_class, resolved_key = await resolve_key(obj_class, key_or_ref)

        result = await client.get(
            obj_class,
            resolved_key,
            fields=ensure_ref_field(obj_class, output_fields),
            limit=limit if limit > 0 else None,
            page=page if page > 0 else None,
            full=full,
        )

        # Build per-object image annotations before formatting so they can be
        # interleaved with each object's field block by format_and_cache.
        # fetch_image_counts reads the inline ref cache populated by
        # parse_objects inside format_and_cache -- but we need the cache warm
        # before the count call. parse_objects is pure and cheap so we run it
        # once here to seed the cache, then format_and_cache runs it again
        # (idempotent). Callers outside CLASSES_WITH_REF skip this entirely.
        annotations: dict[str, str] = {}
        if obj_class in CLASSES_WITH_REF:
            from attachment_store import write_inline_image_refs
            from helpers.html import parse_objects as _parse_objects
            for ticket_key, img_refs in _parse_objects(result).items():
                try:
                    _cls, _oid = ticket_key.split("::", 1)
                    write_inline_image_refs(_cls, _oid, img_refs)
                except Exception:
                    pass

            for obj_data in (result.get("objects") or {}).values():
                oid = str(obj_data.get("key") or "")
                if not oid or not isinstance(obj_data.get("fields"), dict):
                    continue
                att_count, ii_count = await fetch_image_counts(obj_class, oid)
                parts = []
                if att_count:
                    parts.append(str(att_count) + " attachment(s)")
                if ii_count:
                    parts.append(str(ii_count) + " inline image(s)")
                # ii_count == 0: confirmed no inline images, nothing to show.
                # ii_count is None: cannot happen -- cache was seeded above.
                if parts:
                    annotations[oid] = (
                        "[images] "
                        + ", ".join(parts)
                        + ". Call List_ticket_images to fetch them."
                        + " These images are an inherent part of the ticket."
                    )

        return format_and_cache(result, annotations=annotations or None)

    @mcp.tool(
        name="Create_object"
    )
    async def itop_create(
        obj_class: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Create an iTop object. Use Describe_class first if the required fields are unknown."""
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        result = await client.create(
            obj_class,
            parsed,
            output_fields=ensure_ref_field(obj_class, output_fields),
            comment=comment or DEFAULT_COMMENT,
        )
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
        Do not update status with this tool -- use Apply_stimulus_to_object for lifecycle
        transitions such as assignment, resolution, reopening, proposing or pending status."""
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        if isinstance(parsed, dict) and "status" in parsed:
            return (
                "Error: 'status' cannot be set via Update_object. "
                "Use Apply_stimulus_to_object with the appropriate stimulus instead:\n"
                "  ev_assign   - assign ticket\n"
                "  ev_resolve  - resolve ticket (include solution in fields)\n"
                "  ev_reopen   - reopen ticket\n"
                "  ev_propose  - propose solution\n"
                "  ev_pending  - put ticket on hold"
            )

        obj_class, resolved = await resolve_key(obj_class, coerce_ref(ticket_ref, key))

        result = await client.update(
            obj_class,
            resolved,
            parsed,
            output_fields=ensure_ref_field(obj_class, output_fields),
            comment=comment or DEFAULT_COMMENT,
        )
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
        obj_class, resolved = await resolve_key(obj_class, coerce_ref(ticket_ref, key))

        result = await client.delete(
            obj_class,
            resolved,
            comment=comment or DEFAULT_COMMENT,
            simulate=simulate,
        )
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
        or pending status. Use this tool -- not Update_object -- for status changes.
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

        obj_class, resolved = await resolve_key(obj_class, coerce_ref(ticket_ref, key))

        result = await client.apply_stimulus(
            obj_class,
            resolved,
            stimulus,
            fields=parsed,
            output_fields=ensure_ref_field(obj_class, output_fields),
            comment=comment or DEFAULT_COMMENT,
        )
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
        result = await client.get_related(
            obj_class,
            parse_key(key),
            relation=relation,
            depth=depth,
            direction=direction,
            redundancy=redundancy,
        )
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
        result = await client.operations()
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
        fields = await client.get_class_fields(obj_class)

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
