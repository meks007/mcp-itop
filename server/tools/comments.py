"""
Comment tools: add ticket log entries, describe mention configuration.

Mention tokens in the comment text are automatically resolved via
company/resolve_mentions before the log entry is written to iTop.
Tokens that cannot be resolved are left as plain text; the comment
is always saved regardless of mention resolution results.

Reading logs: use Load_object with full=True -- public_log and private_log are
included in the full record, so a separate log-fetch call is never needed.

ID-only contract
----------------
Add_comment_to_ticket requires a confirmed integer database ID (obj_id: int).
Use Resolve_object first if you only have a ref or user-supplied number.

Mention syntax
--------------
  @<id>       Tag a Person by numeric iTop object id, e.g. @24
  ?<id>       Reference an FAQ article by numeric id, e.g. ?4711
  R-<ref>     Link a UserRequest by its ref, e.g. R-000084
  I-<ref>     Link an Incident by its ref, e.g. I-001247
  C-<ref>     Link a Change by its ref, e.g. C-001485

The agent must resolve Person and FAQ objects via Resolve_object before
writing the comment and use the confirmed numeric id in the token. Ticket
references (R-, I-, C-) are resolved automatically from the text without a
prior lookup. IDs and references must never be invented.

Call Describe_mentions to obtain the currently configured tags and their
target classes dynamically.
"""

from __future__ import annotations

import json
from typing import Literal

from client import ItopClient
from helpers import format_and_cache, resolve_mentions_in_text
from helpers.mentions import get_mention_config

_VALID_LOG_FIELDS = {"public_log", "private_log", "private_log_ai"}


def register(mcp, client: ItopClient):
    """Register all comment tools on the given mcp instance."""

    @mcp.tool(name="Add_comment_to_ticket")
    async def itop_add_comment(
        obj_class: str,
        obj_id: int,
        text: str,
        log_field: Literal["public_log", "private_log", "private_log_ai"] = "public_log",
    ) -> str:
        """Add an HTML entry to a ticket log using a confirmed numeric object ID. log_field may be public_log, private_log, or private_log_ai. Mention tokens are resolved automatically: @<id> for a Person, ?<id> for an FAQ article, and R-/I-/C- references for tickets. Resolve Person and FAQ IDs first; call Describe_mentions for configured tags.
        """
        if log_field not in _VALID_LOG_FIELDS:
            return "Error: log_field must be one of " + ", ".join(sorted(_VALID_LOG_FIELDS)) + "."

        resolved_text = await resolve_mentions_in_text(
            text, obj_class, log_field, client
        )

        result = await client.update(
            obj_class,
            obj_id,
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

    @mcp.tool(name="Describe_mentions")
    async def itop_describe_mentions() -> str:
        """Return configured mention tags, target classes, and lookup attributes. Resolve objects before using ID-based tags; for reference-based tags, use the complete ticket reference such as R-000084.
        """
        config = await get_mention_config(client)
        if not config:
            return "Describe_mentions: no mention configuration returned by iTop."
        return json.dumps(config, ensure_ascii=False)
