"""
Pure utility / formatting helpers shared across all tool modules.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple


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
# When output_fields is an explicit list and the class is known to have ref,
# we inject "ref" automatically so callers always get the ticket reference.
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


def ensure_ref_field(obj_class: str, output_fields: str) -> str:
    """Inject 'ref' into output_fields when the class supports it.

    Only modifies output_fields when:
    - obj_class is in CLASSES_WITH_REF, and
    - output_fields is an explicit field list (not '*' or '*+'), and
    - 'ref' is not already present.

    Returns the (possibly modified) output_fields string.
    """
    if output_fields in ("*", "*+"):
        return output_fields
    if obj_class not in CLASSES_WITH_REF:
        return output_fields
    fields = [f.strip() for f in output_fields.split(",")]
    if "ref" not in fields:
        fields.insert(0, "ref")
    return ", ".join(fields)


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
    """Format iTop response objects into readable string."""
    if result.get("code", -1) != 0:
        return f"Error (code {result.get('code')}): {str_or(result, 'message', 'Unknown error')}"
    objects = result.get("objects")
    if not objects:
        return str_or(result, "message", "No objects found.")
    lines = [str_or(result, "message", "")]
    for _obj_key, obj_data in objects.items():
        cls = str_or(obj_data, "class", "?")
        oid = str_or(obj_data, "key", "?")
        fields = obj_data.get("fields", {})
        lines.append(f"\n--- {cls}::{oid} ---")
        for fn, fv in fields.items():
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
