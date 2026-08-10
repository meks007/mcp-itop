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

import logging
import re

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
from cache import get_class_schema, find_lifecycle_state_attribute, get_class_hierarchy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OQL guard
# ---------------------------------------------------------------------------

# Matches strings that start with SELECT (ignoring leading whitespace and case).
_OQL_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
# Matches a WHERE keyword anywhere in the string (word boundary, case-insensitive).
_OQL_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)


def _check_oql_where(key_or_ref: str) -> "str | None":
    """Return an error message when key_or_ref is an OQL SELECT without WHERE.

    Returns None when the input is not OQL or when it contains a WHERE clause.
    """
    if not _OQL_SELECT_RE.match(key_or_ref):
        return None
    if _OQL_WHERE_RE.search(key_or_ref):
        return None
    return (
        "Error: OQL queries passed to Resolve_object must include a WHERE clause. "
        "A bare 'SELECT <class>' without a filter is not allowed -- "
        "add at least one condition, e.g. "
        "\"SELECT UserRequest WHERE id = 42\" or "
        "\"SELECT UserRequest WHERE ref = 'R-000123'\"."
    )


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


async def _fetch_and_cache_object(
    obj_class: str,
    obj_id: "str | int",
    client: ItopClient,
    *,
    full: bool = False,
) -> str:
    """Fetch an iTop object via core/get, apply stripping, and run format_and_cache.

    Used by Load_object and the inline image ref cache-miss path in
    tools/attachments.py. The format_and_cache call writes inline image refs
    to the SQLite cache as a side effect, making them available for
    List_object_attachments without a second round-trip.

    Works for any iTop class, not only tickets.

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
    schema: "dict | None",
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
        """Resolve a ticket reference, user-supplied number, OQL query, or other
        identifier to a confirmed iTop class and numeric database ID.
        Also returns the lifecycle state attribute name and current value when
        the class has a state machine. Use obj_class=Ticket when the concrete
        ticket class is unknown.
        OQL queries must include a WHERE clause; a bare 'SELECT <class>'
        without a filter is not allowed.
        """
        # Guard: reject OQL without a WHERE clause.
        oql_err = _check_oql_where(key_or_ref)
        if oql_err:
            return oql_err

        resolved_class, resolved_key = await resolve_key(obj_class, key_or_ref)

        # -- Step 1: fetch the class schema (cached after first call per class).
        # Discover the lifecycle state attribute name, if any.
        lifecycle_attr: "str | None" = None
        try:
            schema = await get_class_schema(resolved_class, client)
            lifecycle_attr = find_lifecycle_state_attribute(schema)
        except ValueError as exc:
            # Multiple lifecycle fields -- broken class definition, log and skip.
            logger.warning(
                "[Resolve_object] find_lifecycle_state_attribute failed for "
                "cls=%r: %s", resolved_class, exc
            )
        except Exception as exc:
            # Schema fetch failed (e.g. describe_class not available).
            logger.debug(
                "[Resolve_object] schema unavailable for cls=%r: %s",
                resolved_class, exc
            )

        # -- Step 2: build output_fields and run core/get.
        # Always include id and friendlyname; append the lifecycle field when known.
        base_fields = "id, friendlyname"
        if lifecycle_attr:
            output_fields = ensure_ref_field(
                resolved_class, base_fields + ", " + lifecycle_attr
            )
        else:
            output_fields = ensure_ref_field(resolved_class, base_fields)

        result = await client.get(resolved_class, resolved_key, fields=output_fields)

        # -- Step 3: annotate the formatted output with lifecycle metadata.
        # Extract the lifecycle state value from the first returned object so
        # callers get both the attribute name and the current code in one call.
        lifecycle_annotation: "str | None" = None
        if lifecycle_attr:
            objects = result.get("objects") or {}
            for obj_data in objects.values():
                obj_fields = obj_data.get("fields") if isinstance(obj_data, dict) else None
                if isinstance(obj_fields, dict) and lifecycle_attr in obj_fields:
                    lifecycle_value = obj_fields[lifecycle_attr]
                    lifecycle_annotation = (
                        "lifecycle_state_attribute: " + lifecycle_attr + "\n"
                        "lifecycle_state: " + str(lifecycle_value)
                    )
                break

        formatted = format_and_cache(result, requested_class=obj_class)

        if lifecycle_annotation:
            formatted = formatted + "\n" + lifecycle_annotation

        return formatted

    @mcp.tool(name="Load_object")
    async def itop_get(
        obj_class: str,
        obj_id: int,
        output_fields: str = "*",
        limit: int = 25,
        page: int = 0,
        full: bool = False,
    ):
        """Retrieve an iTop object by confirmed numeric ID. Resolve references or ambiguous identifiers first. Use output_fields to select fields; public_log is included by default. Set full=True to disable field stripping.
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
                        + ". Call List_object_attachments to fetch them."
                        + " These images are an inherent part of the object."
                    )

        return format_and_cache(result, annotations=annotations or None)

    @mcp.tool(name="Create_object")
    async def itop_create(
        obj_class: str,
        fields: str,
        output_fields: str = "id, friendlyname",
        comment: str = "",
    ) -> str:
        """Create a concrete iTop object from JSON fields.

        IMPORTANT: call Describe_class first if you have not already done so
        for this class. The class may be abstract and cannot be instantiated;
        Describe_class will tell you which concrete descendant to use instead.
        Creation is blocked here when the class is known to be abstract.

        Mention tokens in HTML fields and Case Log messages are resolved
        automatically: @<id>, ?<id>, and R-/I-/C- references.
        Unresolved tokens remain plain text.
        """
        # Fetch schema (cached). Also populates abstract_flag / descendant_classes.
        try:
            schema = await get_class_schema(obj_class, client)
        except Exception:
            schema = None

        # Guard: refuse to create an instance of an abstract class.
        abstract_flag, concrete_descendants = get_class_hierarchy(obj_class)
        if abstract_flag is True:
            msg = (
                "Error: '" + obj_class + "' is ABSTRACT and cannot be instantiated. "
                "Use Describe_class to see the available concrete descendants"
            )
            if concrete_descendants:
                msg += ": " + ", ".join(concrete_descendants) + "."
            else:
                msg += "."
            return msg

        parsed = parse_json_arg(fields, "fields")
        if isinstance(parsed, str):
            return parsed

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
        """Update an iTop object by confirmed numeric ID. Resolve references first. Do not set its lifecycle-state field directly; use Apply_stimulus_to_object. Mention tokens in HTML fields and Case Log messages are resolved automatically: @<id>, ?<id>, and R-/I-/C- references.
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
        """Simulate deletion of an iTop object by confirmed numeric ID. Deletion is disabled by policy; this tool performs dry-run checks only.
        """
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
        obj_id: int,
        relation: str = "impacts",
        depth: int = 4,
        direction: str = "down",
        redundancy: bool = True,
    ) -> str:
        """Find configuration items related to an iTop object through impact or dependency relations. Requires a confirmed numeric object ID.
        """
        result = await client.get_related(
            obj_class,
            obj_id,
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

    @mcp.tool(name="List_object_operations")
    async def itop_list_operations() -> str:
        """List the REST/JSON operations available on the iTop server."""
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

    @mcp.tool(name="Describe_class")
    async def itop_describe_class(obj_class: str) -> str:
        """Return an iTop class schema: abstract/concrete status, concrete
        descendants (when abstract), field types, enum values, references,
        and lifecycle-state metadata. Always call this before Create_object
        to verify the class is concrete. Results are cached for the process.
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

        # -- Hierarchy header --
        abstract_flag, concrete_descendants = get_class_hierarchy(obj_class)
        lines = []
        if abstract_flag is True:
            lines.append("This class is ABSTRACT and cannot be instantiated.")
            if concrete_descendants:
                lines.append("Concrete descendants you can create instead:")
                for cls_name in concrete_descendants:
                    lines.append("  " + cls_name)
            else:
                lines.append("No concrete descendants found.")
        elif abstract_flag is False:
            lines.append("This class is concrete and can be instantiated directly.")
            if concrete_descendants:
                lines.append("Concrete descendants also available:")
                for cls_name in concrete_descendants:
                    lines.append("  " + cls_name)
        # abstract_flag is None: hierarchy info not available -- omit the block.

        lines.append(
            "\nClass " + obj_class + " -- " + str(len(schema)) + " fields"
            + " (" + str(len(_LEAN_STRIP & schema.keys())) + " private, hidden)",
        )

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
