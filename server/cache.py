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
class_cache.seed_schema(cls, schema)  -> None
class_cache.get_schema(cls)           -> dict | None
class_cache.get_meta(cls, key, ...)   -> Any
class_cache.set_meta(cls, key, value) -> None

# Key resolution (TTL from RESOLVE_KEY_CACHE_TTL env var)
key_cache.get((cls, ref))             -> ResolvedKey | None
key_cache.set((cls, ref), value)      -> None
key_cache.cleanup()                   -> int   # called by housekeeping loop

# Token validation (TTL from TOKEN_CACHE_TTL env var, sliding window)
# NOTE: callers are responsible for hashing the raw token before calling.
#       auth.get_bearer_token_hash() is the single place that does this.
await token_cache.validate(token_hash, probe_fn)  -> bool
await token_cache.evict_by_token(token_hash)      -> None
await token_cache.evict_stale()                   -> int

# Transition map (TTL from TRANSITION_CACHE_TTL env var, default 3600 s)
await get_transition_map(obj_class, client)       -> dict

# Class schema (company/describe_class result, lifetime of process)
await get_class_schema(obj_class, client)         -> dict

# Lifecycle state attribute discovery
find_lifecycle_state_attribute(fields)            -> str | None

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
from typing import Any, Generic, TypeVar

from config import RESOLVE_KEY_CACHE_TTL, TOKEN_CACHE_TTL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type variables
# ---------------------------------------------------------------------------

K = TypeVar("K")
V = TypeVar("V")

# ---------------------------------------------------------------------------
# Transition schema normalisation constants
# ---------------------------------------------------------------------------

# Only these three option flags are meaningful for MCP field prompting.
# Every other option (READONLY, HIDDEN, etc.) is stripped before caching
# so downstream tools never need to filter them again.
#
# The REST API is the single authority on which transitions are exposed.
# All transitions returned by REST are preserved in the cached schema
# regardless of whether they are StimulusUserAction or StimulusInternal.
REQUIRED_FIELD_OPTIONS: frozenset[str] = frozenset({
    "OPT_ATT_MANDATORY",
    "OPT_ATT_MUSTPROMPT",
    "OPT_ATT_MUSTCHANGE",
})


# ---------------------------------------------------------------------------
# Lifecycle state attribute discovery
# ---------------------------------------------------------------------------

def find_lifecycle_state_attribute(fields: dict) -> "str | None":
    """Return the name of the field marked is_lifecycle_state=true, or None.

    Both company/describe_class and company/enumerate_transitions return a
    top-level 'fields' dict. When a class has a lifecycle state machine,
    exactly one field in that dict carries is_lifecycle_state=true. All
    other fields omit the property entirely.

    Returns:
        The field name (e.g. 'status', 'lifecycle_phase') when exactly one
        field is marked, or None when no field is marked (no lifecycle).

    Raises:
        ValueError: when more than one field carries the marker. This
            indicates a broken class definition and must not be guessed
            around silently.
    """
    matches = [
        field_name
        for field_name, metadata in fields.items()
        if isinstance(metadata, dict) and metadata.get("is_lifecycle_state") is True
    ]

    if len(matches) > 1:
        raise ValueError(
            "Invalid class schema: multiple fields are marked "
            "is_lifecycle_state=true: " + ", ".join(sorted(matches))
        )

    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Base class: Cache[K, V]
# ---------------------------------------------------------------------------


