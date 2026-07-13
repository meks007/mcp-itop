"""
Knowledge base tools: search, get article, list categories.
"""

from __future__ import annotations

from helpers import extract_objects, format_objects, format_table, str_or

# Auto-detected KB class (KBEntry or FAQ) - cached after first detection
_KB_CLASS: str | None = None
_KB_CATEGORY_CLASS: str | None = None


def register(mcp, itop_request):
    """Register all KB tools on the given mcp instance."""

    async def _detect_kb_class() -> tuple[str, str]:
        """Detect available KB class (KBEntry or FAQ)."""
        global _KB_CLASS, _KB_CATEGORY_CLASS
        if _KB_CLASS is not None:
            return _KB_CLASS, _KB_CATEGORY_CLASS  # type: ignore

        for cls, cat_cls in [("KBEntry", "KBCategory"), ("FAQ", "FAQCategory")]:
            r = await itop_request({
                "operation": "core/get",
                "class": cls,
                "key": f"SELECT {cls}",
                "output_fields": "id",
                "limit": "1",
            })
            if r.get("code") == 0:
                _KB_CLASS = cls
                _KB_CATEGORY_CLASS = cat_cls
                return cls, cat_cls

        _KB_CLASS = ""
        _KB_CATEGORY_CLASS = ""
        return "", ""

    def _get_kb_fields() -> str:
        return "id,title,summary,category_name,status"

    @mcp.tool()
    async def itop_search_kb(
        query: str,
        limit: int = 20,
    ) -> str:
        """Search knowledge base articles by text in title or summary.

        Auto-detects KB class (only class is FAQ).

        Args:
            query: Search text.
            limit: Max results (default: 20).
        """
        kb_cls, _ = await _detect_kb_class()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        safe = query.replace("'", "\\'")
        oql = f"SELECT {kb_cls} WHERE title LIKE '%{safe}%' OR summary LIKE '%{safe}%'"

        result = await itop_request({
            "operation": "core/get",
            "class": kb_cls,
            "key": oql,
            "output_fields": _get_kb_fields(),
            "limit": str(limit),
        })

        articles = extract_objects(result)
        if not articles:
            return f"No KB articles found for query '{query}'."

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
        """Get full knowledge base article by ID.
        If you encounter anything that looks like a password, redact or skip it!
        
        Args:
            article_id: Article ID (FAQ).
        """
        kb_cls, _ = await _detect_kb_class()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        result = await itop_request({
            "operation": "core/get",
            "class": kb_cls,
            "key": f"SELECT {kb_cls} WHERE id={article_id}",
            "output_fields": "*+",
        })

        articles = extract_objects(result)
        if not articles:
            return f"KB article #{article_id} not found."

        return format_objects(result)

    @mcp.tool()
    async def itop_list_kb_categories() -> str:
        """List all knowledge base categories."""
        _, cat_cls = await _detect_kb_class()
        if not cat_cls:
            return "No KB module installed."

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
