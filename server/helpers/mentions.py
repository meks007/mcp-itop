"""
Mention resolution helper.

Detects canonical mention tokens in HTML/text content and replaces them
with iTop-compatible <a> elements by calling two iTop REST operations:

  company/describe_mentions
      Returns the configured mention tags, their target class and the
      lookup attribute used to resolve the referenced object.

  company/resolve_mentions
      Accepts a list of canonical tokens together with the triggering
      class and target attribute. Returns only successfully resolved
      mentions. Each entry includes the object class, id, visible label
      and a ready-to-use href for the iTop detail view.

Public API
----------
  resolve_mentions_in_text(text, obj_class, attribute, client)
      Resolve all mention tokens found in *text* and return the HTML
      string with matching tokens replaced by <a> elements.
      Tokens that cannot be resolved are left unchanged.

  get_mention_config(client)
      Return the cached describe_mentions configuration dict.
      Re-fetches from iTop when the cache is empty or stale.

Mention token rules
-------------------
A token is eligible when its first character matches a configured tag.

  lookup_attribute == "id"
      The tag character is stripped; the remainder must be a non-empty
      sequence of digits.  Example: "@24" -> Person id 24

  lookup_attribute == "ref"
      The full token (including the tag character) is used as the
      lookup value.  The tag character must NOT be prepended a second
      time.  Example: "R-000084" -> UserRequest ref "R-000084"

Tokens are delimited by whitespace and common punctuation. A token that
does not match the expected shape for its tag (e.g. "@abc" when
lookup_attribute is "id") is silently skipped and left as plain text.

HTML generation
---------------
Every resolved token is replaced by:

  <a href="{href}" data-role="object-mention"
     data-object-class="{class}" data-object-key="{id}">{label}</a>

All attribute values are HTML-escaped. The href delivered by iTop is
used verbatim (after escaping); it is never constructed by this module.

Replacement is performed on plain-text token boundaries so that existing
HTML markup, attribute values and already-linked text are not modified.

Caching
-------
The describe_mentions configuration is cached in-process for
MENTION_CONFIG_TTL_SECONDS (default 300). The cache is shared across
all concurrent requests.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from client import ItopClient

# ---------------------------------------------------------------------------
# Configuration cache
# ---------------------------------------------------------------------------

# TTL for the describe_mentions config (seconds).
MENTION_CONFIG_TTL_SECONDS = 300

_mention_config_cache: dict | None = None
_mention_config_fetched_at: float = 0.0
_mention_config_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _mention_config_lock
    if _mention_config_lock is None:
        _mention_config_lock = asyncio.Lock()
    return _mention_config_lock


async def get_mention_config(client: "ItopClient") -> dict:
    """Return the describe_mentions config, fetching/refreshing as needed.

    The result is a dict keyed by tag character, e.g.:
        {
            "@": {"class": "Person", "lookup_attribute": "id"},
            "R": {"class": "UserRequest", "lookup_attribute": "ref"},
            ...
        }

    PackAsRestResult() wraps the extension payload under "result".
    We read response["result"] and fall back to an empty dict on any error.

    Args:
        client: Active ItopClient instance.

    Returns:
        Mention config dict. Empty dict on error.
    """
    global _mention_config_cache, _mention_config_fetched_at

    now = time.monotonic()
    if (
        _mention_config_cache is not None
        and (now - _mention_config_fetched_at) < MENTION_CONFIG_TTL_SECONDS
    ):
        return _mention_config_cache

    async with _get_lock():
        # Re-check after acquiring lock.
        now = time.monotonic()
        if (
            _mention_config_cache is not None
            and (now - _mention_config_fetched_at) < MENTION_CONFIG_TTL_SECONDS
        ):
            return _mention_config_cache

        try:
            response = await client.request({"operation": "company/describe_mentions"})
            if response.get("code", -1) != 0:
                return _mention_config_cache or {}
            # PackAsRestResult wraps data under "result".
            config = response.get("result") or {}
            if not isinstance(config, dict):
                return _mention_config_cache or {}
            _mention_config_cache = config
            _mention_config_fetched_at = time.monotonic()
        except Exception:
            return _mention_config_cache or {}

    return _mention_config_cache or {}


# ---------------------------------------------------------------------------
# Token detection
# ---------------------------------------------------------------------------

# Characters that terminate a mention token in running text.
# Adjust if iTop refs can legally contain these characters.
_TOKEN_BREAK_RE = re.compile(r"[\s,;:!.()\[\]{}<>\"']+")


def _split_tokens(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, token) triples for every whitespace/punct-delimited
    word in *text*.  Only plain-text regions are considered; this function
    does not parse HTML.
    """
    tokens = []
    pos = 0
    length = len(text)
    while pos < length:
        # Skip delimiters.
        m = _TOKEN_BREAK_RE.match(text, pos)
        if m:
            pos = m.end()
            continue
        # Consume a token.
        end = pos + 1
        while end < length and not _TOKEN_BREAK_RE.match(text, end):
            end += 1
        tokens.append((pos, end, text[pos:end]))
        pos = end
    return tokens