class Cache(Generic[K, V]):
    """Abstract base for all caches.

    Concrete subclasses store entries in a plain dict and may add eviction.
    """

    def get(self, key: K) -> "V | None":  # noqa: D401
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
        name:    Optional name used in debug log messages.
    """

    def __init__(self, ttl: float, sliding: bool = False, name: str = "cache") -> None:
        self._ttl = ttl
        self._sliding = sliding
        self._name = name
        self._store: dict[Any, _TTLEntry] = {}

    # ------------------------------------------------------------------

    def get(self, key: K) -> "V | None":
        if self._ttl <= 0:
            return None
        entry = self._store.get(key)
        if entry is None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[%s] miss key=%r", self._name, key)
            return None
        now = time.monotonic()
        age = now - entry.ts
        if age > self._ttl:
            del self._store[key]
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[%s] miss (expired) key=%r", self._name, key)
            return None
        if self._sliding:
            entry.ts = now
            remaining = self._ttl
        else:
            remaining = self._ttl - age
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[%s] hit key=%r ttl_remaining=%.0fs",
                self._name, key, remaining,
            )
        return entry.value

    def set(self, key: K, value: V) -> None:
        if self._ttl <= 0:
            return
        self._store[key] = _TTLEntry(value=value, ts=time.monotonic())

    def evict(self, key: K) -> None:
        self._store.pop(key, None)

    def cleanup(self) -> int:
        """Evict all expired entries. Returns the count removed.

        Called by housekeeping_loop() in background_tasks.py; must NOT be
        called inline from resolve_key() or other hot paths.
        """
        if self._ttl <= 0:
            return 0
        now = time.monotonic()
        expired = [k for k, e in self._store.items() if now - e.ts > self._ttl]
        for k in expired:
            del self._store[k]
        if expired:
            logger.debug("[%s] cleanup: evicted %d expired entries", self._name, len(expired))
        return len(expired)


# ---------------------------------------------------------------------------
# ClassMetadataCache
# ---------------------------------------------------------------------------


@dataclass
class ClassEntry:
    exists: bool | None = None       # None = not yet probed
    fields: frozenset = field(default_factory=frozenset)
    schema: dict = field(default_factory=dict)  # raw describe_class payload
    meta: dict = field(default_factory=dict)


class ClassMetadataCache(Cache[str, ClassEntry]):
    """Stores per-iTop-class field inventories and arbitrary metadata.

    No TTL -- iTop class schemas do not change at runtime. Entries are
    created on first access and grow over the lifetime of the process.
    """

    def __init__(self) -> None:
        self._store: dict[str, ClassEntry] = {}

    # ------------------------------------------------------------------

    def get(self, key: str) -> "ClassEntry | None":
        entry = self._store.get(key)
        if logger.isEnabledFor(logging.DEBUG):
            if entry is not None:
                logger.debug(
                    "[class_cache] hit cls=%r fields=%d exists=%s",
                    key, len(entry.fields), entry.exists,
                )
            else:
                logger.debug("[class_cache] miss cls=%r", key)
        return entry

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
        if logger.isEnabledFor(logging.DEBUG):
            if entry is not None and entry.fields:
                logger.debug(
                    "[class_cache] get_fields hit cls=%r fields=%d",
                    cls, len(entry.fields),
                )
            else:
                logger.debug("[class_cache] get_fields miss cls=%r", cls)
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

    def seed_schema(self, cls: str, schema: dict) -> None:
        """Store the full describe_class field schema for cls.

        schema is the dict returned under the 'fields' key of the
        company/describe_class REST response.  The field name set is
        derived from the schema keys and unioned into entry.fields so
        that get_fields() stays consistent.

        NOTE: must never call logger.debug() -- same recursion risk as probe_entry.
        """
        entry = self.probe_entry(cls)
        if not schema:
            logger.warning("[class_cache] seed_schema called for cls=%r with empty schema", cls)
            return
        entry.schema = schema
        incoming = frozenset(schema.keys())
        entry.fields = entry.fields | incoming
        entry.exists = True
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[class_cache] seed_schema cls=%r fields=%d",
                cls, len(entry.fields),
            )

    def get_schema(self, cls: str) -> "dict | None":
        """Return the cached describe_class schema for cls, or None if not seeded."""
        entry = self._store.get(cls)
        if entry is None or not entry.schema:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[class_cache] get_schema miss cls=%r", cls)
            return None
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[class_cache] get_schema hit cls=%r fields=%d",
                cls, len(entry.schema),
            )
        return entry.schema

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

    cleanup() is called exclusively by housekeeping_loop() -- never inline.
    """

    pass


# Singleton
key_cache = KeyResolutionCache(ttl=RESOLVE_KEY_CACHE_TTL, sliding=False, name="key_cache")

