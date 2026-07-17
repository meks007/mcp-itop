"""
Pure utility / formatting helpers shared across all tool modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

from config import ITOP_URL, RESOLVE_KEY_CACHE_TTL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

# Tags whose opening or closing form represents a line break in the output.
# Closing a block tag ends that "paragraph"; <br> is always a newline.
_BLOCK_TAGS = re.compile(
    r"<(?:/?(?:p|div|tr|li|dt|dd|blockquote|pre|"
    r"h[1-6]|ul|ol|dl|table|thead|tbody|tfoot|"
    r"figure|figcaption|section|article|aside|header|footer|main|"
    r"hr)\b[^>]*|br\s*/?)>",
    re.IGNORECASE,
)

# MS-Office conditional comments: <![if ...]> ... <![endif]> -- drop entirely.
_MSO_CONDITIONAL_RE = re.compile(
    r"<!\[if[^\]]*\]>.*?<!\[endif\]>",
    re.IGNORECASE | re.DOTALL,
)

# Any remaining tag (inline or unknown).
_ANY_TAG_RE = re.compile(r"<[^>]+>", re.IGNORECASE)

# HTML entities we handle by name.
_HTML_ENTITIES: dict[str, str] = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&apos;": "'", "&nbsp;": " ",
    "&#39;": "'",
}

# All remaining numeric / named entities.
_HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[\da-fA-F]+|[a-zA-Z]+);")


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
    return raw  # leave unknown named entities as-is


def _strip_html(value: str) -> str:
    """Convert HTML to clean plain text, preserving meaningful line breaks.

    Steps:
    1. Drop MS-Office conditional comments wholesale.
    2. Replace block-level tags and <br> with newlines.
    3. Strip all remaining tags.
    4. Decode HTML entities.
    5. Collapse horizontal whitespace on each line, then collapse excess blank lines.
    """
    if not value or "<" not in value:
        return value

    # 1. Remove MSO conditional noise
    value = _MSO_CONDITIONAL_RE.sub("", value)

    # 2. Block tags / <br> -> newline
    value = _BLOCK_TAGS.sub("\n", value)

    # 3. Strip everything else
    value = _ANY_TAG_RE.sub("", value)

    # 4. Entities
    for entity, char in _HTML_ENTITIES.items():
        value = value.replace(entity, char)
    value = _HTML_ENTITY_RE.sub(_decode_entity, value)

    # 5. Normalise whitespace:
    #    - collapse horizontal whitespace within each line
    #    - collapse runs of 3+ newlines to 2
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
        before_set = entry["fields"]
        new = incoming - before_set
        entry["fields"] = before_set | incoming
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
            obj_class,
            result_fields,
        )
        return result_fields, frozenset()

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
        _seed_field_cache(cls, fields)
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


# ---------------------------------------------------------------------------
# resolve_key lookup cache
# ---------------------------------------------------------------------------
# Cache shape: { (obj_class, ref): {"class": str, "id": int, "ts": float} }
# Eviction is lazy: expired entries are removed at the start of each
# resolve_key call. Set RESOLVE_KEY_CACHE_TTL=0 to disable caching.

_RESOLVE_KEY_CACHE: dict[tuple[str, str], dict] = {}


def _cache_cleanup() -> None:
    """Remove all cache entries whose TTL has expired."""
    if RESOLVE_KEY_CACHE_TTL <= 0:
        return
    now = time.monotonic()
    expired = [
        k for k, v in _RESOLVE_KEY_CACHE.items()
        if now - v["ts"] > RESOLVE_KEY_CACHE_TTL
    ]
    for k in expired:
        del _RESOLVE_KEY_CACHE[k]
    if expired:
        logger.debug("[resolve_key_cache] evicted %d expired entry/entries", len(expired))


def _cache_get(obj_class: str, ref: str) -> tuple[str, int] | None:
    """Return (resolved_class, resolved_id) from cache, or None on miss/expiry."""
    if RESOLVE_KEY_CACHE_TTL <= 0:
        return None
    entry = _RESOLVE_KEY_CACHE.get((obj_class, ref))
    if entry is None:
        return None
    if time.monotonic() - entry["ts"] > RESOLVE_KEY_CACHE_TTL:
        del _RESOLVE_KEY_CACHE[(obj_class, ref)]
        logger.debug("[resolve_key_cache] expired entry for class=%r ref=%r", obj_class, ref)
        return None
    logger.debug(
        "[resolve_key_cache] hit class=%r ref=%r -> resolved_class=%r id=%r",
        obj_class, ref, entry["class"], entry["id"],
    )
    return entry["class"], entry["id"]


def _cache_set(obj_class: str, ref: str, resolved_class: str, resolved_id: int) -> None:
    """Store a resolved (class, id) pair in the cache."""
    if RESOLVE_KEY_CACHE_TTL <= 0:
        return
    _RESOLVE_KEY_CACHE[(obj_class, ref)] = {
        "class": resolved_class,
        "id": resolved_id,
        "ts": time.monotonic(),
    }
    logger.debug(
        "[resolve_key_cache] stored class=%r ref=%r -> resolved_class=%r id=%r",
        obj_class, ref, resolved_class, resolved_id,
    )


# ---------------------------------------------------------------------------
# Ticket ref resolver
# ---------------------------------------------------------------------------

async def resolve_ref_class_by_ref_part(
    obj_class: str,
    key: str,
    itop_request,
) -> tuple[str, int, str] | tuple[None, None, None]:
    """Resolve a ref or bare number to (resolved_class, numeric_id, ref_string).

    Builds an OQL query against obj_class using a suffix LIKE match on the
    ref field. obj_class must be a class that carries a ref field (i.e. a
    member of CLASSES_WITH_REF, including the abstract Ticket base class).

    Example:
        resolve_ref_class_by_ref_part("Ticket", "16271", ...) ->
            ("Incident", 15525, "I-016271")

    Returns (None, None, None) when no matching object is found.
    """
    suffix = str(key).zfill(6)
    oql = "SELECT " + obj_class + " WHERE ref LIKE '%" + suffix + "'"
    logger.debug(
        "[resolve_ref_class_by_ref_part] key=%r suffix=%r oql=%r", key, suffix, oql
    )
    result = await itop_request({
        "operation": "core/get",
        "class": obj_class,
        "key": oql,
        "output_fields": "id,ref",
        "limit": "1",
    })
    if result.get("code", -1) != 0:
        logger.debug(
            "[resolve_ref_class_by_ref_part] key=%r -> iTop error code=%r msg=%r",
            key,
            result.get("code"),
            result.get("message"),
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


# ---------------------------------------------------------------------------
# Primary identifier resolver
# ---------------------------------------------------------------------------

async def resolve_key(
    obj_class: str,
    ref: str | None,
    itop_request,
) -> tuple[str, Any]:
    """Resolve an object identifier to (resolved_class, numeric_key).

    Returns a tuple so callers can use the real iTop class discovered during
    resolution (e.g. Incident instead of Ticket).

    For classes in CLASSES_WITH_REF (ticket-like objects):
      - ref is matched via a suffix OQL on the ref field using
        resolve_ref_class_by_ref_part. Both fully-formed refs ("I-016271")
        and bare numbers ("16271" or 16271) are accepted.
      - A lookup cache with TTL (RESOLVE_KEY_CACHE_TTL seconds) avoids
        repeated iTop round-trips for the same identifier.
      - Fallback: if the lookup yields nothing, int(ref) is tried as a
        raw DB id with the class unchanged.

    For all other classes:
      - ref is passed directly as the key in a core/get call with
        output_fields=id. This accepts OQL strings, JSON criteria strings,
        or bare numeric ids.
      - The real class is read from the response object (iTop may return a
        concrete subclass even when querying a base class).
      - A lookup cache with the same TTL applies.
      - Fallback: int(ref) or raw ref string.
    """
    # Lazy TTL cleanup on every call.
    _cache_cleanup()

    ref_str = str(ref).strip() if ref is not None else ""

    if not ref_str:
        return obj_class, ref

    # -- Cache lookup --------------------------------------------------------
    cached = _cache_get(obj_class, ref_str)
    if cached is not None:
        return cached[0], cached[1]

    # -- CLASSES_WITH_REF branch ---------------------------------------------
    if obj_class in CLASSES_WITH_REF:
        found_class, found_id, found_ref = await resolve_ref_class_by_ref_part(
            obj_class, ref_str, itop_request
        )
        if found_class is not None and found_id is not None:
            logger.debug(
                "[resolve_key] ref=%r -> class=%r key=%r ref=%r",
                ref_str, found_class, found_id, found_ref,
            )
            _cache_set(obj_class, ref_str, found_class, found_id)
            return found_class, found_id

    # -- Non-ref class branch ------------------------------------------------
    else:
        result = await itop_request({
            "operation": "core/get",
            "class": obj_class,
            "key": ref_str,
            "output_fields": "id",
        })
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
                    _cache_set(obj_class, ref_str, resolved_class, numeric_id)
                    return resolved_class, numeric_id
                except (ValueError, TypeError):
                    pass

    # -- Fallback ------------------------------------------------------------
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
    itop_request,
) -> tuple[int, int]:
    """Return (attachment_count, inline_image_count) for a ticket object.

    Both queries request only 'id' -- no contents blob is downloaded.
    Attachment count covers ALL Attachment records regardless of MIME type
    because mimetype is embedded in the contents blob and cannot be filtered
    via OQL without fetching it. InlineImage records are always images.

    Returns (0, 0) silently on any iTop error so callers are never blocked.
    Only called for classes in CLASSES_WITH_REF.
    """
    oid = str(obj_id)

    att_result = await itop_request({
        "operation": "core/get",
        "class": "Attachment",
        "key": (
            "SELECT Attachment"
            " WHERE item_class = '" + obj_class + "'"
            " AND item_id = " + oid
        ),
        "output_fields": "id",
    })
    att_count = len(att_result.get("objects") or {})
    logger.debug(
        "[fetch_image_counts] cls=%r id=%r Attachment count=%d",
        obj_class, oid, att_count,
    )

    ii_result = await itop_request({
        "operation": "core/get",
        "class": "InlineImage",
        "key": (
            "SELECT InlineImage"
            " WHERE item_class = '" + obj_class + "'"
            " AND item_id = " + oid
        ),
        "output_fields": "id",
    })
    ii_count = len(ii_result.get("objects") or {})
    logger.debug(
        "[fetch_image_counts] cls=%r id=%r InlineImage count=%d",
        obj_class, oid, ii_count,
    )

    return att_count, ii_count


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

    HTML tags and entities are stripped from all string field values before
    rendering, removing noise from iTop's rich-text fields.

    Fields whose name starts with '_' are treated as synthetic annotations
    injected by the caller (e.g. '_images'). They are rendered at the end of
    each object block with a bracketed label instead of the raw underscore
    name, and are never passed to iTop.
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
            lines.append(f"  {fn}: {fv}")
        # Render synthetic annotations last, with a bracketed label.
        for fn, fv in synthetic.items():
            display_name = fn.lstrip("_")
            lines.append(f"  [{display_name}] {fv}")
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
