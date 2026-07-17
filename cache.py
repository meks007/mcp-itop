"""
Process-level caches for iTop class metadata and resolve_key lookups.

Two independent caches live here:

1. _ITOP_CLASS_REGISTRY
   Stores field inventories and arbitrary metadata per iTop class.
   Seeded passively from every core/get response that passes through
   format_objects() or apply_field_strip(). Never expires -- iTop class
   schemas do not change at runtime.

2. _RESOLVE_KEY_CACHE
   Stores (obj_class, ref) -> (resolved_class, numeric_id) mappings.
   TTL-based eviction; lazy cleanup on every resolve_key call.
   TTL is read from RESOLVE_KEY_CACHE_TTL (env, default 86400 s).

Public API
----------
# Class registry
registry_get_meta(cls, key, default)    read per-class metadata
registry_set_meta(cls, key, value)      write per-class metadata
registry_get_fields(cls)                frozenset of known field names
seed_field_cache(cls, fields)           grow field inventory from response

# Field helper
get_class_fields(cls, itop_request)     warm-or-probe field inventory

# resolve_key cache
cache_get(obj_class, ref)               -> (resolved_class, id) | None
cache_set(obj_class, ref, cls, id)      store resolved pair
cache_cleanup()                         evict expired entries

# Pre-heat
preheat(itop_request)                   probe all CLASSES_WITH_REF
preheat_once(itop_request)              preheat if any class cache still cold
"""

from __future__ import annotations

import time
import logging
from typing import Any

from config import RESOLVE_KEY_CACHE_TTL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Class registry
# ---------------------------------------------------------------------------
# Per-entry shape:
#   {
#     "exists": bool | None,     # None = not yet probed
#     "fields": frozenset[str],  # known field names, grown from live responses
#     "meta":   dict,            # arbitrary per-class metadata
#   }

_ITOP_CLASS_REGISTRY: dict[str, dict] = {}


def _registry_entry(cls: str) -> dict:
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


def seed_field_cache(cls: str, fields: dict) -> None:
    """Grow the field inventory for a class from a live response fields dict.

    Already-known fields are always kept (union, never removed).
    Only genuinely new fields (not yet in the registry) are added and logged.
    """
    entry = _registry_entry(cls)
    if not fields:
        logger.warn(
            "[registry] seed_field_cache cls=%r seed empty fields",
            cls
          )
        return
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


# ---------------------------------------------------------------------------
# Field helper
# ---------------------------------------------------------------------------

async def get_class_fields(obj_class: str, itop_request) -> frozenset[str]:
    """Return the field inventory for obj_class.

    If the registry cache is warm for obj_class, returns the cached frozenset
    immediately without any iTop request.

    If the cache is cold, fires a single probe request (core/get, output_fields=*,
    limit=1) to seed the cache, then returns the resulting frozenset.
    An empty frozenset is returned when the class has no instances or does not
    exist on the iTop server.
    """
    entry = _registry_entry(obj_class)
    if entry["fields"]:
        logger.debug(
            "[get_class_fields] cls=%r cache warm, %d fields",
            obj_class, len(entry["fields"]),
        )
        return entry["fields"]

    logger.debug("[get_class_fields] cls=%r cache cold, probing iTop", obj_class)
    result = await itop_request({
        "operation": "core/get",
        "class": obj_class,
        "key": "SELECT " + obj_class,
        "output_fields": "*",
        "limit": "1",
    })
    if result.get("code", -1) != 0:
        logger.debug(
            "[get_class_fields] cls=%r probe failed code=%r msg=%r",
            obj_class, result.get("code"), result.get("message"),
        )
        seed_field_cache(obj_class, {})
        return frozenset()

    objects = result.get("objects") or {}
    if not objects:
        logger.debug("[get_class_fields] cls=%r probe returned no objects", obj_class)
        seed_field_cache(obj_class, {})
        return frozenset()

    for obj_data in objects.values():
        fields = obj_data.get("fields") or {}
        seed_field_cache(obj_class, fields)
        break

    return entry["fields"]


# ---------------------------------------------------------------------------
# resolve_key cache
# ---------------------------------------------------------------------------
# Cache shape: { (obj_class, ref): {"class": str, "id": int, "ts": float} }

_RESOLVE_KEY_CACHE: dict[tuple[str, str], dict] = {}


def cache_cleanup() -> None:
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


def cache_get(obj_class: str, ref: str) -> tuple[str, int] | None:
    """Return (resolved_class, resolved_id) from cache, or None on miss/expiry."""
    if RESOLVE_KEY_CACHE_TTL <= 0:
        return None
    entry = _RESOLVE_KEY_CACHE.get((obj_class, ref))
    if entry is None:
        return None
    if time.monotonic() - entry["ts"] > RESOLVE_KEY_CACHE_TTL:
        del _RESOLVE_KEY_CACHE[(obj_class, ref)]
        logger.debug(
            "[resolve_key_cache] expired entry for class=%r ref=%r", obj_class, ref
        )
        return None
    logger.debug(
        "[resolve_key_cache] hit class=%r ref=%r -> resolved_class=%r id=%r",
        obj_class, ref, entry["class"], entry["id"],
    )
    return entry["class"], entry["id"]


def cache_set(obj_class: str, ref: str, resolved_class: str, resolved_id: int) -> None:
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
# Pre-heat
# ---------------------------------------------------------------------------

async def preheat(itop_request) -> None:
    """Probe all CLASSES_WITH_REF to warm the field cache.

    Each class gets a single core/get (output_fields=*, limit=1). Errors and
    empty classes are silently ignored -- the cache stays cold for those and
    will be warmed on the first real request.
    """
    # Import here to avoid circular import (helpers imports cache).
    from helpers import CLASSES_WITH_REF

    logger.info("[cache] pre-heating field cache for %d classes", len(CLASSES_WITH_REF))
    for cls in sorted(CLASSES_WITH_REF):
        fields = await get_class_fields(cls, itop_request)
        logger.info(
            "[cache] preheat cls=%r -> %d fields cached",
            cls, len(fields),
        )
    logger.info("[cache] pre-heat complete")


async def preheat_once(itop_request) -> None:
    """Run preheat only if any CLASSES_WITH_REF field cache is still cold.

    Called at the start of the first real iTop request so that a bearer token
    is guaranteed to be available. Subsequent calls are no-ops once all
    classes are warm.
    """
    from helpers import CLASSES_WITH_REF

    if all(_registry_entry(cls)["fields"] for cls in CLASSES_WITH_REF):
        return
    await preheat(itop_request)