# ---------------------------------------------------------------------------
# TokenValidationCache
# ---------------------------------------------------------------------------


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

    TTL is read from TOKEN_CACHE_TTL (env var, default 300 s).
    sliding=True: every cache hit resets the expiry window so an
    actively-used token is never re-validated until it goes idle for the
    full TTL duration.

    Per-key asyncio.Lock instances prevent duplicate iTop probe calls when
    multiple coroutines race to validate the same token simultaneously.
    """

    def __init__(self, ttl: float, sliding: bool = True) -> None:
        super().__init__(ttl=ttl, sliding=sliding, name="token_cache")
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
        entry = self.get(token_hash)
        if entry is not None:
            return entry.valid

        async with self._lock_guard:
            if token_hash not in self._locks:
                self._locks[token_hash] = asyncio.Lock()
            token_lock = self._locks[token_hash]

        async with token_lock:
            entry = self.get(token_hash)
            if entry is not None:
                return entry.valid

            try:
                valid = await probe_fn()
            except Exception:
                valid = False

            self.set(token_hash, TokenEntry(valid=valid, last_seen=time.monotonic()))
            logger.debug(
                "[token_cache] validated: valid=%s hash_prefix=%s ttl=%.0fs",
                valid, token_hash[:8], self._ttl,
            )
            return valid

    async def evict_by_token(self, token_hash: str) -> None:
        """Remove the cache entry and its lock for the given token hash.

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


# Singleton -- TTL read from config (env var TOKEN_CACHE_TTL, default 300 s)
token_cache = TokenValidationCache(ttl=TOKEN_CACHE_TTL, sliding=True)

# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

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


def cache_get(obj_class: str, ref: str) -> "tuple[str, int] | None":
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
# Stores the normalised transition schema (fields + transitions) per iTop class.
#
# Normalisation (applied once on every cache miss):
#   - Top-level fields: preserved as-is from REST (type, values,
#     is_lifecycle_state, etc.). These are field metadata used for hints and
#     lifecycle state discovery -- they must not be filtered.
#   - Per-state fields in transitions: only options in REQUIRED_FIELD_OPTIONS
#     are retained. Fields with no remaining required option are dropped.
#   - Targets: ALL transitions returned by REST are preserved. The REST API
#     is the single authority on which stimuli are exposed.
#
# Key:   obj_class string, e.g. "UserRequest"
# Value: normalised dict with keys "fields" and "transitions"
# TTL:   TRANSITION_CACHE_TTL env var, default 3600 s (1 h)

import os as _os

_TRANSITION_CACHE_TTL = float(_os.environ.get("TRANSITION_CACHE_TTL", "3600"))
_transition_cache: TTLCache = TTLCache(
    ttl=_TRANSITION_CACHE_TTL, sliding=False, name="transition_cache"
)


def _normalise_transition_schema(schema: dict) -> dict:
    """Return a normalised copy of a raw enumerate_transitions schema.

    Top-level field metadata (type, values, is_lifecycle_state, etc.) is
    preserved verbatim. Per-state field options are filtered to
    REQUIRED_FIELD_OPTIONS only; state fields with no remaining required
    option are dropped entirely.

    Target normalisation:
      - Preserve every valid target/stimulus entry returned by REST.
      - No filtering by stimulus type; the REST API controls exposure.

    The input dict is not mutated; a new dict is returned.
    """

    def _normalise_state_fields(raw_fields) -> dict:
        # Filters per-state field entries: keeps only required option flags,
        # drops fields that carry none of them.
        if not isinstance(raw_fields, dict):
            return {}
        result = {}
        for field_name, field_opts in raw_fields.items():
            if not isinstance(field_opts, dict):
                continue
            raw_options = field_opts.get("options", [])
            kept_options = [
                opt for opt in raw_options
                if opt in REQUIRED_FIELD_OPTIONS
            ]
            if not kept_options:
                continue
            normalised_opts = dict(field_opts)
            normalised_opts["options"] = kept_options
            result[field_name] = normalised_opts
        return result

    def _normalise_targets(raw_targets) -> dict:
        if not isinstance(raw_targets, dict):
            return {}
        result = {}
        for next_state, stim_map in raw_targets.items():
            if not isinstance(stim_map, dict) or not stim_map:
                continue
            result[next_state] = dict(stim_map)
        return result

    # Top-level fields are field metadata (type, values, is_lifecycle_state)
    # used for hint generation and lifecycle state attribute discovery.
    # They must be preserved verbatim.
    raw_top_fields = schema.get("fields", {})
    normalised_fields = dict(raw_top_fields) if isinstance(raw_top_fields, dict) else {}

    raw_transitions = schema.get("transitions", {})
    normalised_transitions = {}
    for state_name, state_map in raw_transitions.items():
        if not isinstance(state_map, dict):
            continue
        normalised_transitions[state_name] = {
            "fields": _normalise_state_fields(state_map.get("fields", {})),
            "targets": _normalise_targets(state_map.get("targets", {})),
        }

    return {
        "fields": normalised_fields,
        "transitions": normalised_transitions,
    }


