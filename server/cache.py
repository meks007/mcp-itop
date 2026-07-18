"""
Process-level caches for iTop class metadata and resolve_key lookups.

Two independent caches:

1. _ITOP_CLASS_REGISTRY
   Field inventories and arbitrary metadata per iTop class.
   Seeded passively from every core/get response via seed_field_cache().
   Never expires. No active pre-heat -- cache warms lazily on first use.

2. _RESOLVE_KEY_CACHE
   (obj_class, ref) -> (resolved_class, numeric_id) mappings.
   TTL-based eviction; lazy cleanup on every resolve_key call.
   TTL is read from RESOLVE_KEY_CACHE_TTL (env, default 86400 s).

Public API
----------
# Class registry
registry_get_meta(cls, key, default)
registry_set_meta(cls, key, value)
registry_get_fields(cls)
seed_field_cache(cls, fields)

# Field helper  (uses get_client() internally -- no itop_request_fn param)
get_class_fields(cls)

# resolve_key cache
cache_get(obj_class, ref)
cache_set(obj_class, ref, cls, id)
cache_cleanup()
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
        # NOTE: no logger.debug() here -- this is called from within logging
        # formatter paths (via beartype hooks) and any log call here causes
        # infinite recursion.
        _ITOP_CLASS_REGISTRY[cls] = {"exists": None, "fields": frozenset(), "meta": {}}
    return _ITOP_CLASS_REGISTRY[cls]


def registry_get_meta(cls: str, key: str, default: Any = None) -> Any:
    """Read arbitrary per-class metadata from the registry."""
    return _registry_entry(cls)["meta"].get(key, default)


def registry_set_meta(cls: str, key: str, value: Any) -> None:
    """Write arbitrary per-class metadata into the registry."""
    _registry_entry(cls)["meta"][key] = value


def registry_get_fields(cls: str) -> frozenset:
    """Return the known field inventory for a class (may be empty)."""
    return _registry_entry(cls)["fields"]


def seed_field_cache(cls: str, fields: dict) -> None:
    """Grow the field inventory for a class from a live response fields dict.

    Already-known fields are always kept (union, never removed).
    Called automatically from apply_field_strip() and ensure_class_exists()
    so the cache fills passively on every iTop response.
    """
    entry = _registry_entry(cls)
    if not fields:
        logger.warning("[registry] seed_field_cache cls=%r seed empty fields", cls)
        return
    incoming = frozenset(fields.keys())
    before_set = entry["fields"]
    new_fields = incoming - before_set
    entry["fields"] = before_set | incoming
    entry["exists"] = True
    if new_fields and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[registry] seed_field_cache cls=%r +%d new fields (total=%d)",
            cls, len(new_fields), len(entry["fields"]),
        )


# ---------------------------------------------------------------------------
# Field helper
# ---------------------------------------------------------------------------

async def get_class_fields(obj_class: str) -> frozenset[str]:
    """Return the field inventory for obj_class.

    Uses the ItopClient from the current async context (get_client()).
    Returns the cached frozenset immediately when warm.
    Marks the class as non-existent (exists=False) on probe failure so
    subsequent calls skip the round-trip.
    """
    from client import get_client

    entry = _registry_entry(obj_class)

    if entry["fields"]:
        return entry["fields"]

    if entry["exists"] is False:
        return frozenset()

    logger.debug("[get_class_fields] cls=%r cache cold, probing iTop", obj_class)
    client = get_client()
    result = await client.get(
        obj_class,
        "SELECT " + obj_class,
        fields="*",
        limit=1,
    )
    if result.get("code", -1) != 0:
        logger.debug(
            "[get_class_fields] cls=%r probe failed code=%r msg=%r",
            obj_class, result.get("code"), result.get("message"),
        )
        entry["exists"] = False
        return frozenset()

    objects = result.get("objects") or {}
    if not objects:
        logger.debug("[get_class_fields] cls=%r probe returned no objects", obj_class)
        entry["exists"] = False
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
