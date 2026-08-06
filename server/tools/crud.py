"""
CRUD and utility tools: resolve, get, create, update, delete,
get_related, list_operations, describe_class.

Note: Apply_stimulus_to_object has been moved to tools/transitions.py.

ID-only contract
----------------
All tools except Resolve_object require a confirmed integer database ID
(obj_id: int). Use Resolve_object first when you only have a ref, a bare
number supplied by the user, or any other ambiguous identifier.

Mention resolution
------------------
Create_object and Update_object automatically resolve mention tokens in any
field that is written and that Describe_class reports as Value:HTML, and in
the message of any Case Log add_item payload (public_log, private_log,
private_log_ai).

The resolution is performed via helpers.mentions.resolve_mentions_in_text
which calls company/describe_mentions (cached) and company/resolve_mentions.
Tokens that cannot be resolved are left as plain text; the write operation
is never blocked by a failed mention lookup.
"""

from __future__ import annotations

from client import ItopClient
from helpers import (
    ensure_ref_field,
    fetch_image_counts,
    format_and_cache,
    parse_json_arg,
    resolve_key,
    str_or,
    CLASSES_WITH_REF,
    _SYNTHETIC_FIELDS,
)
from helpers.stripping import _LEAN_STRIP
from helpers.mentions import resolve_mentions_in_text, is_caselog_attribute
from config import DEFAULT_COMMENT
from cache import get_class_schema, find_lifecycle_state_attribute


# ---------------------------------------------------------------------------
# Type taxonomy helpers
# ---------------------------------------------------------------------------

_DERIVED_TYPE_PREFIXES = (
    "AttributeExternalField",
    "AttributeFriendlyName",
    "AttributeFinalClass",
    "AttributeSubItem",
    "AttributeStopWatch",
    "AttributeCaseLog",
    "AttributeCustomFields",
)

_LINK_TYPE_PREFIXES = (
    "Link:",
)


def _field_kind(type_str: str) -> str:
    """Classify an iTop field type string into a broad display category.

    Returns one of: 'enum', 'ref', 'link', 'html', 'text', 'derived', 'value'.
    """
    if type_str.startswith(("eEnum", "eMetaEnum")):
        return "enum"
    if type_str.startswith("Class:"):
        return "ref"
    if type_str.startswith("Link:"):
        return "link"
    if type_str == "Value:HTML":
        return "html"
    if type_str in ("Value:TEXT",):
        return "text"
    for prefix in _DERIVED_TYPE_PREFIXES:
        if type_str.startswith(prefix):
            return "derived"
    return "value"


def _format_field_entry(name: str, meta: dict) -> str:
    """Render a single field entry as an indented multi-line string."""
    type_str = meta.get("type", "?")
    lines = ["  - " + name]
    lines.append("    type: " + type_str)

    kind = _field_kind(type_str)

    if kind == "enum":
        allowed = meta.get("allowed_values")
        if isinstance(allowed, dict) and allowed:
            lines.append("    allowed values:")
            for k, v in allowed.items():
                lines.append("      " + repr(k) + ": " + str(v))
        elif meta.get("values_limited"):
            lines.append("    values: (limited set -- query iTop for options)")

    elif kind == "ref":
        target = type_str[len("Class:"):]
        lines.append("    target class: " + target)
        if meta.get("values_limited"):
            lines.append("    values limited: yes")

    elif kind == "link":
        target = type_str[len("Link:"):]
        lines.append("    target class: " + target)

    elif kind == "html":
        lines.append("    format: html")

    elif kind == "text":
        lines.append("    format: text")

    if meta.get("is_lifecycle_state") is True:
        lines.append("    lifecycle state: yes -- use Apply_stimulus_to_object")

    return "\n".join(lines)


