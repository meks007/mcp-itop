"""
cache.py - Process-level caches for mcp-itop.

Three caches are provided, all backed by a small generic class hierarchy:

  Cache[K, V]             -- abstract base: get/set/evict
  TTLCache[K, V]          -- adds time-based eviction and cleanup()

Concrete singletons (module-level, one per concern):

  class_cache   : ClassMetadataCache       -- iTop class field inventories
  key_cache     : KeyResolutionCache       -- resolved (class, id) per ref
  token_cache   : TokenValidationCache     -- bearer token validity results

Public API
----------
# Class metadata
class_cache.probe_entry(cls)          -> ClassEntry
class_cache.get_fields(cls)           -> frozenset[str]
class_cache.seed(cls, fields)         -> None
class_cache.get_meta(cls, key, ...)   -> Any
class_cache.set_meta(cls, key, value) -> None

# Key resolution (TTL from RESOLVE_KEY_CACHE_TTL env var)
key_cache.get((cls, ref))             -> ResolvedKey | None
key_cache.set((cls, ref), value)      -> None
key_cache.cleanup()                   -> int   # called by housekeeping loop

# Token validation (TTL from TOKEN_CACHE_TTL env var, sliding window)
# NOTE: callers are responsible for hashing the raw token before calling.
token_cache.get(token_hash)           -> bool | None
token_cache.set(token_hash, valid)    -> None
token_cache.evict(token_hash)         -> None

# Transition map (TTL: TRANSITION_CACHE_TTL seconds, default 3600)
get_transition_map(cls, client)       -> dict   (async, caches per obj_class)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from config import logger

K = TypeVar("K")
V = TypeVar("V")


# ---------------------------------------------------------------------------
# Generic cache base classes
# ---------------------------------------------------------------------------

class Cache(ABC, Generic[K, V]):
    """Abstract base: get / set / evict."""

    @abstractmethod
    def get(self, key: K) -> V | None: ...

    @abstractmethod
    def set(self, key: K, value: V) -> None: ...

    @abstractmethod
    def evict(self, key: K) -> None: ...


@dataclass
class _TTLEntry(Generic[V]):
    value: V
    expires_at: float


class TTLCache(Cache[K, V]):
    """Cache with per-entry TTL and a sweep-based cleanup()."""

    def __init__(self, ttl: float, name: str = "") -> None:
        self._ttl = ttl
        self._name = name
        self._store: dict[K, _TTLEntry[V]] = {}

    def get(self, key: K) -> V | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._store[key]
            return None
        return entry.value

    def set(self, key: K, value: V) -> None:
        self._store[key] = _TTLEntry(value=value, expires_at=time.monotonic() + self._ttl)

    def evict(self, key: K) -> None:
        self._store.pop(key, None)

    def cleanup(self) -> int:
        """Remove all expired entries. Returns the number of entries removed."""
        now = time.monotonic()
        expired = [k for k, e in self._store.items() if now > e.expires_at]
        for k in expired:
            del self._store[k]
        if expired:
            logger.debug("[%s] cache cleanup: removed %d expired entries", self._name, len(expired))
        return len(expired)


# ---------------------------------------------------------------------------
# Class metadata cache
# ---------------------------------------------------------------------------

@dataclass
class ClassEntry:
    """Mutable metadata record for a single iTop class."""
    fields: frozenset[str] = field(default_factory=frozenset)
    exists: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ClassMetadataCache:
    """Persistent (no TTL) cache of iTop class field inventories and metadata."""

    def __init__(self) -> None:
        self._store: dict[str, ClassEntry] = {}

    def probe_entry(self, cls: str) -> ClassEntry:
        """Return the ClassEntry for cls, creating one if absent."""
        if cls not in self._store:
            self._store[cls] = ClassEntry()
        return self._store[cls]

    def get_fields(self, cls: str) -> frozenset[str]:
        return self._store.get(cls, ClassEntry()).fields

    def seed(self, cls: str, fields: dict) -> None:
        """Populate the field inventory from a core/get response fields dict."""
        entry = self.probe_entry(cls)
        entry.fields = frozenset(fields.keys())
        entry.exists = True

    def get_meta(self, cls: str, key: str, default: Any = None) -> Any:
        return self._store.get(cls, ClassEntry()).meta.get(key, default)

    def set_meta(self, cls: str, key: str, value: Any) -> None:
        self.probe_entry(cls).meta[key] = value


# ---------------------------------------------------------------------------
# Key resolution cache
# ---------------------------------------------------------------------------

@dataclass
class ResolvedKey:
    resolved_class: str
    resolved_id: int


class KeyResolutionCache(TTLCache[tuple[str, str], ResolvedKey]):
    """TTL cache mapping (obj_class, ref) -> ResolvedKey."""


# ---------------------------------------------------------------------------
# Token validation cache
# ---------------------------------------------------------------------------

class TokenValidationCache(TTLCache[str, bool]):
    """Sliding-window TTL cache for bearer token validity.

    set() always resets the TTL to the full window so that active tokens
    are not evicted mid-session.
    """


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

import os as _os

_RESOLVE_KEY_CACHE_TTL = float(_os.environ.get("RESOLVE_KEY_CACHE_TTL", "300"))
_TOKEN_CACHE_TTL       = float(_os.environ.get("TOKEN_CACHE_TTL",        "300"))

class_cache  = ClassMetadataCache()
key_cache    = KeyResolutionCache(ttl=_RESOLVE_KEY_CACHE_TTL,  name="key_cache")
token_cache  = TokenValidationCache(ttl=_TOKEN_CACHE_TTL, name="token_cache")


# ---------------------------------------------------------------------------
# Convenience wrappers used by helpers and tools
# ---------------------------------------------------------------------------

def registry_get_meta(cls: str, key: str, default: Any = None) -> Any:
    return class_cache.get_meta(cls, key, default)


def registry_set_meta(cls: str, key: str, value: Any) -> None:
    class_cache.set_meta(cls, key, value)


def registry_get_fields(cls: str) -> frozenset:
    return class_cache.get_fields(cls)


def seed_field_cache(cls: str, fields: dict) -> None:
    class_cache.seed(cls, fields)


def cache_get(obj_class: str, ref: str) -> tuple[str, int] | None:
    result = key_cache.get((obj_class, ref))
    if result is None:
        return None
    return result.resolved_class, result.resolved_id


def cache_set(obj_class: str, ref: str, resolved_class: str, resolved_id: int) -> None:
    key_cache.set((obj_class, ref), ResolvedKey(resolved_class, resolved_id))


def cache_cleanup() -> None:
    key_cache.cleanup()


# ---------------------------------------------------------------------------
# Transition map cache
# ---------------------------------------------------------------------------
#
# Stores the full enumerate_transitions schema per iTop class.
# TTL defaults to 3600 s (1 h) -- workflows change rarely.
# Key:   obj_class string, e.g. "UserRequest"
# Value: dict with keys "fields" and "transitions"

_TRANSITION_CACHE_TTL = float(_os.environ.get("TRANSITION_CACHE_TTL", "3600"))
_transition_cache: TTLCache[str, dict] = TTLCache(
    ttl=_TRANSITION_CACHE_TTL, name="transition_cache"
)


async def get_transition_map(obj_class: str, client) -> dict:
    """Return the cached transition map for obj_class, fetching on cache miss.

    The schema is fetched via client.enumerate_transitions() and cached for
    TRANSITION_CACHE_TTL seconds (default 3600). Subsequent calls within the
    TTL window return the cached dict without an iTop round-trip.

    Args:
        obj_class: iTop class name, e.g. "UserRequest".
        client:    ItopClient instance -- used only on cache miss.

    Returns:
        dict with top-level keys "fields" and "transitions".
    """
    cached = _transition_cache.get(obj_class)
    if cached is not None:
        logger.debug("[transition_cache] hit for cls=%r", obj_class)
        return cached

    logger.debug("[transition_cache] miss for cls=%r -- fetching from iTop", obj_class)
    data = await client.enumerate_transitions(obj_class)
    _transition_cache.set(obj_class, data)
    return data
