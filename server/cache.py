"""
Process-level caches for iTop class metadata and resolve_key lookups.

Two independent caches:

1. Class metadata cache (_ITOP_CLASS_REGISTRY)
   Stores field inventories and arbitrary metadata per iTop class.
   Seeded passively from every core/get response via seed_field_cache().
   Never expires. No active pre-heat -- cache warms lazily on first use.

2. Key resolution cache (_RESOLVE_KEY_CACHE)
   Maps (obj_class, ref) to (resolved_class, numeric_id).
   TTL-based eviction; lazy cleanup on every resolve_key call.
   TTL is read from RESOLVE_KEY_CACHE_TTL (env, default 86400 s).

Public API
----------
# Class metadata cache
registry_add_entry(cls)           -- get-or-create entry for cls
registry_get_meta(cls, key, default)
registry_set_meta(cls, key, value)
registry_get_fields(cls)
seed_field_cache(cls, fields)

# Key resolution cache
cache_get(obj_class, ref)
cache_set(obj_class, ref, cls, id)
cache_cleanup()

Note: get_class_fields() has moved to ItopClient.get_class_fields() in
client.py. Use client.get_class_fields(cls) for policy-aware field
discovery that respects _LEAN_STRIP.
"""

from __future__ import annotations

import time
import logging
from typing import Any

from config import RESOLVE_KEY_CACHE_TTL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Class metadata cache
# ---------------------------------------------------------------------------
# Per-entry shape:
#   {
#     "exists": bool | None,     # None = not yet probed
#     "fields": frozenset[str],  # known field names, grown from live responses
#     "meta":   dict,            # arbitrary per-class metadata
#   }

_ITOP_CLASS_REGISTRY: dict[str, dict] = {}


def registry_add_entry(cls: str) -> dict:
    """Get-or-create the registry entry for cls.

    Returns the mutable entry dict so callers can read or write exists/fields/meta
    directly. Creates a blank entry on first call for a given class name.

    NOTE: must never call logger.debug() -- this function is invoked from within
    logging formatter paths (via beartype hooks) and any log call here causes
    infinite recursion.
    """
    if cls not in _ITOP_CLASS_REGISTRY:
        _ITOP_CLASS_REGISTRY[cls] = {"exists": None, "fields": frozenset(), "meta": {}}
    return _ITOP_CLASS_REGISTRY[cls]


def registry_get_meta(cls: str, key: str, default: Any = None) -> Any:
    """Read arbitrary per-class metadata from the class metadata cache."""
    return registry_add_entry(cls)["meta"].get(key, default)


def registry_set_meta(cls: str, key: str, value: Any) -> None:
    """Write arbitrary per-class metadata into the class metadata cache."""
    registry_add_entry(cls)["meta"][key] = value


def registry_get_fields(cls: str) -> frozenset:
    """Return the known field inventory for a class (may be empty frozenset)."""
    return registry_add_entry(cls)["fields"]


def seed_field_cache(cls: str, fields: dict) -> None:
    """Grow the field inventory for a class from a live response fields dict.

    Already-known fields are always kept (union, never removed).
    Sets exists=True as a side effect.
    Called automatically from apply_field_strip() and ensure_class_exists()
    so the cache fills passively on every iTop response.

    NOTE: must never call logger.debug() -- same recursion risk as registry_add_entry.
    """
    entry = registry_add_entry(cls)
    if not fields:
        logger.warning("[class_cache] seed_field_cache cls=%r called with empty fields", cls)
        return
    incoming = frozenset(fields.keys())
    before_set = entry["fields"]
    new_fields = incoming - before_set
    entry["fields"] = before_set | incoming
    entry["exists"] = True
    if new_fields and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[class_cache] seed_field_cache cls=%r +%d new fields (total=%d)",
            cls, len(new_fields), len(entry["fields"]),
        )


# ---------------------------------------------------------------------------
# Key resolution cache
# ---------------------------------------------------------------------------
# Cache shape: { (obj_class, ref): {"class": str, "id": int, "ts": float} }

_RESOLVE_KEY_CACHE: dict[tuple[str, str], dict] = {}


def cache_cleanup() -> None:
    """Remove all key resolution cache entries whose TTL has expired."""
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
        logger.debug("[key_cache] evicted %d expired entry/entries", len(expired))


def cache_get(obj_class: str, ref: str) -> tuple[str, int] | None:
    """Return (resolved_class, resolved_id) from the key cache, or None on miss/expiry."""
    if RESOLVE_KEY_CACHE_TTL <= 0:
        return None
    entry = _RESOLVE_KEY_CACHE.get((obj_class, ref))
    if entry is None:
        return None
    if time.monotonic() - entry["ts"] > RESOLVE_KEY_CACHE_TTL:
        del _RESOLVE_KEY_CACHE[(obj_class, ref)]
        logger.debug(
            "[key_cache] expired entry for class=%r ref=%r", obj_class, ref
        )
        return None
    return entry["class"], entry["id"]


def cache_set(obj_class: str, ref: str, resolved_class: str, resolved_id: int) -> None:
    """Store a resolved (class, id) pair in the key resolution cache."""
    if RESOLVE_KEY_CACHE_TTL <= 0:
        return
    _RESOLVE_KEY_CACHE[(obj_class, ref)] = {
        "class": resolved_class,
        "id": resolved_id,
        "ts": time.monotonic(),
    }
