"""
Knowledge base tools: search articles, get article, list categories.

ID-only contract
----------------
Get_KB_article requires a confirmed integer database ID (article_id: int).
Use Resolve_object first if you only have a ref or user-supplied number.

The IDs returned by Search_KB_articles and List_KB_categories are confirmed
integer database IDs. Pass them directly to Get_KB_article or Load_object
without calling Resolve_object first.

Class configuration
-------------------
The KB article class and category class are read from config at startup:
  ITOP_KB_CLASS     -- article class, default KBEntry (set FAQ in .env if needed)
  ITOP_KB_CAT_CLASS -- derived automatically: KBEntry->KBCategory, FAQ->FAQCategory

No runtime class probing is performed. The values are fixed for the lifetime
of the process.

Text body field
---------------
Resolved once per process via get_class_schema (describe_class) against
_KB_TEXT_FIELD_CANDIDATES, then cached as registry meta. No live probing.

Caching
-------
- Text field:     class_cache meta key "text_field" (permanent)
- Class schema:   class_cache schema slot via get_class_schema (permanent)

iTop defaults for new articles
-------------------------------
Articles in iTop are typically created with:
  status     = production
  visibility = internal
These are iTop server-side defaults; this tool does not set them explicitly.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from client import ItopClient
from cache import get_class_schema
from config import ITOP_KB_CLASS, ITOP_KB_CAT_CLASS
from helpers import (
    extract_objects,
    format_and_cache,
    format_table,
    registry_get_fields,
    registry_get_meta,
    registry_set_meta,
    str_or,
)
from helpers.html import _strip_html

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Candidate text body fields in preference order.
# Resolved against the class schema (get_class_schema), not via live probing,
# to avoid false positives from iTop returning code=0 on empty result sets.
_KB_TEXT_FIELD_CANDIDATES = ["description", "summary", "solution", "document"]

# Maximum characters of stripped body text shown as a snippet in search results.
_SNIPPET_LEN = 120


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _sanitise_like_term(term: str) -> str:
    """Escape OQL LIKE wildcards and dangerous characters from a search term.

    Removes single quotes (OQL string delimiter).
    Escapes percent and underscore (OQL LIKE wildcards) with a backslash so
    they are treated as literals. Backslashes are escaped first to avoid
    double-escaping.
    """
    term = term.replace("'", "")
    term = term.replace("\\", "\\\\")
    term = term.replace("%", "\\%")
    term = term.replace("_", "\\_")
    return term


def _kb_list_fields(text_field: str) -> str:
    """Return the output_fields string used for search result rows."""
    return "id,title," + text_field + ",category_name,status"


def _build_oql_clauses(
    safe_terms: list[str],
    text_field: str,
    match_mode: str,
) -> str:
    """Build the WHERE clause portion of an OQL query.

    all_terms:    every term must appear in title or body (AND across terms,
                  OR across fields per term).
    exact_phrase: the full query is treated as one literal phrase that must
                  appear consecutively in title or body.
    any_term:     any single term is sufficient (OR across all terms and
                  fields). Broad -- use only as a fallback.
    """
    if match_mode == "exact_phrase":
        # safe_terms already escaped; rejoin as the original phrase.
        phrase = " ".join(safe_terms)
        return (
            "title LIKE '%" + phrase + "%'"
            + " OR " + text_field + " LIKE '%" + phrase + "%'"
        )

    if match_mode == "all_terms":
        # Each term produces one (title OR body) sub-clause; all must match.
        per_term = [
            "(title LIKE '%" + t + "%' OR " + text_field + " LIKE '%" + t + "%')"
            for t in safe_terms
        ]
        return " AND ".join(per_term)

    # any_term: union of all field/term combinations.
    return " OR ".join(
        "title LIKE '%" + t + "%' OR " + text_field + " LIKE '%" + t + "%'"
        for t in safe_terms
    )


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, client: ItopClient):
    """Register all KB tools on the given mcp instance."""

    async def _kb_text_field() -> str:
        """Return the confirmed text body field for ITOP_KB_CLASS.

        Resolution order:
          1. Registry meta cache -- free after first call.
          2. Class schema via get_class_schema (describe_class) -- definitive.
          3. Registry field inventory (seeded elsewhere on prior get calls).
          4. Hard fallback to "description".
        """
        cached = registry_get_meta(ITOP_KB_CLASS, "text_field")
        if cached:
            logger.debug("[kb] _kb_text_field: cls=%r text_field=%r (meta cache)", ITOP_KB_CLASS, cached)
            return cached

        try:
            schema = await get_class_schema(ITOP_KB_CLASS, client)
            for field in _KB_TEXT_FIELD_CANDIDATES:
                if field in schema:
                    registry_set_meta(ITOP_KB_CLASS, "text_field", field)
                    logger.debug(
                        "[kb] _kb_text_field: cls=%r text_field=%r (schema)", ITOP_KB_CLASS, field
                    )
                    return field
        except Exception as exc:
            logger.warning(
                "[kb] _kb_text_field: get_class_schema failed cls=%r exc=%s", ITOP_KB_CLASS, exc
            )

        known = registry_get_fields(ITOP_KB_CLASS)
        if known:
            for field in _KB_TEXT_FIELD_CANDIDATES:
                if field in known:
                    registry_set_meta(ITOP_KB_CLASS, "text_field", field)
                    logger.debug(
                        "[kb] _kb_text_field: cls=%r text_field=%r (field inventory)",
                        ITOP_KB_CLASS, field,
                    )
                    return field

        logger.debug("[kb] _kb_text_field: cls=%r text_field=description (fallback)", ITOP_KB_CLASS)
        registry_set_meta(ITOP_KB_CLASS, "text_field", "description")
        return "description"

    @mcp.tool(name="Search_KB_articles")
    async def itop_search_kb(
        query: str,
        match_mode: Literal["all_terms", "exact_phrase", "any_term"] = "all_terms",
        limit: int = 20,
    ) -> str:
        """Search titles and body text in the configured knowledge-base article class.

        Use the most specific suitable query with all_terms first. Do not use any_term
        unless that search returns no useful result.

        - all_terms (default): all query terms must appear in an article's title or body.
        - exact_phrase: the complete query must appear consecutively.
        - any_term: one or more query terms may match; use only as a fallback.

        Results include confirmed numeric IDs for direct use with Get_KB_article or Load_object.
        """
        text_field = await _kb_text_field()

        terms = [t.strip() for t in re.split(r"[\s,]+", query) if t.strip()]
        if not terms:
            terms = [query]
        safe_terms = [_sanitise_like_term(t) for t in terms]

        clauses = _build_oql_clauses(safe_terms, text_field, match_mode)
        effective_oql = "SELECT " + ITOP_KB_CLASS + " WHERE " + clauses

        logger.debug(
            "[kb] search_kb: mode=%r oql=%r limit=%d", match_mode, effective_oql, limit
        )

        result = await client.get(
            ITOP_KB_CLASS,
            effective_oql,
            fields=_kb_list_fields(text_field),
            limit=limit,
        )

        if result.get("code", -1) != 0:
            return (
                "Error from iTop: " + str(result.get("message", "unknown error"))
                + " (OQL: " + effective_oql + ")"
            )

        articles = extract_objects(result)
        if not articles:
            return (
                "No KB articles found for query '" + query + "' (mode=" + match_mode + ").\n"
                "OQL used: " + effective_oql + "\n"
                "Body field used: " + text_field + ".\n"
                "Consider refining the concepts or using a broader match_mode."
            )

        header = ["ID", "Title", "Category", "Status", "Summary"]
        rows = []
        for a in articles:
            f = a["fields"]
            raw_body = str_or(f, text_field, "")
            stripped = _strip_html(raw_body)
            snippet = stripped[:_SNIPPET_LEN].strip()
            if len(stripped) > _SNIPPET_LEN:
                snippet += "..."
            rows.append([
                str(a["key"]),
                str_or(f, "title", "?")[:60],
                str_or(f, "category_name", "-"),
                str_or(f, "status", "?"),
                snippet,
            ])

        out = [
            "**" + ITOP_KB_CLASS + " Articles** matching '"
            + query + "' (mode=" + match_mode + "):",
            "",
        ]
        out.append(format_table(header, rows))
        if len(articles) >= limit:
            out.append(
                "\nShowing " + str(limit) + " results. Use a higher limit or more specific "
                "query to narrow results."
            )
        return "\n".join(out)

    @mcp.tool(name="Get_KB_article")
    async def itop_get_kb_article(article_id: int) -> str:
        """Retrieve a complete knowledge-base article by confirmed numeric ID using the configured article class. IDs returned by Search_KB_articles can be used directly; otherwise resolve the ID first.
        """
        logger.debug(
            "[kb] get_kb_article: cls=%r article_id=%r", ITOP_KB_CLASS, article_id
        )

        result = await client.get(ITOP_KB_CLASS, article_id, fields="*+")

        if result.get("code", -1) != 0:
            return (
                "Error from iTop: " + str(result.get("message", "unknown error"))
                + " (article_id=" + str(article_id) + ")"
            )

        if not extract_objects(result):
            return "KB article #" + str(article_id) + " not found."

        return format_and_cache(result)

    @mcp.tool(name="List_KB_categories")
    async def itop_list_kb_categories(limit: int = 100) -> str:
        """List categories from the configured knowledge-base category class. Returned IDs are confirmed numeric IDs and can be passed directly to Load_object.
        """
        logger.debug("[kb] list_kb_categories: cat_cls=%r limit=%d", ITOP_KB_CAT_CLASS, limit)

        try:
            schema = await get_class_schema(ITOP_KB_CAT_CLASS, client)
            optional = [f for f in ("description",) if f in schema]
        except Exception as exc:
            logger.warning(
                "[kb] list_kb_categories: get_class_schema failed cls=%r exc=%s",
                ITOP_KB_CAT_CLASS, exc,
            )
            optional = []

        fields = "id,name" + ("," + ",".join(optional) if optional else "")
        logger.debug("[kb] list_kb_categories: fields=%r", fields)

        result = await client.get(
            ITOP_KB_CAT_CLASS,
            "SELECT " + ITOP_KB_CAT_CLASS,
            fields=fields,
            limit=limit,
        )

        if result.get("code", -1) != 0:
            return "Error from iTop: " + str(result.get("message", "unknown error"))

        cats = extract_objects(result)
        if not cats:
            return "No KB categories found."

        header = ["ID", "Name"] + (["Description"] if optional else [])
        rows = []
        for c in cats:
            f = c["fields"]
            row = [str(c["key"]), str_or(f, "name", "?")]
            if optional:
                row.append(str_or(f, "description", "")[:80])
            rows.append(row)

        out = ["**KB Categories (" + ITOP_KB_CAT_CLASS + "):**", ""]
        out.append(format_table(header, rows))
        if len(cats) >= limit:
            out.append(
                "\nShowing " + str(limit) + " categories. Use a higher limit if more exist."
            )
        return "\n".join(out)
