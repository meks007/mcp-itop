"""
Comment tools: add ticket log entries, describe mention configuration.

Mention tokens in the comment text are automatically resolved via
company/resolve_mentions before the log entry is written to iTop.
Tokens that cannot be resolved are left as plain text; the comment
is always saved regardless of mention resolution results.

Reading logs: use Load_object with full=True -- public_log and private_log are
included in the full record, so a separate log-fetch call is never needed.

Mention syntax
--------------
  @<id>       Tag a Person by numeric iTop object id, e.g. @24
  ?<id>       Reference an FAQ article by numeric id, e.g. ?4711
  R-<ref>     Link a UserRequest by its ref, e.g. R-000084
  I-<ref>     Link an Incident by its ref, e.g. I-001247
  C-<ref>     Link a Change by its ref, e.g. C-001485

The agent must resolve Person and FAQ objects via Load_object before writing
the comment and use the confirmed numeric id in the token. Ticket references
(R-, I-, C-) are resolved automatically from the text without a prior lookup.
IDs and references must never be invented.

Call Describe_mentions to obtain the currently configured tags and their target
classes dynamically. This is the authoritative source; do not rely solely on
the examples above.
"""

from __future__ import annotations

import json
from typing import Literal, Optional, Union

from client import ItopClient
from helpers import coerce_ref, format_and_cache, resolve_key, resolve_mentions_in_text
from helpers.mentions import get_mention_config

_VALID_LOG_FIELDS = {"public_log", "private_log", "private_log_ai"}


def register(mcp, client: ItopClient):
    """Register all comment tools on the given mcp instance."""

    @mcp.tool(
        name="Add_comment_to_ticket"
    )
    async def itop_add_comment(
        ticket_class: str,
        text: str,
        ticket_ref: Optional[str] = None,
        ticket_id: Optional[Union[int, str]] = None,
        log_field: Literal["public_log", "private_log", "private_log_ai"] = "public_log",
    ) -> str:
        """Add an HTML log entry to an iTop ticket.

        Use log_field='public_log' to write a portal-visible comment or interim report (default).
        Use log_field='private_log' for an internal note.
        Prefer ticket_ref; bare ticket IDs are resolved automatically.
        To read existing comments, use Load_object with full=True.

        Mention tokens in *text* are resolved automatically:
          @<id>     Person by numeric id (resolve Person first, then use @<id>)
          ?<id>     FAQ article by numeric id (resolve FAQ first, then use ?<id>)
          R-<ref>   UserRequest reference -- resolved automatically
          I-<ref>   Incident reference -- resolved automatically
          C-<ref>   Change reference -- resolved automatically
        Unresolvable tokens are stored as plain text without blocking the comment.
        Call Describe_mentions for the authoritative and up-to-date tag list.
        """
        if not ticket_ref and not ticket_id:
            return "Error: supply ticket_ref (e.g. 'R-016271') or ticket_id."

        if log_field not in _VALID_LOG_FIELDS:
            return "Error: log_field must be one of " + ", ".join(sorted(_VALID_LOG_FIELDS)) + "."

        ticket_class, key = await resolve_key(
            ticket_class, coerce_ref(ticket_ref or "", ticket_id or "")
        )

        # Resolve mention tokens before writing the log entry.
        # The resolved HTML replaces canonical tokens (@<id>, ?<id>, R-<ref>, ...)
        # with iTop-compatible <a> elements. Unresolved tokens remain plain text.
        resolved_text = await resolve_mentions_in_text(
            text, ticket_class, log_field, client
        )

        result = await client.update(
            ticket_class,
            key,
            {
                log_field: {
                    "add_item": {
                        "message": resolved_text,
                        "format": "html",
                    }
                }
            },
            output_fields="id, ref, friendlyname",
            comment="MCP: added comment to " + log_field,
        )
        return format_and_cache(result)

    @mcp.tool(
        name="Describe_mentions"
    )
    async def itop_describe_mentions() -> str:
        """Return the currently configured mention tags and their iTop target classes.

        Call this tool before writing a mention to confirm the valid token syntax.
        Use it:
          - at the start of any task that requires an intentional Person or FAQ mention;
          - when you need to verify currently valid tag characters and target classes;
          - whenever the configuration may have changed (e.g. a new tag was added).

        Returns a JSON object keyed by tag character, e.g.:
          {
            "@": {"class": "Person", "lookup_attribute": "id"},
            "?": {"class": "FAQ", "lookup_attribute": "id"},
            "R": {"class": "UserRequest", "lookup_attribute": "ref"},
            "I": {"class": "Incident", "lookup_attribute": "ref"},
            "C": {"class": "Change", "lookup_attribute": "ref"}
          }

        lookup_attribute "id":  strip the tag character, use the remaining digits as the object id.
                                You must look up the object first and use the confirmed numeric id.
        lookup_attribute "ref": use the full token as-is (e.g. R-000084). Resolved automatically.
        """
        config = await get_mention_config(client)
        if not config:
            return "Describe_mentions: no mention configuration returned by iTop (company/describe_mentions)."
        return json.dumps(config, ensure_ascii=False)
