"""
helpers/html.py

HTML stripping utilities and inline image ref extraction.
All functions are pure (no iTop requests, no SQLite).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled regex patterns
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
# Triple-quoted raw strings are used to avoid quote-escaping issues.
_INLINE_IMG_RE = re.compile(
    r"""<img\b[^>]*\bdata-img-id=["']?(\d+)["']?"""
    r"""[^>]*\bdata-img-secret=["']?([0-9a-fA-F]+)["']?[^>]*>|"""
    r"""<img\b[^>]*\bdata-img-secret=["']?([0-9a-fA-F]+)["']?"""
    r"""[^>]*\bdata-img-id=["']?(\d+)["']?[^>]*>""",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# HTML decoding
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

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
# Inline image ref extraction
# ---------------------------------------------------------------------------

def parse_objects(result: dict) -> dict[str, list[dict]]:
    """Extract inline image refs from all string fields across all objects.

    Scans every string value in result['objects'] -- including strings nested
    inside dicts and lists (e.g. private_log.entries[].message_html) -- for
    <img> tags that carry both data-img-id and data-img-secret attributes.
    Returns a mapping of
    '{obj_class}::{obj_id}' -> [{'id': str, 'secret': str}, ...].

    Field values are NOT modified; stripping happens in formatters.py.
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
        # are scanned without recursion overhead.
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