async def get_transition_map(obj_class: str, client) -> dict:
    """Return the normalised transition schema for obj_class.

    Fetches from iTop on cache miss, normalises the payload, then caches
    the result. Callers always receive the normalised schema dict (keys
    "fields" and "transitions") -- never the raw REST envelope.

    TTL is controlled by the TRANSITION_CACHE_TTL env var (default 3600 s).
    The cache is in-process only; restart the server to force a reload.

    Args:
        obj_class: iTop class name, e.g. "UserRequest".
        client:    ItopClient instance -- used only on cache miss.

    Returns:
        Normalised dict with top-level keys "fields" and "transitions".

    Raises:
        ValueError: if the iTop response is missing or has an unexpected structure.
    """
    cached = _transition_cache.get(obj_class)
    if cached is not None:
        logger.debug("[transition_cache] hit for cls=%r", obj_class)
        return cached

    logger.debug("[transition_cache] miss for cls=%r -- fetching from iTop", obj_class)

    response = await client.enumerate_transitions(obj_class)
    schema = response.get("result")

    if not isinstance(schema, dict):
        raise ValueError(
            "enumerate_transitions returned no valid result for " + obj_class
        )
    if not isinstance(schema.get("transitions"), dict):
        raise ValueError(
            "enumerate_transitions result missing transitions map for " + obj_class
        )
    if not isinstance(schema.get("fields"), dict):
        raise ValueError(
            "enumerate_transitions result missing fields map for " + obj_class
        )

    normalised = _normalise_transition_schema(schema)
    _transition_cache.set(obj_class, normalised)
    logger.debug(
        "[transition_cache] cached normalised schema for cls=%r "
        "states=%d top_fields=%d",
        obj_class,
        len(normalised["transitions"]),
        len(normalised["fields"]),
    )
    return normalised


# ---------------------------------------------------------------------------
# Class schema cache (company/describe_class)
# ---------------------------------------------------------------------------
# Stores the full field schema per iTop class for the lifetime of the process.
# No TTL: iTop class definitions do not change at runtime without a restart.
# A per-class asyncio.Lock prevents duplicate REST calls on parallel access.
#
# Key:   obj_class string, e.g. "UserRequest"
# Value: raw fields dict from the describe_class REST response

_schema_locks: dict[str, asyncio.Lock] = {}
_schema_lock_guard = asyncio.Lock()


async def get_class_schema(obj_class: str, client) -> dict:
    """Return the full field schema for obj_class from company/describe_class.

    On cache hit, returns the stored schema dict immediately.
    On cache miss, acquires a per-class lock, calls company/describe_class,
    validates the response, seeds the cache, and returns the schema.

    The schema dict maps field name to a metadata dict containing at least
    'type', and optionally 'allowed_values', 'values_limited', and
    'is_lifecycle_state'.

    Args:
        obj_class: iTop class name, e.g. "UserRequest".
        client:    ItopClient instance -- used only on cache miss.

    Returns:
        dict mapping field name -> field metadata dict.

    Raises:
        ValueError: if the iTop response is missing or malformed.
    """
    cached = class_cache.get_schema(obj_class)
    if cached is not None:
        return cached

    async with _schema_lock_guard:
        if obj_class not in _schema_locks:
            _schema_locks[obj_class] = asyncio.Lock()
        cls_lock = _schema_locks[obj_class]

    async with cls_lock:
        # Re-check after acquiring the per-class lock (another coroutine
        # may have populated the cache while we were waiting).
        cached = class_cache.get_schema(obj_class)
        if cached is not None:
            return cached

        logger.debug(
            "[class_schema] miss cls=%r -- calling company/describe_class", obj_class
        )
        response = await client.describe_class(obj_class)

        # The REST envelope wraps the payload under 'result'; fall back to
        # the top-level response dict for installations that differ.
        payload = response.get("result") or response
        schema = payload.get("fields") if isinstance(payload, dict) else None

        if not isinstance(schema, dict) or not schema:
            raise ValueError(
                "describe_class returned no valid fields for " + obj_class
                + " -- response: " + repr(response)
            )

        class_cache.seed_schema(obj_class, schema)
        logger.debug(
            "[class_schema] cached schema cls=%r fields=%d",
            obj_class, len(schema),
        )
        return schema
