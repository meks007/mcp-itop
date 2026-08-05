"""
CRUD and utility tools: get, create, update, delete,
get_related, list_operations, describe_class.

Note: Apply_stimulus_to_object has been moved to tools/transitions.py.

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
from helpers.stripping import _LEAN_STRIP
from helpers.mentions import resolve_mentions_in_text, is_caselog_attribute
from config import DEFAULT_COMMENT
from cache import get_class_schema, find_lifecycle_state_attribute


# ---------------------------------------------------------------------------
# Type taxonomy helpers
# ---------------------------------------------------------------------------

# iTop type prefixes that identify derived / read-only / relational fields.
# These are not writable via core/create or core/update and are grouped
# separately in the Describe_class output.
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

    # Mark the lifecycle state attribute explicitly so the model knows
    # this field cannot be updated directly via Update_object.
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


# ---------------------------------------------------------------------------
# Mention resolution helpers for field payloads
# ---------------------------------------------------------------------------

async def _resolve_mentions_in_fields(
    fields: dict,
    obj_class: str,
    schema: dict | None,
    client: ItopClient,
) -> dict:
    """Return a copy of *fields* with mention tokens resolved in HTML attributes.

    Iterates over every key in *fields* and:
      - If the key is a Case Log attribute (public_log / private_log /
        private_log_ai) containing an add_item.message string, resolves
        mention tokens in that message string.
      - If the key is a plain Value:HTML field according to *schema*, resolves
        mention tokens in the field value string.
      - All other fields are left unchanged.

    A separate company/resolve_mentions call is made per attribute so that
    iTop can generate a context-correct href for each target attribute.

    Args:
        fields:     Parsed fields dict as supplied to core/create or core/update.
        obj_class:  Concrete iTop class being written, e.g. "UserRequest".
        schema:     Class schema from get_class_schema, or None when unavailable.
                    When None, only Case Log attributes are processed.
        client:     Active ItopClient instance.

    Returns:
        New dict equal to *fields* but with mention tokens replaced by <a>
        elements wherever applicable.
    """
    if not isinstance(fields, dict):
        return fields

    # Build a set of known HTML field names from the schema.
    html_fields: set[str] = set()
    if schema:
        for name, meta in schema.items():
            if _field_kind(meta.get("type", "")) == "html":
                html_fields.add(name)

    result: dict = {}
    for attr, value in fields.items():
        # --- Case Log attribute (add_item payload) ---
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

        # --- Plain Value:HTML field ---
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

        # Preserve the originally requested class before resolve_key overwrites
        # obj_class with the concrete resolved class. The original value is
        # passed to format_and_cache so it can warn the agent when the returned
        # finalclass differs (e.g. agent passed "Ticket", iTop returned "UserRequest").
        requested_class = obj_class
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

        return format_and_cache(result, annotations=annotations or None, requested_class=requested_class)

    @mcp.tool(
        name="Create_object"
    )
    async def itop_create(
        obj_class: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Create an iTop object. Use Describe_class first if the required fields are unknown.

        Mention tokens in Value:HTML fields and Case Log add_item messages are
        resolved automatically before the object is created:
          @<id>     Person by numeric id (resolve Person first, then use @<id>)
          ?<id>     FAQ article by numeric id (resolve FAQ first, then use ?<id>)
          R-<ref>   UserRequest reference -- resolved automatically
          I-<ref>   Incident reference -- resolved automatically
          C-<ref>   Change reference -- resolved automatically
        Unresolvable tokens are stored as plain text without blocking the create.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        # Load schema to identify Value:HTML fields. Failure is non-fatal;
        # only Case Log attributes will be processed when schema is unavailable.
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

    @mcp.tool(
        name="Update_object"
    )
    async def itop_update(
        obj_class: str,
        fields: str,
        ticket_ref: str = "",
        key: Union[str, int] = "",
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Update fields on an existing iTop object.

        For tickets, prefer ticket_ref; bare ticket numbers are resolved automatically.

        The lifecycle state attribute of a class (the field that drives workflow
        transitions, e.g. 'status' on UserRequest) cannot be set via this tool.
        Use Apply_stimulus_to_object for any lifecycle transition such as
        assignment, resolution, reopening, proposing or pending status.

        Any other field named 'status' that is not the lifecycle state attribute
        of the concrete class can be updated normally through this tool.

        Mention tokens in Value:HTML fields and Case Log add_item messages are
        resolved automatically before the update is written:
          @<id>     Person by numeric id (resolve Person first, then use @<id>)
          ?<id>     FAQ article by numeric id (resolve FAQ first, then use ?<id>)
          R-<ref>   UserRequest reference -- resolved automatically
          I-<ref>   Incident reference -- resolved automatically
          C-<ref>   Change reference -- resolved automatically
        Unresolvable tokens are stored as plain text without blocking the update.
        """
        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

        # Resolve the concrete class before doing any schema lookup.
        # A generic parent class or ticket ref may resolve to a concrete child
        # class with its own lifecycle definition.
        obj_class, resolved = await resolve_key(obj_class, coerce_ref(ticket_ref, key))

        # Identify the lifecycle state attribute for this concrete class.
        # get_class_schema is cached for the process lifetime, so this is a
        # fast in-memory lookup after the first call.
        try:
            schema = await get_class_schema(obj_class, client)
            lifecycle_attribute = find_lifecycle_state_attribute(schema)
        except ValueError as exc:
            # More than one field marked is_lifecycle_state=true -- broken schema.
            return "Error: " + str(exc)
        except Exception:
            # describe_class unavailable -- cannot determine lifecycle attribute.
            # Allow the update to proceed; iTop will reject protected fields itself.
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
            return (
                "Error fetching schema for '" + obj_class + "': " + str(exc)
            )
        except Exception as exc:
            return (
                "Unexpected error fetching schema for '" + obj_class + "': " + str(exc)
            )

        if not schema:
            return "Class '" + obj_class + "' returned an empty schema."

        # Split fields into groups for readability.
        # Fields in _LEAN_STRIP (e.g. private_log) are excluded from output
        # to stay consistent with the stripping policy applied to object data.
        enums = {}
        refs = {}
        links = {}
        html_text = {}
        derived = {}
        plain = {}

        for name, meta in schema.items():
            if name in _LEAN_STRIP:
                continue
            kind = _field_kind(meta.get("type", ""))
            if kind == "enum":
                enums[name] = meta
            elif kind == "ref":
                refs[name] = meta
            elif kind == "link":
                links[name] = meta
            elif kind in ("html", "text"):
                html_text[name] = meta
            elif kind == "derived":
                derived[name] = meta
            else:
                plain[name] = meta

        lines = [
            "Class " + obj_class + " -- " + str(len(schema)) + " fields"
            + " (" + str(len(_LEAN_STRIP & schema.keys())) + " private, hidden)",
        ]

        def _section(title: str, fields: dict) -> None:
            if not fields:
                return
            lines.append("\n" + title + " (" + str(len(fields)) + "):")
            for name in sorted(fields):
                lines.append(_format_field_entry(name, fields[name]))

        _section("Enum fields", enums)
        _section("Object reference fields (ExternalKey)", refs)
        _section("Relation / link-set fields", links)
        _section("HTML and text fields", html_text)
        _section("Simple value fields", plain)
        _section("Derived, system and read-only fields", derived)

        return "\n".join(lines)
