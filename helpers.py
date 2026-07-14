"""
Pure utility / formatting helpers shared across all tool modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

from config import ITOP_URL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SLA helpers
# ---------------------------------------------------------------------------

SLA_ANALYSIS_FIELDS = (
    "id,ref,title,status,service_name,org_name,agent_name,caller_name,"
    "start_date,assignment_date,resolution_date,close_date,"
    "sla_tto_passed,sla_ttr_passed,time_spent,"
    "last_update"
)

_SLA_PASSED_VALUES = {"true", "yes", "1"}
_SLA_BREACHED_VALUES = {"false", "no", "0"}


def sla_is_passed(val: str) -> bool:
    return val.strip().lower() in _SLA_PASSED_VALUES if val else False


def sla_is_breached(val: str) -> bool:
    return val.strip().lower() in _SLA_BREACHED_VALUES if val else False


# ---------------------------------------------------------------------------
# Universal class registry
# ---------------------------------------------------------------------------
# Single process-level cache for all iTop class metadata.
# Per-entry shape:
#   {
#     "exists": bool | None,     # None = not yet probed
#     "fields": frozenset[str],  # known field names, grown from live responses
#     "meta":   dict,            # arbitrary per-class metadata (e.g. text_field for KB)
#   }
_ITOP_CLASS_REGISTRY: dict[str, dict] = {}


def _registry_entry(cls: str) -> dict:
    """Return (and lazily create) the registry slot for a class."""
    if cls not in _ITOP_CLASS_REGISTRY:
        logger.debug("[registry] init slot for class=%r", cls)
        _ITOP_CLASS_REGISTRY[cls] = {"exists": None, "fields": frozenset(), "meta": {}}
    return _ITOP_CLASS_REGISTRY[cls]


def registry_get_meta(cls: str, key: str, default: Any = None) -> Any:
    """Read arbitrary per-class metadata from the registry."""
    value = _registry_entry(cls)["meta"].get(key, default)
    logger.debug("[registry] get_meta cls=%r key=%r -> %r", cls, key, value)
    return value


def registry_set_meta(cls: str, key: str, value: Any) -> None:
    """Write arbitrary per-class metadata into the registry."""
    logger.debug("[registry] set_meta cls=%r key=%r value=%r", cls, key, value)
    _registry_entry(cls)["meta"][key] = value


def registry_get_fields(cls: str) -> frozenset:
    """Return the known field inventory for a class (may be empty if not yet seen)."""
    fields = _registry_entry(cls)["fields"]
    logger.debug("[registry] get_fields cls=%r -> %d fields known", cls, len(fields))
    return fields


def _seed_field_cache(cls: str, fields: dict) -> None:
    """Grow the field inventory for a class from a live response fields dict.

    Already-known fields are always kept (union, never removed).
    Only genuinely new fields (not yet in the registry) are added and logged.
    """
    entry = _registry_entry(cls)
    if fields:
        incoming = frozenset(fields.keys())
        before_set = entry["fields"]                  # snapshot before update
        new = incoming - before_set                   # truly new fields only
        entry["fields"] = before_set | incoming       # union: existing + incoming
        entry["exists"] = True
        logger.debug(
            "[registry] seed_field_cache cls=%r fields_before=%d fields_after=%d new=%r",
            cls,
            len(before_set),
            len(entry["fields"]),
            sorted(new),
        )


async def ensure_class_exists(candidates: list[str], itop_request) -> str:
    """Return the first class in candidates that exists on the iTop server.

    Each candidate is probed with a minimal core/get (limit 1, output_fields id)
    unless its existence is already cached in _ITOP_CLASS_REGISTRY. The first
    confirmed class name is returned; an empty string is returned if none exist.

    All probe results (positive and negative) are cached, so subsequent calls
    within the same server process pay no network cost.
    """
    for cls in candidates:
        entry = _registry_entry(cls)
        if entry["exists"] is True:
            logger.debug("[registry] ensure_class_exists cls=%r -> cached True", cls)
            return cls
        if entry["exists"] is False:
            logger.debug("[registry] ensure_class_exists cls=%r -> cached False, skip", cls)
            continue
        r = await itop_request({
            "operation": "core/get",
            "class": cls,
            "key": f"SELECT {cls}",
            "output_fields": "id",
            "limit": "1",
        })
        if r.get("code") == 0:
            entry["exists"] = True
            for obj_data in (r.get("objects") or {}).values():
                _seed_field_cache(cls, obj_data.get("fields") or {})
            logger.debug("[registry] ensure_class_exists cls=%r -> exists=True (probed)", cls)
            return cls
        else:
            entry["exists"] = False
            logger.debug(
                "[registry] ensure_class_exists cls=%r -> exists=False code=%r msg=%r",
                cls,
                r.get("code"),
                r.get("message"),
            )
    logger.debug("[registry] ensure_class_exists candidates=%r -> none found", candidates)
    return ""


async def resolve_output_fields(
    obj_class: str,
    output_fields: str,
    strip: frozenset[str],
    itop_request,  # noqa: ARG001 - reserved for future warm-miss describe fallback
) -> tuple[str, frozenset[str]]:
    """Resolve (output_fields, strip) into (fields_to_request, post_strip_set).

    Three cases:

    1. Explicit field list (not * or *+):
       Send as-is. Return the full strip set for post-response application.
       Fields in the strip set that the LLM explicitly requested are silently
       removed from the result without an error.

    2. Wildcard (* or *+), field cache WARM for obj_class:
       Build an explicit field list from the cached set minus strip and
       _SYNTHETIC_FIELDS. Send that explicit list. Return an empty post-strip
       set -- nothing left to strip.

    3. Wildcard (* or *+), field cache COLD for obj_class:
       Send the wildcard as-is. Return the strip set for post-response
       application. The response will seed the cache via apply_field_strip /
       format_objects, so the next call hits case 2.

    No extra describe request is fired -- the cache grows naturally from every
    successful core/get response processed by apply_field_strip or format_objects.
    """
    logger.debug(
        "[resolve_output_fields] cls=%r output_fields=%r strip=%r",
        obj_class,
        output_fields,
        sorted(strip),
    )
    is_wildcard = output_fields in ("*", "*+")

    if not is_wildcard or not strip:
        logger.debug(
            "[resolve_output_fields] passthrough (explicit or no strip) -> %r strip=%r",
            output_fields,
            sorted(strip),
        )
        return output_fields, strip

    cached_fields = _registry_entry(obj_class)["fields"]

    if cached_fields:
        # Warm path: build explicit list pre-filtered
        explicit = sorted(cached_fields - strip - _SYNTHETIC_FIELDS)
        if obj_class in CLASSES_WITH_REF:
            if "ref" in explicit:
                explicit = ["ref"] + [f for f in explicit if f not in ("ref", "id")]
        if not explicit:
            # Safety valve: strip removed everything -- fall back to wildcard + post-strip
            logger.debug(
                "[resolve_output_fields] cls=%r warm cache but strip removed all fields, fallback to wildcard",
                obj_class,
            )
            return output_fields, strip
        result_fields = ", ".join(explicit)
        logger.debug(
            "[resolve_output_fields] cls=%r WARM cache hit, explicit fields=%r post_strip=empty",
            obj_class,
            result_fields,
        )
        return result_fields, frozenset()

    # Cold path: wildcard + post-strip
    logger.debug(
        "[resolve_output_fields] cls=%r COLD cache miss, using wildcard=%r with post_strip=%r",
        obj_class,
        output_fields,
        sorted(strip),
    )
    return output_fields, strip


def apply_field_strip(result: dict, strip: frozenset[str]) -> dict:
    """Remove strip fields from every object in an iTop result dict.

    Mutates result in-place and returns it.
    Seeds _ITOP_CLASS_REGISTRY with the full field set BEFORE stripping, so
    the warm-cache path in resolve_output_fields sees the complete inventory
    on the next call.
    """
    if not strip:
        return result
    objects = result.get("objects")
    if not objects:
        return result
    for obj_data in objects.values():
        cls = obj_data.get("class", "")
        fields = obj_data.get("fields")
        if not isinstance(fields, dict):
            continue
        _seed_field_cache(cls, fields)   # seed before popping
        stripped = [key for key in strip if key in fields]
        for key in strip:
            fields.pop(key, None)
        if stripped:
            logger.debug(
                "[apply_field_strip] cls=%r stripped fields=%r", cls, stripped
            )
        else:
            logger.debug(
                "[apply_field_strip] cls=%r no fields to strip from strip set=%r",
                cls,
                sorted(strip),
            )
    return result


# ---------------------------------------------------------------------------
# Classes that carry a human-readable "ref" ticket number in iTop
# ---------------------------------------------------------------------------

CLASSES_WITH_REF: frozenset[str] = frozenset({
    "UserRequest",
    "Incident",
    "Problem",
    "Change",
    "ChangeRequest",
    "NormalChange",
    "EmergencyChange",
    "RoutineChange",
    "ServiceRequest",
    "RFC",
    "RFI",
})

# Matches a fully-formed iTop ref, e.g. "R-016271" or "I-003"
_REF_PATTERN = re.compile(r"^[A-Z]+-\d+$")

# Matches a bare integer string, e.g. "15525"
_BARE_NUMBER_PATTERN = re.compile(r"^\d+$")

# Fields injected by MCP output formatting that must never be sent to iTop.
_SYNTHETIC_FIELDS: frozenset[str] = frozenset({"link"})


def is_bare_number(key: Any) -> bool:
    """Return True if key is a bare integer or a string of only digits."""
    if isinstance(key, int):
        return True
    if isinstance(key, str) and _BARE_NUMBER_PATTERN.match(key):
        return True
    return False


def ensure_ref_field(obj_class: str, output_fields: str) -> str:
    """Inject 'ref' and strip synthetic/redundant fields for ticket classes.

    For classes in CLASSES_WITH_REF:
    - 'ref' is injected at the front if not already present.
    - 'id' is always removed (redundant once ref is present).
    - Synthetic MCP-injected fields (e.g. 'link') are always removed because
      they do not exist as iTop attributes and would cause API errors.

    '*' and '*+' are passed through unchanged (iTop handles field expansion).
    Non-ticket classes only have synthetic fields stripped.
    """
    if output_fields not in ("*", "*+"):
        fields = [f.strip() for f in output_fields.split(",") if f.strip()]
        fields = [f for f in fields if f not in _SYNTHETIC_FIELDS]
        output_fields = ", ".join(fields)

    if output_fields in ("*", "*+"):
        return output_fields
    if obj_class not in CLASSES_WITH_REF:
        return output_fields

    fields = [f.strip() for f in output_fields.split(",") if f.strip()]
    fields = [f for f in fields if f != "id"]
    if "ref" not in fields:
        fields.insert(0, "ref")
    return ", ".join(fields)


async def resolve_ticket_by_number(number: int, itop_request) -> tuple[str, str] | tuple[None, None]:
    """Resolve a bare ticket number to (obj_class, ref) via a single OQL call.

    iTop's Ticket base class covers UserRequest, Incident, Change, Problem, etc.
    The ref number part is always 6 digits (zero-padded), and is unique across
    all ticket subclasses -- so a single LIKE query is sufficient to identify
    both the real class and the full ref string.

    Example:
        resolve_ticket_by_number(15525, ...) -> ("Incident", "I-015525")

    Returns (None, None) when no ticket with that number is found.
    """
    suffix = str(number).zfill(6)
    oql = "SELECT Ticket WHERE ref LIKE '%" + suffix + "'"
    logger.debug("[resolve_ticket_by_number] number=%r suffix=%r oql=%r", number, suffix, oql)
    result = await itop_request({
        "operation": "core/get",
        "class": "Ticket",
        "key": oql,
        "output_fields": "ref",
        "limit": "1",
    })
    if result.get("code", -1) != 0:
        logger.debug(
            "[resolve_ticket_by_number] number=%r -> iTop error code=%r msg=%r",
            number,
            result.get("code"),
            result.get("message"),
        )
        return None, None
    for obj_data in (result.get("objects") or {}).values():
        obj_class = obj_data.get("class") or ""
        ref = (obj_data.get("fields") or {}).get("ref") or ""
        if obj_class and ref:
            logger.debug(
                "[resolve_ticket_by_number] number=%r -> resolved class=%r ref=%r",
                number,
                obj_class,
                ref,
            )
            return obj_class, ref
    logger.debug("[resolve_ticket_by_number] number=%r -> not found", number)
    return None, None


async def resolve_ticket_ref(
    obj_class: str,
    key: str,
    itop_request,
) -> tuple[str, Any]:
    """Resolve obj_class + key, performing a bare-number lookup when needed.

    If key is a bare number (e.g. "15525" or 15525) and obj_class is "Ticket"
    or not a known concrete ticket class, a single OQL probe is fired against
    the Ticket base class to find the real class and full ref.

    Returns (resolved_class, resolved_key) where resolved_key is either:
      - {"ref": "R-015525"}  (ref criteria dict)
      - the original parsed key (OQL string, JSON dict, etc.)

    This function is the single entry point for all tools that accept a
    user-supplied ticket identifier without knowing the class upfront.
    """
    logger.debug("[resolve_ticket_ref] cls=%r key=%r", obj_class, key)
    parsed = parse_key(key)

    # Already a fully-formed ref string -- use as-is
    if isinstance(parsed, str) and _REF_PATTERN.match(parsed):
        logger.debug(
            "[resolve_ticket_ref] key=%r is a fully-formed ref, returning cls=%r ref=%r",
            key,
            obj_class,
            parsed,
        )
        return obj_class, {"ref": parsed}

    # Bare number -- probe Ticket base class to find real class + ref
    if is_bare_number(parsed):
        number = int(parsed) if isinstance(parsed, str) else parsed
        logger.debug(
            "[resolve_ticket_ref] key=%r is bare number=%r, probing Ticket base class",
            key,
            number,
        )
        found_class, found_ref = await resolve_ticket_by_number(number, itop_request)
        if found_class and found_ref:
            logger.debug(
                "[resolve_ticket_ref] bare number=%r resolved -> cls=%r ref=%r",
                number,
                found_class,
                found_ref,
            )
            return found_class, {"ref": found_ref}
        # Not found -- fall through and let iTop return an error naturally
        suffix = str(number).zfill(6)
        fallback_oql = "SELECT Ticket WHERE ref LIKE '%" + suffix + "'"
        logger.debug(
            "[resolve_ticket_ref] bare number=%r not resolved, falling back to OQL=%r",
            number,
            fallback_oql,
        )
        return obj_class, fallback_oql

    # OQL, JSON dict, or anything else -- pass through
    logger.debug(
        "[resolve_ticket_ref] key=%r parsed as %r (OQL/dict), passing through cls=%r",
        key,
        type(parsed).__name__,
        obj_class,
    )
    return obj_class, parsed


def parse_key_for_ticket(obj_class: str, key: str) -> Any:
    """Synchronous key parser for ticket classes (no lookup, use resolve_ticket_ref for lookup).

    For cases where an async lookup is not possible (rare), this converts
    fully-formed refs to criteria dicts. Bare numbers are returned as-is
    (the async resolve_ticket_ref should be preferred instead).
    """
    parsed = parse_key(key)
    if obj_class in CLASSES_WITH_REF:
        if isinstance(parsed, str) and _REF_PATTERN.match(parsed):
            return {"ref": parsed}
    return parsed


async def resolve_key(obj_class: str, ref: str | None, numeric_id: Any, itop_request) -> Any:
    """Resolve a ticket identifier to a numeric key for mutation operations.

    Preference order:
    1. ref (ticket ref string, e.g. "R-016271") -- looked up via iTop.
    2. numeric_id as bare number -- triggers Ticket base class lookup.
    3. numeric_id as fallback (DB id).
    """
    if ref and isinstance(ref, str) and _REF_PATTERN.match(ref.strip()):
        result = await itop_request({
            "operation": "core/get",
            "class": obj_class,
            "key": {"ref": ref.strip()},
            "output_fields": "id",
        })
        objects = result.get("objects") or {}
        for _k, obj_data in objects.items():
            raw_id = obj_data.get("key") or (obj_data.get("fields") or {}).get("id")
            if raw_id is not None:
                try:
                    return int(raw_id)
                except (ValueError, TypeError):
                    pass

    if numeric_id is not None:
        # If numeric_id looks like a bare ticket number, resolve via Ticket base class
        if is_bare_number(numeric_id):
            number = int(numeric_id)
            found_class, found_ref = await resolve_ticket_by_number(number, itop_request)
            if found_class and found_ref:
                result = await itop_request({
                    "operation": "core/get",
                    "class": found_class,
                    "key": {"ref": found_ref},
                    "output_fields": "id",
                })
                objects = result.get("objects") or {}
                for _k, obj_data in objects.items():
                    raw_id = obj_data.get("key") or (obj_data.get("fields") or {}).get("id")
                    if raw_id is not None:
                        try:
                            return int(raw_id)
                        except (ValueError, TypeError):
                            pass
        try:
            return int(numeric_id)
        except (ValueError, TypeError):
            return numeric_id
    return numeric_id


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def str_or(d: dict, key: str, default: str = "") -> str:
    v = d.get(key)
    return str(v) if v is not None else default


def parse_key(key: str) -> Any:
    try:
        return json.loads(key)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return int(key)
    except ValueError:
        return key


def parse_json_arg(raw: str, arg_name: str) -> dict | str:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return f"Invalid JSON in '{arg_name}': {e.msg} at position {e.pos}"


def parse_date_range(start: str, end: str) -> Tuple[str, str]:
    """Normalize date strings; return (start, end) ISO format."""
    try:
        dt_start = (
            datetime.fromisoformat(start)
            if start
            else (datetime.now(timezone.utc) - timedelta(days=30))
        )
        dt_end = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    except ValueError:
        raise ValueError("Invalid date format. Use ISO 8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    return dt_start.strftime("%Y-%m-%d %H:%M:%S"), dt_end.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# iTop response formatters
# ---------------------------------------------------------------------------

def extract_objects(result: dict) -> list[dict]:
    """Extract list of {class, key, fields} from iTop response."""
    objs = result.get("objects")
    if not objs:
        return []
    out = []
    for _obj_key, obj_data in objs.items():
        out.append({
            "class": obj_data.get("class", "?"),
            "key": obj_data.get("key", "?"),
            "fields": obj_data.get("fields", {}),
        })
    return out


def format_objects(result: dict) -> str:
    """Format iTop response objects into readable string.

    Side effect: seeds _ITOP_CLASS_REGISTRY with the field inventory for
    every class seen, so resolve_output_fields hits the warm-cache path on
    subsequent calls for the same class.
    """
    if result.get("code", -1) != 0:
        return f"Error (code {result.get('code')}): {str_or(result, 'message', 'Unknown error')}"
    objects = result.get("objects")
    if not objects:
        return str_or(result, "message", "No objects found.")
    lines = [str_or(result, "message", "")]
    for _obj_key, obj_data in objects.items():
        cls = str_or(obj_data, "class", "?")
        oid = str_or(obj_data, "key", "?")
        fields = obj_data.get("fields", {}) or {}
        _seed_field_cache(cls, fields)
        ref = fields.get("ref")
        label = ref if ref else oid
        lines.append(f"\n--- {cls}::{label} ---")
        if ITOP_URL and oid:
            lines.append(
                f"  link: {ITOP_URL}/pages/UI.php?operation=details&class={cls}&id={oid}"
            )
        for fn, fv in fields.items():
            if fn == "ref" or (ref and fn == "id"):
                continue
            if isinstance(fv, (dict, list)):
                fv = json.dumps(fv, indent=2, ensure_ascii=False)
            lines.append(f"  {fn}: {fv}")
    return "\n".join(lines)


def format_table(header: list[str], rows: list[list[str]]) -> str:
    """Simple aligned table formatter."""
    if not rows:
        return "(no data)"
    col_widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))
    lines = []
    sep = " | ".join(h.ljust(w) for h, w in zip(header, col_widths))
    lines.append(sep)
    lines.append("-+-".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(" | ".join(c.ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


def format_duration(seconds: float) -> str:
    """Format seconds to human-readable duration."""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}min"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}min"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"
