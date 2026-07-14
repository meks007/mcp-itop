"""
Knowledge base tools: search, get article, list categories.
"""

from __future__ import annotations

from helpers import (
    ensure_class_exists,
    extract_objects,
    format_objects,
    format_table,
    registry_get_meta,
    registry_set_meta,
    str_or,
)

# Candidate class pairs: (article_class, category_class)
_KB_CANDIDATES = ["KBEntry", "FAQ"]
_KB_CAT_MAP = {"KBEntry": "KBCategory", "FAQ": "FAQCategory"}
_KB_TEXT_FIELD_CANDIDATES = ["description", "summary", "contents", "solution", "document"]


def register(mcp, itop_request):
    """Register all KB tools on the given mcp instance."""

    async def _kb_class() -> str:
        """Return the confirmed KB article class, probing once if needed."""
        return await ensure_class_exists(_KB_CANDIDATES, itop_request)

    async def _kb_text_field(kb_cls: str) -> str:
        """Return the confirmed text body field for kb_cls.

        Result is stored in the universal class registry under meta key
        'text_field' so the probe runs at most once per server lifetime.
        """
        cached = registry_get_meta(kb_cls, "text_field")
        if cached:
            return cached
        for field in _KB_TEXT_FIELD_CANDIDATES:
            r = await itop_request({
                "operation": "core/get",
                "class": kb_cls,
                "key": f"SELECT {kb_cls}",
                "output_fields": field,
                "limit": "1",
            })
            if r.get("code") == 0:
                registry_set_meta(kb_cls, "text_field", field)
                return field
        # Last resort -- keeps title search working even if body probe fails
        registry_set_meta(kb_cls, "text_field", "description")
        return "description"

    def _kb_list_fields(text_field: str) -> str:
        return f"id,title,{text_field},category_name,status"

    @mcp.tool()
    async def itop_search_kb(
        query: str,
        oql: str = "",
        limit: int = 20,
    ) -> str:
        """Search knowledge-base articles by text in title or body.

        Auto-detects KBEntry vs FAQ and probes which field holds the article
        body (description, summary, contents, solution, document). Single
        quotes in query are stripped -- iTop OQL has no backslash-escape support.

        Supply oql to bypass the auto-built LIKE query entirely (same pattern
        as itop_get). When oql is provided, query is used only in the header.
        Call itop_describe_class with the KB class first if the exact fields
        are known.

        Args:
            query: Search text. Single quotes stripped before OQL use.
            oql: Optional full OQL override, e.g. "SELECT KBEntry WHERE title LIKE '%vpn%'".
            limit: Maximum results; default 20.
        """
        kb_cls = await _kb_class()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        text_field = await _kb_text_field(kb_cls)

        if oql:
            effective_oql = oql
        else:
            safe = query.replace("'", "")
            effective_oql = (
                f"SELECT {kb_cls} WHERE title LIKE '%{safe}%'"
                f" OR {text_field} LIKE '%{safe}%'"
            )

        result = await itop_request({
            "operation": "core/get",
            "class": kb_cls,
            "key": effective_oql,
            "output_fields": _kb_list_fields(text_field),
            "limit": str(limit),
        })

        articles = extract_objects(result)
        if not articles:
            return (
                f"No KB articles found for query '{query}'.\n"
                f"OQL used: {effective_oql}\n"
                f"Body field probed: {text_field}\n"
                "Tip: call itop_describe_class with the KB class to verify available "
                "fields, then retry with an explicit oql parameter."
            )

        header = ["ID", "Title", "Category", "Status"]
        rows = []
        for a in articles:
            f = a["fields"]
            rows.append([
                str(a["key"]),
                str_or(f, "title", "?")[:60],
                str_or(f, "category_name", "-"),
                str_or(f, "status", "?"),
            ])

        out = [f"**{kb_cls} Articles** matching '{query}':", ""]
        out.append(format_table(header, rows))
        return "\n".join(out)

    @mcp.tool()
    async def itop_get_kb_article(article_id: int) -> str:
        """Get the full content of a knowledge-base article by ID.

        Auto-detects KBEntry vs FAQ. Redact or skip anything resembling a password.

        Args:
            article_id: Numeric article ID.
        """
        kb_cls = await _kb_class()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        result = await itop_request({
            "operation": "core/get",
            "class": kb_cls,
            "key": f"SELECT {kb_cls} WHERE id={article_id}",
            "output_fields": "*+",
        })

        if not extract_objects(result):
            return f"KB article #{article_id} not found."

        return format_objects(result)

    @mcp.tool()
    async def itop_list_kb_categories() -> str:
        """List all knowledge-base categories.

        Auto-detects KBCategory vs FAQCategory.
        """
        kb_cls = await _kb_class()
        if not kb_cls:
            return "No KB module installed."

        cat_cls = _KB_CAT_MAP.get(kb_cls, "KBCategory")

        result = await itop_request({
            "operation": "core/get",
            "class": cat_cls,
            "key": f"SELECT {cat_cls}",
            "output_fields": "id,name,description",
            "limit": "100",
        })

        cats = extract_objects(result)
        if not cats:
            return "No KB categories found."

        header = ["ID", "Name", "Description"]
        rows = []
        for c in cats:
            f = c["fields"]
            rows.append([
                str(c["key"]),
                str_or(f, "name", "?"),
                str_or(f, "description", "")[:60],
            ])

        out = ["**KB Categories:**", ""]
        out.append(format_table(header, rows))
        return "\n".join(out)
