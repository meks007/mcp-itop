"""
helpers/utils.py

Generic, stateless utility functions and shared constants.
No iTop requests, no SQLite, no HTML parsing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

# ---------------------------------------------------------------------------
# Ticket class constants
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


# ---------------------------------------------------------------------------
# Type tests
# ---------------------------------------------------------------------------

def is_bare_number(key: Any) -> bool:
    """Return True if key is a bare integer or a string of only digits."""
    if isinstance(key, int):
        return True
    if isinstance(key, str) and _BARE_NUMBER_PATTERN.match(key):
        return True
    return False


# ---------------------------------------------------------------------------
# Ref coercion
# ---------------------------------------------------------------------------

def coerce_ref(ticket_ref: str, key: Any) -> str | None:
    """Merge ticket_ref and key into a single identifier string, or None.

    Most write tools accept an optional ticket_ref AND an optional key.
    This helper consolidates the repeated pattern:
        ref = str(ticket_ref or key or "").strip() or None
    so each tool no longer repeats it inline.

    Args:
        ticket_ref: Preferred human-readable ticket ref, e.g. 'R-016292'.
        key:        Fallback key: numeric ID, OQL string, or empty string.

    Returns:
        Stripped non-empty string, or None when both inputs are falsy.
    """
    result = str(ticket_ref or key or "").strip()
    return result if result else None


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def str_or(d: dict, key: str, default: str = "") -> str:
    """Return str(d[key]) or default when key is missing or None."""
    v = d.get(key)
    return str(v) if v is not None else default


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def _try_json_parse(raw: str):
    """Attempt to parse raw as JSON.

    Returns (parsed_value, None) on success,
            (None, JSONDecodeError)  on failure.
    Shared by parse_key() and parse_json_arg() to avoid duplicate try/except.
    """
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, e


def parse_key(key: str) -> Any:
    """Parse a key string to the most specific Python type.

    Tries JSON first, then int, then returns the raw string unchanged.
    Used for OQL keys that may be integers, dicts, or plain strings.
    """
    parsed, _ = _try_json_parse(key)
    if parsed is not None:
        return parsed
    try:
        return int(key)
    except (ValueError, TypeError):
        return key


def parse_json_arg(raw: str, arg_name: str) -> dict | str:
    """Parse a JSON string argument from a tool call.

    Returns the parsed dict on success, or a human-readable error string
    on failure so the tool can return it directly to the MCP client.
    """
    parsed, err = _try_json_parse(raw)
    if err is not None:
        return f"Invalid JSON in '{arg_name}': {err.msg} at position {err.pos}"
    return parsed


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_date_range(start: str, end: str) -> Tuple[str, str]:
    """Normalize date strings; return (start, end) in iTop datetime format.

    Defaults: start = 30 days ago, end = now (UTC).
    Raises ValueError on unparseable input.
    """
    try:
        dt_start = (
            datetime.fromisoformat(start)
            if start
            else (datetime.now(timezone.utc) - timedelta(days=30))
        )
        dt_end = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    except ValueError:
        raise ValueError(
            "Invalid date format. Use ISO 8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
        )
    return dt_start.strftime("%Y-%m-%d %H:%M:%S"), dt_end.strftime("%Y-%m-%d %H:%M:%S")
