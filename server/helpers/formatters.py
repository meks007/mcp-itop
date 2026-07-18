"""
helpers/formatters.py - iTop response formatters and output helpers.

Pure formatting: no iTop REST requests, no SQLite writes (the SQLite write
in format_and_cache is a transparent side effect via a deferred import of
attachment_store to avoid circular imports).
"""

from __future__ import annotations

import json
import logging

from config import ITOP_URL
from cache import seed_field_cache
from helpers.html import _strip_html, strip_html_recursive, parse_objects
from helpers.utils import str_or

logger = logging.getLogger(__name__)


def extract_objects(result: dict) -> list[dict]:
    """Extract list of {class, key, fields} from an iTop response dict."""
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


def _is_empty(fv) -> bool:
    """Return True only for values that carry no information.

    None, empty string, empty dict, and empty list are considered empty.
    Integers, floats, and booleans (including 0 and False) are never empty.
    """
    if fv is None:
        return True
    if isinstance(fv, str):
        return not fv
    if isinstance(fv, (dict, list)):
        return not fv
    return False


def _format_objects(result: dict, *, strip_empty: bool = True) -> tuple[str, dict[str, list[dict]]]:
    """Format iTop response objects into a readable string and extract inline image refs.

    Internal implementation -- call format_and_cache() from tool code instead.

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

    When strip_empty=True (default), fields whose value is None, an empty
    string (including strings that were HTML-only and stripped to nothing),
    an empty dict, or an empty list are omitted from the output. Integers,
    floats, and booleans (including 0 and False) are never omitted.

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
                "  link: " + ITOP_URL
                + "/pages/UI.php?operation=details&class=" + cls + "&id=" + oid
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
                if strip_empty and _is_empty(fv):
                    continue
                fv = json.dumps(fv, indent=2, ensure_ascii=False)
            if strip_empty and _is_empty(fv):
                continue
            lines.append("  " + fn + ": " + str(fv))
        for fn, fv in synthetic.items():
            display_name = fn.lstrip("_")
            lines.append("  [" + display_name + "] " + str(fv))
    return "\n".join(lines), refs


def format_objects(result: dict, *, strip_empty: bool = True) -> tuple[str, dict[str, list[dict]]]:
    """Public alias for _format_objects. Kept for external callers."""
    return _format_objects(result, strip_empty=strip_empty)


def format_and_cache(result: dict, *, strip_empty: bool = True) -> str:
    """Format iTop response and persist inline image refs to SQLite.

    Calls _format_objects() to get the formatted text and the inline image
    refs map, then writes each ticket's refs to the attachment_store cache
    via write_inline_image_refs().

    When strip_empty=True (default), fields with no meaningful value
    (None, empty string, empty dict, empty list) are omitted from output.
    Integers, floats, and booleans (including 0 and False) are never omitted.

    The deferred import of attachment_store avoids a circular import:
      helpers -> attachment_store -> config  (safe)
      attachment_store must NOT import helpers at module level.
    """
    from attachment_store import write_inline_image_refs

    text, refs = _format_objects(result, strip_empty=strip_empty)

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
    """Simple aligned plain-text table formatter."""
    if not rows:
        return "(no data)"
    col_widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))
    lines = []
    lines.append(" | ".join(h.ljust(w) for h, w in zip(header, col_widths)))
    lines.append("-+-".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(" | ".join(c.ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


def format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
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
