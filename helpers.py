"""
Pure utility / formatting helpers shared across all tool modules.

helpers/
    html.py        -- strip / parse HTML
    formatters.py  -- format_objects, format_and_cache, format_table
    resolvers.py   -- resolve_key, resolve_ref_class_by_ref_part, ensure_class_exists
    sla.py         -- SLA constants and helpers
    utils.py       -- str_or, parse_key, parse_json_arg, parse_date_range, coerce_ref

This flat file is the compatibility shim; all names are re-exported so existing
imports across tools, cache, and server continue to work without change.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

from config import ITOP_URL, RESOLVE_KEY_CACHE_TTL
from cache import (
    cache_cleanup,
    cache_get,
    cache_set,
    get_class_fields,
    registry_get_fields,
    registry_get_meta,
    registry_set_meta,
    seed_field_cache,
    _registry_entry,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

_BLOCK_TAGS = re.compile(
    r"<(?:/?(?:p|div|tr|li|dt|dd|blockquote|pre|"
    r"h[1-6]|ul|ol|dl|table|thead|tbody|tfoot|"
    r"figure|figcaption|section|article|aside|header|footer|main|"
    r"hr)\b[^>]*|br\s*/?)>",
    re.IGNORECASE,
)

_MSO_CONDITIONAL_RE = re.compile(
    r"<!\[if[^\]]*\]>.*?<!\[endif\]>",
    re.IGNORECASE | re.DOTALL,
)

_ANY_TAG_RE = re.compile(r"<[^>]+>", re.IGNORECASE)

_HTML_ENTITIES: dict[str, str] = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&apos;": "'", "&nbsp;": " ",
    "&#39;": "'",
}

_HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[\da-fA-F]+|[a-zA-Z]+);")

# Matches <img> tags that carry data-img-id and data-img-secret attributes.
# Both attributes may appear in any order and the tag may have other attrs.
_INLINE_IMG_RE = re.compile(
    r'<img\b[^>]*\bdata-img-id=["\']?(\d+)["\']?'
    r'[^>]*\bdata-img-secret=["\']?([0-9a-fA-F]+)["\']?[^>]*>|'
    r'<img\b[^>]*\bdata-img-secret=["\']?([0-9a-fA-F]+)["\']?'
    r'[^>]*\bdata-img-id=["\']?(\d+)["\']?[^>]*>',
    re.IGNORECASE | re.DOTALL,
)


def _decode_entity(m: re.Match) -> str:
    raw = m.group(0)
    inner = raw[1:-1]
    try:
        if inner.startswith("#x") or inner.startswith("#X"):
            return chr(int(inner[2:], 16))
        if inner.startswith("#"):
            return chr(int(inner[1:]))
    except (ValueError, OverflowError):
        pass
    return raw


def _strip_html(value: str) -> str:
    """Convert HTML to clean plain text, preserving meaningful line breaks."""
    if not value or "<" not in value:
        return value
    value = _MSO_CONDITIONAL_RE.sub("", value)
    value = _BLOCK_TAGS.sub("\n", value)
    value = _ANY_TAG_RE.sub("", value)
    for entity, char in _HTML_ENTITIES.items():
        value = value.replace(entity, char)
    value = _HTML_ENTITY_RE.sub(_decode_entity, value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    value = "\n".join(lines)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def strip_html_recursive(obj: Any) -> Any:
    """Recursively strip HTML from all string values in dicts/lists."""
    if isinstance(obj, str):
        return _strip_html(obj)
    if isinstance(obj, dict):
        return {k: strip_html_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_html_recursive(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Inline image parsing
# ---------------------------------------------------------------------------

def parse_objects(result: dict) -> dict[str, list[dict]]:
    """Extract inline image refs from all string fields across all objects.

    Scans every string value in result['objects'] -- including strings nested
    inside dicts and lists (e.g. private_log.entries[].message_html) -- for
    <img> tags that carry both data-img-id and data-img-secret attributes.
    Returns a mapping of
    '{obj_class}::{obj_id}' -> [{'id': str, 'secret': str}, ...].

    The field values are NOT modified; stripping happens in _format_objects.
    Deduplication per ticket is applied (same img_id appears only once).
    """
    refs: dict[str, list[dict]] = {}
    objects = result.get("objects")
    if not objects:
        return refs

    for _obj_key, obj_data in objects.items():
        cls = obj_data.get("class") or ""
        oid = str(obj_data.get("key") or "")
        if not cls or not oid:
            continue

        ticket_key = cls + "::" + oid
        seen_ids: set[str] = set()
        found: list[dict] = []

        # Use an explicit stack to descend into nested dicts and lists so
        # that strings buried in e.g. private_log.entries[].message_html
        # are scanned. No helper function is used; traversal is inline.
        stack = list((obj_data.get("fields") or {}).values())
        while stack:
            fv = stack.pop()
            if isinstance(fv, dict):
                stack.extend(fv.values())
                continue
            if isinstance(fv, list):
                stack.extend(fv)
                continue
            if not isinstance(fv, str) or "data-img-id" not in fv:
                continue
            for m in _INLINE_IMG_RE.finditer(fv):
                # Group layout: (id-first-img-id, id-first-secret,
                #                secret-first-secret, secret-first-img-id)
                if m.group(1) and m.group(2):
                    img_id = m.group(1)
                    secret = m.group(2)
                else:
                    img_id = m.group(4)
                    secret = m.group(3)
                if img_id and img_id not in seen_ids:
                    seen_ids.add(img_id)
                    found.append({"id": img_id, "secret": secret})

        if found or ticket_key not in refs:
            refs[ticket_key] = found

        logger.debug(
            "[parse_objects] cls=%r id=%r found %d inline img ref(s)",
            cls, oid, len(found),
        )

    return refs


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
# Classes that carry a human-readable "ref" ticket number in iTop
# ---------------------------------------------------------------------------

CLASSES_WITH_REF: frozenset[str] = frozenset({
    "Ticket",
    "UserRequest",
    "Incident",
    "Problem",
    "Change",
    "NormalChange",
    "EmergencyChange",
    "RoutineChange",
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


def coerce_ref(ticket_ref: str, key: Any) -> str | None:
    """Normalise the ticket_ref / key pair used by most write tools.

    Both crud.py tools (update, delete, apply_stimulus) and the attachment
    tools accept an optional ticket_ref AND an optional key parameter.
    This helper merges them into a single resolved string, or None when
    both are empty, so each tool no longer has to repeat the same one-liner.

    Args:
        ticket_ref: Preferred human-readable ticket ref, e.g. 'R-016292'.
        key:        Fallback key (numeric ID, OQL, or empty string).

    Returns:
        Stripped non-empty string, or None when both inputs are falsy.
    """
    result = str(ticket_ref or key or "").strip()
    return result if result else None


def ensure_ref_field(obj_class: str, output_fields: str) -> str:
    """Inject 'ref' and strip synthetic/redundant fields for ticket classes."""
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


async def ensure_class_exists(candidates: list[str], itop_request_fn) -> str:
    """Return the first class in candidates that exists on the iTop server."""
    from client import itop_core_get

    for cls in candidates:
        entry = _registry_entry(cls)
        if entry["exists"] is True:
            logger.debug("[registry] ensure_class_exists cls=%r -> cached True", cls)
            return cls
        if entry["exists"] is False:
            logger.debug("[registry] ensure_class_exists cls=%r -> cached False, skip", cls)
            continue
        r = await itop_core_get(itop_request_fn, cls, "SELECT " + cls, fields="id", limit=1)
        if r.get("code") == 0:
            entry["exists"] = True
            for obj_data in (r.get("objects") or {}).values():
                seed_field_cache(cls, obj_data.get("fields") or {})
            logger.debug("[registry] ensure_class_exists cls=%r -> exists=True (probed)", cls)
            return cls
        else:
            entry["exists"] = False
            logger.debug(
                "[registry] ensure_class_exists cls=%r -> exists=False code=%r msg=%r",
                cls, r.get("code"), r.get("message"),
            )
    logger.debug("[registry] ensure_class_exists candidates=%r -> none found", candidates)
    return ""


async def resolve_output_fields(
    obj_class: str,
    output_fields: str,
    strip: frozenset[str],
    itop_request_fn,
) -> tuple[str, frozenset[str]]:
    """Resolve (output_fields, strip) into (fields_to_request, post_strip_set)."""
    logger.debug(
        "[resolve_output_fields] cls=%r output_fields=%r strip=%r",
        obj_class, output_fields, sorted(strip),
    )
    is_wildcard = output_fields in ("*", "*+")

    if not is_wildcard or not strip:
        logger.debug(
            "[resolve_output_fields] passthrough (explicit or no strip) -> %r strip=%r",
            output_fields, sorted(strip),
        )
        return output_fields, strip

    cached_fields = registry_get_fields(obj_class)

    if cached_fields:
        explicit = sorted(cached_fields - strip - _SYNTHETIC_FIELDS)
        if obj_class in CLASSES_WITH_REF:
            if "ref" in explicit:
                explicit = ["ref"] + [f for f in explicit if f not in ("ref", "id")]
        if not explicit:
            logger.debug(
                "[resolve_output_fields] cls=%r warm cache but strip removed all fields, fallback to wildcard",
                obj_class,
            )
            return output_fields, strip
        result_fields = ", ".join(explicit)
        logger.debug(
            "[resolve_output_fields] cls=%r WARM cache hit, explicit fields=%r post_strip=empty",
            obj_class, result_fields,
        )
        return result_fields, frozenset()

    logger.debug(
        "[resolve_output_fields] cls=%r COLD cache miss, using wildcard=%r with post_strip=%r",
        obj_class, output_fields, sorted(strip),
    )
    return output_fields, strip


def apply_field_strip(result: dict, strip: frozenset[str]) -> dict:
    """Remove strip fields from every object in an iTop result dict."""
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
        seed_field_cache(cls, fields)
        stripped = [key for key in strip if key in fields]
        for key in strip:
            fields.pop(key, None)
        if stripped:
            logger.debug("[apply_field_strip] cls=%r stripped fields=%r", cls, stripped)
        else:
            logger.debug(
                "[apply_field_strip] cls=%r no fields to strip from strip set=%r",
                cls, sorted(strip),
            )
    return result


# ---------------------------------------------------------------------------
# Ticket ref resolver
# ---------------------------------------------------------------------------

async def resolve_ref_class_by_ref_part(
    obj_class: str,
    key: str,
    itop_request_fn,
) -> tuple[str, int, str] | tuple[None, None, None]:
    """Resolve a ref or bare number to (resolved_class, numeric_id, ref_string).

    Builds an OQL query against obj_class using a suffix LIKE match on the
    ref field. obj_class must be a member of CLASSES_WITH_REF (including the
    abstract Ticket base class which carries a ref field in iTop).

    Returns (None, None, None) when no matching object is found.
    """
    from client import itop_core_get

    suffix = str(key).zfill(6)
    oql = "SELECT " + obj_class + " WHERE ref LIKE '%" + suffix + "'"
    logger.debug(
        "[resolve_ref_class_by_ref_part] key=%r suffix=%r oql=%r", key, suffix, oql
    )
    result = await itop_core_get(itop_request_fn, obj_class, oql, fields="id,ref", limit=1)
    if result.get("code", -1) != 0:
        logger.debug(
            "[resolve_ref_class_by_ref_part] key=%r -> iTop error code=%r msg=%r",
            key, result.get("code"), result.get("message"),
        )
        return None, None, None
    for obj_data in (result.get("objects") or {}).values():
        resolved_class = obj_data.get("class") or ""
        found_ref = (obj_data.get("fields") or {}).get("ref") or ""
        found_id = (obj_data.get("fields") or {}).get("id") or ""
        if resolved_class and found_ref and found_id:
            logger.debug(
                "[resolve_ref_class_by_ref_part] key=%r -> class=%r id=%r ref=%r",
                key, resolved_class, found_id, found_ref,
            )
            try:
                return resolved_class, int(found_id), found_ref
            except (ValueError, TypeError):
                pass
    logger.debug("[resolve_ref_class_by_ref_part] key=%r -> not found", key)
    return None, None, None


async def resolve_key(
    obj_class: str,
    ref: str | None,
    itop_request_fn,
) -> tuple[str, Any]:
    """Resolve an object identifier to (resolved_class, numeric_key).

    For CLASSES_WITH_REF: ref is matched via suffix OQL on the ref field.
    For all other classes: ref is passed directly as key in a core/get call.
    A TTL cache avoids repeated iTop round-trips for the same identifier.
    Fallback: int(ref) or raw ref string.
    """
    from client import itop_core_get

    cache_cleanup()

    ref_str = str(ref).strip() if ref is not None else ""
    if not ref_str:
        return obj_class, ref

    cached = cache_get(obj_class, ref_str)
    if cached is not None:
        return cached[0], cached[1]

    if obj_class in CLASSES_WITH_REF:
        found_class, found_id, found_ref = await resolve_ref_class_by_ref_part(
            obj_class, ref_str, itop_request_fn
        )
        if found_class is not None and found_id is not None:
            logger.debug(
                "[resolve_key] ref=%r -> class=%r key=%r ref=%r",
                ref_str, found_class, found_id, found_ref,
            )
            cache_set(obj_class, ref_str, found_class, found_id)
            return found_class, found_id
    else:
        result = await itop_core_get(itop_request_fn, obj_class, ref_str, fields="id")
        objects = result.get("objects") or {}
        for obj_data in objects.values():
            resolved_class = obj_data.get("class") or obj_class
            raw_id = obj_data.get("key") or (obj_data.get("fields") or {}).get("id")
            if raw_id is not None:
                try:
                    numeric_id = int(raw_id)
                    logger.debug(
                        "[resolve_key] key=%r -> class=%r id=%r",
                        ref_str, resolved_class, numeric_id,
                    )
                    cache_set(obj_class, ref_str, resolved_class, numeric_id)
                    return resolved_class, numeric_id
                except (ValueError, TypeError):
                    pass

    try:
        numeric = int(ref_str)
        logger.debug(
            "[resolve_key] fallback int: ref=%r -> class=%r key=%r",
            ref_str, obj_class, numeric,
        )
        return obj_class, numeric
    except (ValueError, TypeError):
        pass
    logger.debug(
        "[resolve_key] fallback raw: ref=%r -> class=%r key=%r",
        ref_str, obj_class, ref_str,
    )
    return obj_class, ref_str


# ---------------------------------------------------------------------------
# Image count helper
# ---------------------------------------------------------------------------

async def fetch_image_counts(
    obj_class: str,
    obj_id: str | int,
    itop_request_fn,
) -> tuple[int, int]:
    """Return (attachment_count, inline_image_count) for a ticket object."""
    from client import itop_core_get

    oid = str(obj_id)
    att_oql = (
        "SELECT Attachment"
        " WHERE item_class = '" + obj_class + "'"
        " AND item_id = " + oid
    )
    att_result = await itop_core_get(itop_request_fn, "Attachment", att_oql, fields="id")
    att_count = len(att_result.get("objects") or {})
    logger.debug(
        "[fetch_image_counts] cls=%r id=%r Attachment count=%d",
        obj_class, oid, att_count,
    )

    ii_oql = (
        "SELECT InlineImage"
        " WHERE item_class = '" + obj_class + "'"
        " AND item_id = " + oid
    )
    ii_result = await itop_core_get(itop_request_fn, "InlineImage", ii_oql, fields="id")
    ii_count = len(ii_result.get("objects") or {})
    logger.debug(
        "[fetch_image_counts] cls=%r id=%r InlineImage count=%d",
        obj_class, oid, ii_count,
    )

    return att_count, ii_count


# ---------------------------------------------------------------------------
# Generic utilities
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


def _try_json_parse(raw: str):
    """Shared JSON parse used by parse_key and parse_json_arg."""
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, e


def parse_json_arg(raw: str, arg_name: str) -> dict | str:
    parsed, err = _try_json_parse(raw)
    if err is not None:
        return f"Invalid JSON in '{arg_name}': {err.msg} at position {err.pos}"
    return parsed


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


def _format_objects(result: dict) -> tuple[str, dict[str, list[dict]]]:
    """Format iTop response objects into a readable string and extract inline image refs.

    Internal implementation used exclusively by format_and_cache().
    External callers should always use format_and_cache() which additionally
    persists inline image refs to the SQLite cache as a side effect.

    Returns a tuple of:
      - text: formatted string suitable for MCP tool output
      - refs: mapping of '{obj_class}::{obj_id}' -> [{'id', 'secret'}, ...]
              as returned by parse_objects(). Empty dict when no refs found.

    Processing order:
      1. parse_objects() scans all string values (including nested dicts/lists)
         for data-img-id/data-img-secret <img> tags and collects refs WITHOUT
         modifying field values.
      2. HTML stripping then removes all tags (including <img>) from fields.
      3. The formatted text is assembled from the stripped values.

    Seeds the field registry from every response so resolve_output_fields
    hits the warm-cache path on subsequent calls for the same class.
    Fields starting with '_' are rendered as bracketed synthetic annotations.
    """
    if result.get("code", -1) != 0:
        return (
            "Error (code " + str(result.get("code")) + "): "
            + str_or(result, "message", "Unknown error"),
            {},
        )
    objects = result.get("objects")
    if not objects:
        return str_or(result, "message", "No objects found."), {}

    # Step 1: extract inline image refs before any stripping.
    refs = parse_objects(result)

    # Step 2: format and strip.
    lines = [str_or(result, "message", "")]
    for _obj_key, obj_data in objects.items():
        cls = str_or(obj_data, "class", "?")
        oid = str_or(obj_data, "key", "?")
        fields = obj_data.get("fields", {}) or {}
        seed_field_cache(cls, fields)
        ref = fields.get("ref")
        label = ref if ref else oid
        lines.append("\n--- " + cls + "::" + label + " ---")
        if ITOP_URL and oid:
            lines.append(
                "  link: " + ITOP_URL + "/pages/UI.php?operation=details&class=" + cls + "&id=" + oid
            )
        synthetic = {}
        for fn, fv in fields.items():
            if fn.startswith("_"):
                synthetic[fn] = fv
                continue
            if fn == "ref" or (ref and fn == "id"):
                continue
            if isinstance(fv, str):
                fv = _strip_html(fv)
            elif isinstance(fv, (dict, list)):
                fv = strip_html_recursive(fv)
                fv = json.dumps(fv, indent=2, ensure_ascii=False)
            lines.append("  " + fn + ": " + str(fv))
        for fn, fv in synthetic.items():
            display_name = fn.lstrip("_")
            lines.append("  [" + display_name + "] " + str(fv))
    return "\n".join(lines), refs


# Keep the old public name as an alias so any external caller is not broken.
# Prefer _format_objects internally; prefer format_and_cache from tools.
format_objects = _format_objects


def format_and_cache(result: dict) -> str:
    """Format iTop response and persist inline image refs to SQLite.

    Calls _format_objects() to get the formatted text and the inline image
    refs map, then writes each ticket's refs to the attachment_store cache
    via write_inline_image_refs(). Returns only the formatted string so all
    existing callers can be migrated by replacing format_objects() with
    format_and_cache() without any other changes.

    The SQLite write is a transparent side effect. The cache entry is keyed
    by (obj_class, obj_id) and expires after INLINE_IMAGE_REF_TTL seconds.
    """
    # Import here to avoid a circular import at module level:
    # helpers -> attachment_store -> config (safe), but attachment_store
    # must not import helpers at the top level.
    from attachment_store import write_inline_image_refs

    text, refs = _format_objects(result)

    for ticket_key, img_refs in refs.items():
        try:
            cls, oid = ticket_key.split("::", 1)
            write_inline_image_refs(cls, oid, img_refs)
        except Exception as exc:
            logger.warning(
                "[format_and_cache] failed to write inline image refs for %r: %s",
                ticket_key, exc,
            )

    return text


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