async def _fetch_and_cache_ticket(
    obj_class: str,
    obj_id: "str | int",
    client: ItopClient,
    *,
    full: bool = False,
) -> str:
    """Fetch an object via core/get, apply stripping, and run format_and_cache.

    Used by Load_object and the attachments cache-miss path. The format_and_cache
    call writes inline image refs to the SQLite cache as a side effect.

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


# ---------------------------------------------------------------------------
# Mention resolution helpers for field payloads
# ---------------------------------------------------------------------------

async def _resolve_mentions_in_fields(
    fields: dict,
    obj_class: str,
    schema: dict | None,
    client: ItopClient,
) -> dict:
    """Return a copy of fields with mention tokens resolved in HTML attributes.

    Processes Case Log add_item.message strings and plain Value:HTML fields.
    All other fields are left unchanged. Unresolvable tokens are kept as
    plain text and never block the write operation.
    """
    if not isinstance(fields, dict):
        return fields

    html_fields: set[str] = set()
    if schema:
        for name, meta in schema.items():
            if _field_kind(meta.get("type", "")) == "html":
                html_fields.add(name)

    result: dict = {}
    for attr, value in fields.items():
        if is_caselog_attribute(attr):
            if (
                isinstance(value, dict)
                and isinstance(value.get("add_item"), dict)
                and isinstance(value["add_item"].get("message"), str)
            ):
                msg = value["add_item"]["message"]
                resolved_msg = await resolve_mentions_in_text(
                    msg, obj_class, attr, client
                )
                new_add_item = dict(value["add_item"])
                new_add_item["message"] = resolved_msg
                result[attr] = dict(value)
                result[attr]["add_item"] = new_add_item
            else:
                result[attr] = value
            continue

        if attr in html_fields and isinstance(value, str):
            result[attr] = await resolve_mentions_in_text(
                value, obj_class, attr, client
            )
            continue

        result[attr] = value

    return result


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, client: ItopClient):
    """Register all CRUD tools on the given mcp instance."""

    @mcp.tool(name="Resolve_object")
    async def itop_resolve(
        obj_class: str,
        key_or_ref: str,
    ) -> str:
        """Main entry point. Resolves any identifier to a confirmed integer ID.

        Call this first whenever you have a ticket ref (R-016516), a bare
        number supplied by the user, or any other ambiguous identifier.
        Returns the confirmed class, database ID, ref, and friendlyname in
        one lightweight iTop call.

        All other tools require a strict integer obj_id -- use the ID
        returned here for all subsequent calls.

        Accepts: ticket ref (R-016516), bare number (16505), OQL query, or
        any string key. Use obj_class=Ticket when the concrete class is
        unknown.
        """
        resolved_class, resolved_key = await resolve_key(obj_class, key_or_ref)
        fields = ensure_ref_field(resolved_class, "id, friendlyname")
        result = await client.get(resolved_class, resolved_key, fields=fields)
        return format_and_cache(result, requested_class=obj_class)

    @mcp.tool(name="Load_object")
    async def itop_get(
        obj_class: str,
        obj_id: int,
        output_fields: str = "*",
        limit: int = 25,
        page: int = 0,
        full: bool = False,
    ):
        """Retrieve iTop objects by confirmed integer database ID.

        obj_id must be a confirmed integer ID -- use Resolve_object first if
        you only have a ref or user-supplied number.

        output_fields: use "*" for all standard fields. Use Describe_class
        when field names are unknown. public_log is included by default.
        Set full=True only when the user explicitly requests private_log.
        Do not disclose private_log otherwise.

        Output rendering:
          Single object:   present as a two-column table with columns
                           "Field" and "Value". Do not paraphrase fields
                           in prose.
          Multiple objects: present as a table with one row per object and
                            one column per field.
        """
        if not output_fields or not output_fields.strip():
            visible = sorted(
                await client.get_class_fields(obj_class) - _SYNTHETIC_FIELDS
            )
            if not visible:
                return (
                    "You need to query with obj_id AND output_fields. "
                    "No instances of this class found. Available fields are *."
                )
            return (
                "You need to query with obj_id AND output_fields. "
                "Available fields are * or: " + ", ".join(visible)
            )

        result = await client.get(
            obj_class,
            obj_id,
            fields=ensure_ref_field(obj_class, output_fields),
            limit=limit if limit > 0 else None,
            page=page if page > 0 else None,
            full=full,
        )

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
                if parts:
                    annotations[oid] = (
                        "[images] "
                        + ", ".join(parts)
                        + ". Call List_ticket_images to fetch them."
                        + " These images are an inherent part of the ticket."
                    )

        return format_and_cache(result, annotations=annotations or None)

    @mcp.tool(name="Create_object")
    async def itop_create(
        obj_class: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Create an iTop object. Use Describe_class first if field names are unknown.

        Mention tokens in Value:HTML fields and Case Log add_item messages are
        resolved automatically before the object is created:
          @<id>   Person by numeric id
          ?<id>   FAQ article by numeric id
          R-<ref>, I-<ref>, C-<ref>  ticket references
        Unresolvable tokens are stored as plain text without blocking the create.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        try:
            schema = await get_class_schema(obj_class, client)
        except Exception:
            schema = None

        parsed = await _resolve_mentions_in_fields(parsed, obj_class, schema, client)

        result = await client.create(
            obj_class,
            parsed,
            output_fields=ensure_ref_field(obj_class, output_fields),
            comment=comment or DEFAULT_COMMENT,
        )
        return format_and_cache(result)

    @mcp.tool(name="Update_object")
    async def itop_update(
        obj_class: str,
        obj_id: int,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Update fields on an existing iTop object.

        obj_id must be the confirmed integer database ID. Use Resolve_object
        first if you only have a ref. Do not include the lifecycle state
        attribute in fields; use Apply_stimulus_to_object instead.

        Mention tokens in Value:HTML fields and Case Log add_item messages are
        resolved automatically (@<id>, ?<id>, R-<ref>, I-<ref>, C-<ref>).
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        try:
            schema = await get_class_schema(obj_class, client)
            lifecycle_attribute = find_lifecycle_state_attribute(schema)
        except ValueError as exc:
            return "Error: " + str(exc)
        except Exception:
            lifecycle_attribute = None
            schema = None

        if lifecycle_attribute and isinstance(parsed, dict) and lifecycle_attribute in parsed:
            return (
                "Error: '" + lifecycle_attribute
                + "' is the lifecycle state attribute for class '" + obj_class
                + "' and cannot be set via Update_object. "
                "Use Apply_stimulus_to_object with the appropriate target_state instead."
            )

        parsed = await _resolve_mentions_in_fields(parsed, obj_class, schema, client)

        result = await client.update(
            obj_class,
            obj_id,
            parsed,
            output_fields=ensure_ref_field(obj_class, output_fields),
            comment=comment or DEFAULT_COMMENT,
        )
        return format_and_cache(result)

    @mcp.tool(name="Delete_object")
    async def itop_delete(
        obj_class: str,
        obj_id: int,
        comment: str = "",
        simulate: bool = True,
    ) -> str:
        """Deletion is disabled by policy. Do not use this tool to remove iTop objects.

        It runs in simulation mode by default and is retained only for controlled
        dry-run checks."""
        result = await client.delete(
            obj_class,
            obj_id,
            comment=comment or DEFAULT_COMMENT,
            simulate=simulate,
        )
        return format_and_cache(result)

    @mcp.tool(name="Get_object_relations")
    async def itop_get_related(
        obj_class: str,
        key: str,
        relation: str = "impacts",
        depth: int = 4,
        direction: str = "down",
        redundancy: bool = True,
    ) -> str:
        """Find CIs related to a given object via impact or dependency relations."""
        from helpers.utils import parse_key
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
        if not relations:
            return output
        counts = []
        for rel_type, rel_data in relations.items():
            if isinstance(rel_data, dict):
                counts.append(rel_type + ": " + str(len(rel_data)) + " object(s)")
        if counts:
            output = output + "\nRelation summary: " + ", ".join(counts)
        return output

    @mcp.tool(name="List_operations")
    async def itop_list_operations() -> str:
        """List all available REST/JSON operations on the iTop server."""
        result = await client.list_operations()
        if result.get("code", -1) != 0:
            return "Error: " + str_or(result, "message", "Unknown error")
        ops = result.get("operations", [])
        if not ops:
            return "No operations returned."
        lines = ["Available iTop REST/JSON operations:", ""]
        for op in ops:
            name = op.get("verb") or op.get("name") or str(op)
            desc = op.get("description", "")
            lines.append("  " + name + (": " + desc if desc else ""))
        return "\n".join(lines)

    @mcp.tool(name="Describe_class")
    async def itop_describe_class(obj_class: str) -> str:
        """Describe the field schema of an iTop class.

        Uses company/describe_class to return authoritative field metadata
        including field type, format (text/html), allowed enum values, and
        whether a field is the lifecycle state attribute of the class.
        Works for classes with zero instances. Results are cached for the
        lifetime of the server process.
        """
        try:
            schema = await get_class_schema(obj_class, client)
        except ValueError as exc:
            return "Error: " + str(exc)

        if not schema:
            return "No fields returned for class '" + obj_class + "'."

        writable: list[str] = []
        derived: list[str] = []
        linked: list[str] = []

        for name, meta in sorted(schema.items()):
            kind = _field_kind(meta.get("type", ""))
            entry = _format_field_entry(name, meta)
            if kind == "derived":
                derived.append(entry)
            elif kind == "link":
                linked.append(entry)
            else:
                writable.append(entry)

        sections: list[str] = ["Class: " + obj_class, ""]
        if writable:
            sections.append("Writable / queryable fields:")
            sections.extend(writable)
        if linked:
            sections.append("")
            sections.append("Linked sets (read-only, use separate OQL to query):")
            sections.extend(linked)
        if derived:
            sections.append("")
            sections.append("Derived / read-only fields:")
            sections.extend(derived)

        return "\n".join(sections)
