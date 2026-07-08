"""
Pure utility / formatting helpers shared across all tool modules.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

from config import ITOP_URL


# -------------------------------------------------------------------------
# SLA helpers
# -------------------------------------------------------------------------

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


# -------------------------------------------------------------------------
# Classes that carry a human-readable "ref" ticket number in iTop
# -------------------------------------------------------------------------

# iTop assigns a "ref" field (e.g. "R-000123") to all ticket-like classes.
# Used by ensure_ref_field() and parse_key_for_ticket().
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

# Matches iTop ticket ref strings like "R-000123", "INC-42", "P-007".
_REF_PATTERN = re.compile(r"^[A-Z]+-\d+$")

# Fields injected by MCP output formatting that must never be sent to iTop.
_SYNTHETIC_FIELDS: frozenset[str] = frozenset({"link"})


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
    # Strip synthetic fields universally, even for non-ticket classes
    if output_fields not in ("*", "*+"):
        fields = [f.strip() for f in output_fields.split(",") if f.strip()]
        fields = [f for f in fields if f not in _SYNTHETIC_FIELDS]
        output_fields = ", ".join(fields)

    if output_fields in ("*", "*+"):
        return output_fields
    if obj_class not in CLASSES_WITH_REF:
        return output_fields

    fields = [f.strip() for f in output_fields.split(",") if f.strip()]
    # Always strip id for ticket classes - ref is the canonical identifier
    fields = [f for f in fields if f != "id"]
    if "ref" not in fields:
        fields.insert(0, "ref")
    return ", ".join(fields)


def parse_key_for_ticket(obj_class: str, key: str) -> Any:
    """Parse a key, resolving ref strings to JSON criteria for ticket classes.

    For classes in CLASSES_WITH_REF, if the key looks like a ticket ref
    (e.g. "R-000123", "INC-42"), it is converted to {"ref": "<value>"} so
    iTop resolves it server-side. All other key forms (numeric ID, OQL,
    JSON) are handled by the standard parse_key() logic.
    """
    parsed = parse_key(key)
    if (
        obj_class in CLASSES_WITH_REF
        and isinstance(parsed, str)
        and _REF_PATTERN.match(parsed)
    ):
        return {"ref": parsed}
    return parsed


async def resolve_key(obj_class: str, ref: str | None, numeric_id: Any, itop_request) -> Any:
    """Resolve a ticket identifier to a numeric key for mutation operations.

    Preference order:
    1. If ref is provided and looks like a ticket ref, look up the numeric key
       via iTop and return it. This guarantees the correct object is targeted.
    2. If ref is not a recognizable ref string, fall back to numeric_id.
    3. If neither is usable, return numeric_id as-is.

    Args:
        obj_class: iTop class (e.g. UserRequest).
        ref: Ticket ref string (e.g. "R-016271") or None.
        numeric_id: Numeric ID or string ID as fallback.
        itop_request: Async callable for iTop REST requests.

    Returns:
        Numeric integer key for use in iTop mutation operations.
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
    # Fallback to numeric_id
    if numeric_id is not None:
        try:
            return int(numeric_id)
        except (ValueError, TypeError):
            return numeric_id
    return numeric_id


# -------------------------------------------------------------------------
# Generic helpers
# -------------------------------------------------------------------------

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


# -------------------------------------------------------------------------
# iTop response formatters
# -------------------------------------------------------------------------

def extract_objects(result: dict) -> list[dict]:
    """Extract list of {class, key, fields} from iTop response."""
    objs = result.get("objects")
    if not objs:
        return []
    out = []
    for _obj_key, obj_data in objs.items():
        out.append(
            {
                "class": obj_data.get("class", "?"),
                "key": obj_data.get("key", "?"),
                "fields": obj_data.get("fields", {}),
            }
        )
    return out


def format_objects(result: dict) -> str:
    """Format iTop response objects into readable string.

    For each object:
    - If a 'ref' field is present, it is used as the header label instead of
      the numeric key, and 'ref' is suppressed from the field list.
    - 'id' is always suppressed when 'ref' is present (redundant).
    - A 'link' line is injected using ITOP_URL + class + numeric key, giving
      the LLM a direct URL to the object in the iTop UI.
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
        # Use ref as the header label when available
        ref = fields.get("ref")
        label = ref if ref else oid
        lines.append(f"\n--- {cls}::{label} ---")
        # Inject direct link to the iTop UI object page
        if ITOP_URL and oid:
            lines.append(
                f"  link: {ITOP_URL}/pages/UI.php?operation=details&class={cls}&id={oid}"
            )
        for fn, fv in fields.items():
            # ref is already in the header; id is redundant when ref is present
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
