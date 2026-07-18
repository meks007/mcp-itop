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
key_cache.cleanup()                   -> int

# Token validation (TTL = TOKEN_CACHE_TTL, sliding window)
# NOTE: callers are responsible for hashing the raw token before calling.
#       auth.get_bearer_token_hash() is the single place that does this.
await token_cache.validate(token_hash, probe_fn)  -> bool
await token_cache.evict_by_token(token_hash)      -> None
await token_cache.evict_stale()                   -> int

Backward-compatible aliases keep existing callers working unchanged:
  registry_add_entry, registry_get_meta, registry_set_meta,
  registry_get_fields, seed_field_cache,
  cache_get, cache_set, cache_cleanup
"""

from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Generic, Iterator, TypeVar

from config import RESOLVE_KEY_CACHE_TTL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type variables
# ---------------------------------------------------------------------------

K = TypeVar("K")
V = TypeVar("V")

# ---------------------------------------------------------------------------
# Base class: Cache[K, V]
# ---------------------------------------------------------------------------


class Cache(Generic[K, V]):
    """Abstract base for all caches.

    Concrete subclasses store entries in a plain dict and may add eviction.
    """

    def get(self, key: K) -> V | None:  # noqa: D401
        """Return the cached value for key, or None on miss."""
        raise NotImplementedError

    def set(self, key: K, value: V) -> None:
        """Store value under key."""
        raise NotImplementedError

    def evict(self, key: K) -> None:
        """Remove the entry for key (no-op if not present)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Base class: TTLCache[K, V]
# ---------------------------------------------------------------------------


@dataclass
class _TTLEntry(Generic[V]):
    value: V
    ts: float  # time.monotonic() at insertion


class TTLCache(Cache[K, V]):
    """Cache with time-to-live eviction.

    Args:
        ttl:     Seconds before an entry expires.  ttl <= 0 disables caching.
        sliding: If True, every get() that hits an entry resets its clock.
                 If False (default), TTL is measured from insertion only.
    """

    def __init__(self, ttl: float, sliding: bool = False) -> None:
        self._ttl = ttl
        self._sliding = sliding
        self._store: dict[Any, _TTLEntry] = {}

    # ------------------------------------------------------------------

    def get(self, key: K) -> V | None:
        if self._ttl <= 0:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        now = time.monotonic()
        if now - entry.ts > self._ttl:
            del self._store[key]
            return None
        if self._sliding:
            entry.ts = now
        return entry.value

    def set(self, key: K, value: V) -> None:
        if self._ttl <= 0:
            return
        self._store[key] = _TTLEntry(value=value, ts=time.monotonic())

    def evict(self, key: K) -> None:
        self._store.pop(key, None)

    def cleanup(self) -> int:
        """Evict all expired entries. Returns the count removed."""
        if self._ttl <= 0:
            return 0
        now = time.monotonic()
        expired = [k for k, e in self._store.items() if now - e.ts > self._ttl]
        for k in expired:
            del self._store[k]
        if expired:
            logger.debug("[cache] cleanup: evicted %d expired entries", len(expired))
        return len(expired)


# ---------------------------------------------------------------------------
# ClassMetadataCache
# ---------------------------------------------------------------------------


@dataclass
class ClassEntry:
    exists: bool | None = None       # None = not yet probed
    fields: frozenset = field(default_factory=frozenset)
    meta: dict = field(default_factory=dict)


class ClassMetadataCache(Cache[str, ClassEntry]):
    """Stores per-iTop-class field inventories and arbitrary metadata.

    No TTL -- iTop class schemas do not change at runtime. Entries are
    created on first access and grow over the lifetime of the process.
    """

    def __init__(self) -> None:
        self._store: dict[str, ClassEntry] = {}

    # ------------------------------------------------------------------

    def get(self, key: str) -> ClassEntry | None:
        return self._store.get(key)

    def set(self, key: str, value: ClassEntry) -> None:
        self._store[key] = value

    def evict(self, key: str) -> None:
        self._store.pop(key, None)

    # ------------------------------------------------------------------
    # Domain helpers
    # ------------------------------------------------------------------

    def probe_entry(self, cls: str) -> ClassEntry:
        """Get-or-create the ClassEntry for cls.

        NOTE: must never call logger.debug() -- this method is invoked from
        within logging formatter paths (via beartype hooks) and any log call
        here causes infinite recursion.
        """
        if cls not in self._store:
            self._store[cls] = ClassEntry()
        return self._store[cls]

    def get_fields(self, cls: str) -> frozenset:
        """Return the known field set for cls (empty frozenset if not seeded)."""
        entry = self._store.get(cls)
        return entry.fields if entry is not None else frozenset()

    def seed(self, cls: str, fields: dict) -> None:
        """Grow the field set for cls from a live iTop response fields dict.

        Always unions new fields with existing ones -- never removes any.
        Sets exists=True as a side effect.

        NOTE: must never call logger.debug() -- same recursion risk as probe_entry.
        """
        entry = self.probe_entry(cls)
        if not fields:
            logger.warning("[class_cache] seed called for cls=%r with empty fields", cls)
            return
        incoming = frozenset(fields.keys())
        before_set = entry.fields
        new_fields = incoming - before_set
        entry.fields = before_set | incoming
        entry.exists = True
        if new_fields and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[class_cache] seed cls=%r +%d new fields (total=%d)",
                cls, len(new_fields), len(entry.fields),
            )

    def get_meta(self, cls: str, key: str, default: Any = None) -> Any:
        """Read arbitrary per-class metadata."""
        return self.probe_entry(cls).meta.get(key, default)

    def set_meta(self, cls: str, key: str, value: Any) -> None:
        """Write arbitrary per-class metadata."""
        self.probe_entry(cls).meta[key] = value