def _find_candidates(text: str, config: dict) -> list[tuple[int, int, str, dict]]:
    """Identify mention candidates in *text*.

    Returns a list of (start, end, token, tag_config) sorted by start
    position.  Only tokens whose first character is a configured tag and
    whose shape matches the expected lookup_attribute type are included.

    Overlapping candidates are not possible because tokens are
    non-overlapping by construction.
    """
    candidates = []
    for start, end, token in _split_tokens(text):
        if not token:
            continue
        tag_char = token[0]
        tag_cfg = config.get(tag_char)
        if not tag_cfg:
            continue
        lookup_attr = tag_cfg.get("lookup_attribute", "")
        if lookup_attr == "id":
            # Remainder after the tag char must be all digits.
            remainder = token[1:]
            if not remainder or not remainder.isdigit():
                continue
        # For lookup_attribute == "ref" (or any other value) the full token
        # is sent as-is; no further shape validation is applied here. iTop
        # will reject tokens it cannot resolve and they will be left as plain
        # text.
        candidates.append((start, end, token, tag_cfg))
    return candidates


# ---------------------------------------------------------------------------
# Resolver call
# ---------------------------------------------------------------------------

async def _call_resolve_mentions(
    tokens: list[str],
    obj_class: str,
    attribute: str,
    client: "ItopClient",
) -> dict[str, dict]:
    """Call company/resolve_mentions and return a dict keyed by input token.

    PackAsRestResult() wraps extension data under "result". We read
    response["result"]["resolved"] accordingly.

    Args:
        tokens:     List of canonical mention tokens, e.g. ["R-000084", "@24"].
        obj_class:  Concrete iTop class of the object being written.
        attribute:  Target attribute name, e.g. "public_log" or "description".
        client:     Active ItopClient instance.

    Returns:
        Dict mapping each successfully resolved input token to its resolved
        data dict (class, id, label, href).  Unresolved tokens are absent.
    """
    if not tokens:
        return {}

    try:
        response = await client.request({
            "operation": "company/resolve_mentions",
            "class": obj_class,
            "attribute": attribute,
            "mentions": tokens,
        })
    except Exception:
        return {}

    if response.get("code", -1) != 0:
        return {}

    # PackAsRestResult wraps data under "result"; resolved list is nested.
    resolved_list = (response.get("result") or {}).get("resolved") or []
    return {
        item["input"]: item
        for item in resolved_list
        if isinstance(item, dict) and "input" in item and "href" in item
    }


# ---------------------------------------------------------------------------
# HTML link builder
# ---------------------------------------------------------------------------

def _build_anchor(resolved: dict) -> str:
    """Return a safe iTop mention <a> element from a resolved mention dict.

    All values are HTML-escaped.  The href delivered by iTop is used
    verbatim after escaping; it is never constructed here.

    Required attributes per iTop Core mention detection:
      href              - detail page URL
      data-role         - must be "object-mention"
      data-object-class - resolved iTop class
      data-object-key   - resolved iTop object id

    Args:
        resolved: Single entry from the "resolved" array of
                  company/resolve_mentions.

    Returns:
        HTML string for the <a> element.
    """
    href = html.escape(str(resolved.get("href", "")), quote=True)
    obj_class = html.escape(str(resolved.get("class", "")), quote=True)
    obj_id = html.escape(str(resolved.get("id", "")), quote=True)
    label = html.escape(str(resolved.get("label", "")))
    return (
        '<a href="' + href + '"'
        + ' data-role="object-mention"'
        + ' data-object-class="' + obj_class + '"'
        + ' data-object-key="' + obj_id + '">'
        + label
        + '</a>'
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def resolve_mentions_in_text(
    text: str,
    obj_class: str,
    attribute: str,
    client: "ItopClient",
) -> str:
    """Detect mention tokens in *text* and replace resolved ones with <a> links.

    Tokens that iTop cannot resolve are left as plain text.  The comment is
    always returned, even when some or all mentions fail to resolve.

    This function operates on the plain-text portions of *text*.  It does not
    parse HTML; callers should pass the raw message string before it is wrapped
    in an add_item payload.

    Args:
        text:       Raw comment or HTML field value containing mention tokens.
        obj_class:  Concrete iTop class of the object being written, e.g.
                    "UserRequest".
        attribute:  Target attribute name, e.g. "public_log", "description".
        client:     Active ItopClient instance.

    Returns:
        Text with resolved mention tokens replaced by iTop-compatible <a>
        elements.
    """
    if not text:
        return text

    config = await get_mention_config(client)
    if not config:
        return text

    candidates = _find_candidates(text, config)
    if not candidates:
        return text

    # Deduplicate while preserving order for the API call.
    seen: set[str] = set()
    unique_tokens: list[str] = []
    for _, _, token, _ in candidates:
        if token not in seen:
            seen.add(token)
            unique_tokens.append(token)

    resolved_map = await _call_resolve_mentions(unique_tokens, obj_class, attribute, client)
    if not resolved_map:
        return text

    # Replace candidates from right to left to preserve string offsets.
    result = text
    for start, end, token, _ in reversed(candidates):
        resolved = resolved_map.get(token)
        if resolved is None:
            continue
        anchor = _build_anchor(resolved)
        result = result[:start] + anchor + result[end:]

    return result


# ---------------------------------------------------------------------------
# Helpers for tool modules
# ---------------------------------------------------------------------------

_CASELOG_ATTRIBUTES = frozenset({"public_log", "private_log", "private_log_ai"})


def is_caselog_attribute(attribute: str) -> bool:
    """Return True when *attribute* is a known Case Log field."""
    return attribute in _CASELOG_ATTRIBUTES
