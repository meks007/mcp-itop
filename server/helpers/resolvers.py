"""
helpers/resolvers.py - iTop-aware ref and class resolution helpers.

All functions use get_client() from the client module to reach the
ItopClient bound to the current async context. No itop_request_fn
parameter is accepted or forwarded anywhere.
"""

from __future__ import annotations

import logging
from typing import Any

from config import RESOLVE_KEY_CACHE_TTL
from cache import (
    cache_cleanup,
    cache_get,
    cache_set,
    get_class_fields,
    registry_get_fields,
    seed_field_cache,
    _registry_entry,
)
from helpers.utils import (
    CLASSES_WITH_REF,
    _SYNTHETIC_FIELDS,
    str_or,
    is_bare_number,
)

logger = logging.getLogger(__name__)


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


async def ensure_class_exists(candidates: list[str]) -> str:
    """Return the first class in candidates that exists on the iTop server.

    Uses get_client() from the current async context.
    """
    from client import get_client
    client = get_client()

    for cls in candidates:
        entry = _registry_entry(cls)
        if entry["exists"] is True:
            logger.debug("[registry] ensure_class_exists cls=%r -> cached True", cls)
            return cls
        if entry["exists"] is False:
            logger.debug("[registry] ensure_class_exists cls=%r -> cached False, skip", cls)
            continue
        r = await client.get(cls, "SELECT " + cls, fields="id", limit=1)
        if r.get("code") == 0:
            entry["exists"] = True
            for obj_data in (r.get("objects") or {}).values():
                seed_field_cache(cls, obj_data.get("fields") or {})
            logger.debug(
                "[registry] ensure_class_exists cls=%r -> exists=True (probed)", cls
            )
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
) -> tuple[str, frozenset[str]]:
    """Resolve (output_fields, strip) into (fields_to_request, post_strip_set).

    Uses get_class_fields() which internally calls get_client().
    """
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
                "[resolve_output_fields] cls=%r warm cache but strip removed all fields,"
                " fallback to wildcard",
                obj_class,
            )
            return output_fields, strip
        result_fields = ", ".join(explicit)
        logger.debug(
            "[resolve_output_fields] cls=%r WARM cache hit, explicit fields=%r"
            " post_strip=empty",
            obj_class, result_fields,
        )
        return result_fields, frozenset()

    logger.debug(
        "[resolve_output_fields] cls=%r COLD cache miss, using wildcard=%r with"
        " post_strip=%r",
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


async def resolve_ref_class_by_ref_part(
    obj_class: str,
    key: str,
) -> tuple[str, int, str] | tuple[None, None, None]:
    """Resolve a ref or bare number to (resolved_class, numeric_id, ref_string).

    Uses get_client() from the current async context.
    Returns (None, None, None) when no matching object is found.
    """
    from client import get_client
    client = get_client()

    suffix = str(key).zfill(6)
    oql = "SELECT " + obj_class + " WHERE ref LIKE '%" + suffix + "'"
    logger.debug(
        "[resolve_ref_class_by_ref_part] key=%r suffix=%r oql=%r", key, suffix, oql
    )
    result = await client.request({
        "operation": "core/get",
        "class": obj_class,
        "key": oql,
        "output_fields": "id,ref",
        "limit": "1",
    })
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
) -> tuple[str, Any]:
    """Resolve an object identifier to (resolved_class, numeric_key).

    Uses get_client() from the current async context.
    For CLASSES_WITH_REF: ref matched via suffix OQL on the ref field.
    For all other classes: ref passed directly as key in a core/get call.
    Fallback: int(ref) or raw ref string.
    """
    from client import get_client
    client = get_client()

    cache_cleanup()

    ref_str = str(ref).strip() if ref is not None else ""
    if not ref_str:
        return obj_class, ref

    cached = cache_get(obj_class, ref_str)
    if cached is not None:
        return cached[0], cached[1]

    if obj_class in CLASSES_WITH_REF:
        found_class, found_id, found_ref = await resolve_ref_class_by_ref_part(
            obj_class, ref_str
        )
        if found_class is not None and found_id is not None:
            logger.debug(
                "[resolve_key] ref=%r -> class=%r key=%r ref=%r",
                ref_str, found_class, found_id, found_ref,
            )
            cache_set(obj_class, ref_str, found_class, found_id)
            return found_class, found_id
    else:
        result = await client.request({
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


async def fetch_image_counts(
    obj_class: str,
    obj_id: str | int,
) -> tuple[int, int]:
    """Return (attachment_count, inline_image_count) for a ticket object.

    Uses get_client() from the current async context.
    """
    from client import get_client
    client = get_client()

    oid = str(obj_id)

    att_result = await client.request({
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

    ii_result = await client.request({
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
