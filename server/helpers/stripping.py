"""
helpers/stripping.py - Field stripping for iTop REST responses.

Owns _LEAN_STRIP and apply_field_strip so that client.py can import
them without creating a circular dependency with helpers/resolvers.py.
"""

from __future__ import annotations

import logging

from cache import seed_field_cache

logger = logging.getLogger(__name__)

# Fields stripped from every core/get response when full=False.
# Add field names here to hide them from callers by default.
_LEAN_STRIP: frozenset[str] = frozenset({"private_log"})


def apply_field_strip(result: dict, strip: frozenset[str]) -> dict:
    """Remove strip fields from every object in an iTop result dict.

    Mutates result in place and returns it for convenience.
    Seeds the field cache as a side effect so that resolve_output_fields
    can use warm-cache paths on subsequent calls for the same class.
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
        seed_field_cache(cls, fields)
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
                cls, sorted(strip),
            )
    return result