# Singleton
class_cache = ClassMetadataCache()

# ---------------------------------------------------------------------------
# KeyResolutionCache
# ---------------------------------------------------------------------------


@dataclass
class ResolvedKey:
    resolved_class: str
    resolved_id: int


class KeyResolutionCache(TTLCache[tuple, ResolvedKey]):
    """Maps (obj_class, ref) to (resolved_class, numeric_id).

    TTL is read from RESOLVE_KEY_CACHE_TTL (env var, default 86400 s).
    sliding=False: insertion time determines expiry, hits do not reset the clock.
    """

    pass


# Singleton
key_cache = KeyResolutionCache(ttl=RESOLVE_KEY_CACHE_TTL, sliding=False)

# ---------------------------------------------------------------------------
# TokenValidationCache
# ---------------------------------------------------------------------------

TOKEN_CACHE_TTL: float = 60.0  # seconds, sliding window


@dataclass
class TokenEntry:
    valid: bool
    last_seen: float


class TokenValidationCache(TTLCache[str, TokenEntry]):
    """Caches bearer token validation results with a sliding TTL.

    Key: pre-computed SHA-256 hex digest of the raw token. This class never
    sees or hashes raw tokens -- that responsibility belongs to auth.py via
    auth.get_bearer_token_hash().
    Value: TokenEntry with valid flag and last_seen timestamp.

    sliding=True: every cache hit resets the expiry window so an
    actively-used token is never re-validated until it goes idle for the
    full TTL duration.

    Per-key asyncio.Lock instances prevent duplicate iTop probe calls when
    multiple coroutines race to validate the same token simultaneously.
    """

    def __init__(self, ttl: float, sliding: bool = True) -> None:
        super().__init__(ttl=ttl, sliding=sliding)
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()

    # ------------------------------------------------------------------

    async def validate(self, token_hash: str, probe_fn) -> bool:
        """Validate a token by its pre-computed hash, using the cache to skip
        repeated probes.

        token_hash must be the SHA-256 hex digest of the raw bearer token,
        computed by auth.get_bearer_token_hash(). This method never hashes
        or inspects the raw token.

        probe_fn is an async callable with no arguments that returns bool.
        auth.py passes a closure over the raw iTop list_operations call.

        Flow:
          1. Fast path: non-expired entry found -- slide TTL, return valid.
          2. Slow path: acquire per-key lock, re-check, then call probe_fn.
        """
        # Fast path -- no lock needed for a plain dict read.
        entry = self.get(token_hash)
        if entry is not None:
            return entry.valid

        # Slow path: ensure per-key lock exists, then probe.
        async with self._lock_guard:
            if token_hash not in self._locks:
                self._locks[token_hash] = asyncio.Lock()
            token_lock = self._locks[token_hash]

        async with token_lock:
            # Re-check after acquiring the lock; another coroutine may have
            # already completed the probe while we were waiting.
            entry = self.get(token_hash)
            if entry is not None:
                return entry.valid

            try:
                valid = await probe_fn()
            except Exception:
                valid = False

            self.set(token_hash, TokenEntry(valid=valid, last_seen=time.monotonic()))
            logger.debug(
                "[token_cache] validated: valid=%s hash_prefix=%s", valid, token_hash[:8]
            )
            return valid

    async def evict_by_token(self, token_hash: str) -> None:
        """Remove the cache entry and its lock for the given token hash.

        token_hash must be the SHA-256 hex digest of the raw bearer token,
        computed by auth.get_bearer_token_hash().

        Called by auth.evict_token() whenever iTop returns code==1 (UNAUTH).
        Safe to call when the hash is not cached (no-op).
        """
        async with self._lock_guard:
            removed = self._store.pop(token_hash, None)
            self._locks.pop(token_hash, None)
        if removed is not None:
            logger.warning(
                "[token_cache] evicted (UNAUTH): hash_prefix=%s", token_hash[:8]
            )

    async def evict_stale(self) -> int:
        """Remove all token entries past their TTL. Returns count removed.

        Called periodically by housekeeping_loop() in background_tasks.py.
        Replaces evict_stale_token_cache() from auth.py.
        """
        if self._ttl <= 0:
            return 0
        now = time.monotonic()
        async with self._lock_guard:
            stale = [
                h for h, e in self._store.items()
                if now - e.ts > self._ttl
            ]
            for h in stale:
                self._store.pop(h, None)
                self._locks.pop(h, None)
        if stale:
            logger.debug(
                "[token_cache] evict_stale: removed %d stale entries", len(stale)
            )
        return len(stale)


# Singleton
token_cache = TokenValidationCache(ttl=TOKEN_CACHE_TTL, sliding=True)

# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------
# These names were the original public API of cache.py. Existing callers
# continue to work without modification until they are updated to use the
# class methods directly.

def registry_add_entry(cls: str) -> ClassEntry:
    return class_cache.probe_entry(cls)


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
