"""
Knowledge base tools: search articles, get article, list categories.

ID-only contract
----------------
Get_KB_article requires a confirmed integer database ID (article_id: int).
Use Resolve_object first if you only have a ref or user-supplied number.

Auto-detection
--------------
All three tools auto-detect the installed KB class at runtime by probing
[KBEntry, FAQ] in order. The result is cached for the lifetime of the process
via ensure_class_exists / the class_cache registry. The text body field is
resolved once per class via get_class_schema and cached as registry meta.

Caching
-------
- KB class:       class_cache registry (permanent, probed once per process)
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

from client import ItopClient
from cache import get_class_schema
from helpers import (
    ensure_class_exists,
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

# Candidate article classes in probe order.
_KB_CANDIDATES = ["KBEntry", "FAQ"]

# Maps article class to its category class.
_KB_CAT_MAP = {"KBEntry": "KBCategory", "FAQ": "FAQCategory"}

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


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, client: ItopClient):
    """Register all KB tools on the given mcp instance."""

    async def _kb_meta() -> tuple:
        """Return (kb_cls, text_field) resolving and caching both in one call.

        Resolution order for text_field:
          1. Registry meta cache -- free after first call.
          2. Class schema via get_class_schema (describe_class) -- definitive.
          3. Registry field inventory seeded by ensure_class_exists.
          4. Hard fallback to "description".
        """
        kb_cls = await ensure_class_exists(_KB_CANDIDATES)
        if not kb_cls:
            logger.debug("[kb] _kb_meta: no KB class found in candidates %r", _KB_CANDIDATES)
            return "", "description"

        cached = registry_get_meta(kb_cls, "text_field")
        if cached:
            logger.debug("[kb] _kb_meta: cls=%r text_field=%r (meta cache)", kb_cls, cached)
            return kb_cls, cached

        # Prefer schema-based resolution (authoritative field inventory).
        try:
            schema = await get_class_schema(kb_cls, client)
            for field in _KB_TEXT_FIELD_CANDIDATES:
                if field in schema:
                    registry_set_meta(kb_cls, "text_field", field)
                    logger.debug(
                        "[kb] _kb_meta: cls=%r text_field=%r (schema)", kb_cls, field
                    )
                    return kb_cls, field
        except Exception as exc:
            logger.warning("[kb] _kb_meta: get_class_schema failed cls=%r exc=%s", kb_cls, exc)

        # Fall back to the field inventory seeded by ensure_class_exists.
        known = registry_get_fields(kb_cls)
        if known:
            for field in _KB_TEXT_FIELD_CANDIDATES:
                if field in known:
                    registry_set_meta(kb_cls, "text_field", field)
                    logger.debug(
                        "[kb] _kb_meta: cls=%r text_field=%r (field inventory)", kb_cls, field
                    )
                    return kb_cls, field

        logger.debug("[kb] _kb_meta: cls=%r text_field=description (fallback)", kb_cls)
        registry_set_meta(kb_cls, "text_field", "description")
        return kb_cls, "description"

    @mcp.tool(name="Search_KB_articles")
    async def itop_search_kb(
        keywords: str,
        limit: int = 20,
    ) -> str:
        """Search knowledge-base articles by title, summary, or description.

        Pass meaningful, specific keywords that describe the topic -- individual
        nouns such as object type, symptom, or component. Multiple keywords can
        be separated by spaces or commas; each is searched independently with OR
        logic, which yields far better results than passing full phrases or
        sentences. Automatically detects the available KB class and body field.

        Note: iTop KB articles are typically created with status=production and
        visibility=internal. Search results include all status values; filter
        with additional OQL via Load_object if needed.
        """
        kb_cls, text_field = await _kb_meta()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        terms = [t.strip() for t in re.split(r"[\s,]+", keywords) if t.strip()]
        if not terms:
            terms = [keywords]
        safe_terms = [_sanitise_like_term(t) for t in terms]
        clauses = " OR ".join(
            "title LIKE '%" + t + "%' OR " + text_field + " LIKE '%" + t + "%'"
            for t in safe_terms
        )
        effective_oql = "SELECT " + kb_cls + " WHERE " + clauses

        logger.debug("[kb] search_kb: oql=%r limit=%d", effective_oql, limit)

        result = await client.get(
            kb_cls,
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
                "No KB articles found for keywords '" + keywords + "'.\n"
                "OQL used: " + effective_oql + "\n"
                "Body field used: " + text_field + "."
            )

        header = ["ID", "Title", "Category", "Status", "Summary"]
        rows = []
        for a in articles:
            f = a["fields"]
            raw_body = str_or(f, text_field, "")
            snippet = _strip_html(raw_body)[:_SNIPPET_LEN].strip()
            if len(_strip_html(raw_body)) > _SNIPPET_LEN:
                snippet += "..."
            rows.append([
                str(a["key"]),
                str_or(f, "title", "?")[:60],
                str_or(f, "category_name", "-"),
                str_or(f, "status", "?"),
                snippet,
            ])

        out = ["**" + kb_cls + " Articles** matching '" + keywords + "':", ""]
        out.append(format_table(header, rows))
        if len(articles) >= limit:
            out.append(
                "\nShowing " + str(limit) + " results. Use a higher limit or more specific "
                "keywords to narrow results."
            )
        return "\n".join(out)

    @mcp.tool(name="Get_KB_article")
    async def itop_get_kb_article(article_id: int) -> str:
        """Get the full content of a knowledge-base article by numeric ID.

        Auto-detects KBEntry vs FAQ. article_id must be a confirmed integer
        database ID -- use Resolve_object first if you only have a ref.
        """
        kb_cls, _ = await _kb_meta()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        logger.debug("[kb] get_kb_article: cls=%r article_id=%r", kb_cls, article_id)

        result = await client.get(kb_cls, article_id, fields="*+")

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
        """List all knowledge-base categories. Auto-detects KBCategory vs FAQCategory."""
        kb_cls, _ = await _kb_meta()
        if not kb_cls:
            return "No KB module installed."

        cat_cls = _KB_CAT_MAP.get(kb_cls, "KBCategory")
        logger.debug("[kb] list_kb_categories: cat_cls=%r limit=%d", cat_cls, limit)

        # Resolve available fields via schema to avoid requesting fields that
        # do not exist on this installation.
        try:
            schema = await get_class_schema(cat_cls, client)
            optional = [f for f in ("description",) if f in schema]
        except Exception as exc:
            logger.warning(
                "[kb] list_kb_categories: get_class_schema failed cls=%r exc=%s", cat_cls, exc
            )
            optional = []

        fields = "id,name" + ("," + ",".join(optional) if optional else "")
        logger.debug("[kb] list_kb_categories: fields=%r", fields)

        result = await client.get(
            cat_cls,
            "SELECT " + cat_cls,
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

        out = ["**KB Categories (" + cat_cls + "):**", ""]
        out.append(format_table(header, rows))
        if len(cats) >= limit:
            out.append(
                "\nShowing " + str(limit) + " categories. Use a higher limit if more exist."
            )
        return "\n".join(out)
