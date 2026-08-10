"""
Indexed search tool: Search_objects.

Calls company/indexedsearch on the iTop REST endpoint, which in turn queries
the Typesense index maintained by the company-search-typesense extension.
The MCP server has no direct Typesense dependency; all search configuration
(host, API key, collection, field weights) is owned by the iTop extension.

Tool contract
-------------
Search_objects(query, obj_class, limit) -> str

  query      Required. Non-blank search text.
  obj_class  Optional iTop class filter. Empty string searches all indexed
             classes (FAQ, UserRequest, Incident, NormalChange by default).
  limit      1..10. Clamped before the iTop call.

LLM-facing output (success, one or more hits):

  Found <N> indexed match(es):

  1. <class> #<id> -- <friendlyname>
  2. <class> #<id>

  Use Load_object with the selected class and ID.

LLM-facing output (zero hits):

  No indexed objects matched "<query>".

LLM-facing output (iTop error or malformed response):

  Search is temporarily unavailable. Use Resolve_object with a known
  reference or an OQL query, then use Load_object with the returned
  class and ID.

Helper
------
_format_indexed_search_response(response, query) is extracted as a pure
function so it can be unit-tested without FastMCP registration machinery.
"""

from __future__ import annotations

from client import ItopClient

# Recovery instruction returned on any non-zero iTop code or malformed result.
_UNAVAILABLE = (
    "Search is temporarily unavailable. Use Resolve_object with a known "
    "reference or an OQL query, then use Load_object with the returned "
    "class and ID."
)

# Maximum hits accepted from the caller. The iTop extension also enforces this.
_MAX_LIMIT = 10
_MIN_LIMIT = 1


def _format_indexed_search_response(response: dict, query: str) -> str:
    """Format the raw iTop REST response for the LLM.

    Args:
        response: Full iTop response dict as returned by client.indexed_search().
        query:    The original trimmed query string, used in the no-hit message.

    Returns:
        A plain-text string suitable for direct LLM consumption.
    """
    if not isinstance(response, dict) or response.get("code", -1) != 0:
        return _UNAVAILABLE

    result = response.get("result")
    if not isinstance(result, dict):
        return _UNAVAILABLE

    raw_hits = result.get("hits")
    if not isinstance(raw_hits, list):
        raw_hits = []

    # Defensive validation and deduplication (defense in depth; the iTop
    # extension already deduplicates, but the MCP layer does not trust it).
    seen: dict[str, bool] = {}
    hits: list[dict] = []
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        cls = hit.get("class", "")
        if not isinstance(cls, str) or cls.strip() == "":
            continue
        obj_id = hit.get("id", 0)
        if not isinstance(obj_id, int) or obj_id <= 0:
            continue
        key = cls + ":" + str(obj_id)
        if key in seen:
            continue
        seen[key] = True
        hits.append({"class": cls, "id": obj_id, "friendlyname": str(hit.get("friendlyname", ""))})

    if not hits:
        return 'No indexed objects matched "' + query + '".'

    lines = ["Found " + str(len(hits)) + " indexed match" + ("es" if len(hits) != 1 else "") + ":"]
    lines.append("")
    for i, hit in enumerate(hits, start=1):
        fn = hit["friendlyname"].strip()
        if fn:
            lines.append(str(i) + ". " + hit["class"] + " #" + str(hit["id"]) + " -- " + fn)
        else:
            lines.append(str(i) + ". " + hit["class"] + " #" + str(hit["id"]))
    lines.append("")
    lines.append("Use Load_object with the selected class and ID.")
    return "\n".join(lines)


def register(mcp, client: ItopClient) -> None:
    """Register the Search_objects tool on the given FastMCP instance."""

    @mcp.tool(name="Search_objects")
    async def itop_search_objects(
        query: str,
        obj_class: str = "",
        limit: int = 10,
    ) -> str:
        """Search Typesense-indexed iTop objects (FAQ, user requests, incidents,
        normal changes). Returns class and numeric ID for use with Load_object.

        Results come from the Typesense index maintained by the iTop extension.
        The index may lag behind live iTop data by a short synchronisation window.
        Always load the full, authoritative object with Load_object after selecting
        a result.

        When search is unavailable, use Resolve_object with a known reference or
        OQL query and then Load_object with the returned class and ID.

        Args:
            query:     Search text. Required; must not be blank.
            obj_class: Optional iTop class filter, e.g. 'FAQ' or 'UserRequest'.
                       Leave empty to search all indexed classes.
            limit:     Maximum results to return (1-10).
        """
        query = query.strip()
        if not query:
            return "Error: query must not be empty."

        obj_class = obj_class.strip()
        clamped_limit = max(_MIN_LIMIT, min(_MAX_LIMIT, int(limit)))

        response = await client.indexed_search(query, obj_class, clamped_limit)
        return _format_indexed_search_response(response, query)
