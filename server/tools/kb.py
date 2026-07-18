"""
Knowledge base tools: search, get article, list categories.
"""

from __future__ import annotations

import re

from client import ItopClient
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

# Candidate article classes in probe order
_KB_CANDIDATES = ["KBEntry", "FAQ"]
_KB_CAT_MAP = {"KBEntry": "KBCategory", "FAQ": "FAQCategory"}

# Candidate text body fields in preference order.
# iTop returns code=0 even for unknown fields in a plain SELECT output_fields
# probe, but OQL WHERE clauses reject unknown field names with code=100.
# We resolve the text field against the registry field inventory rather than
# via a live request to avoid false positives.
_KB_TEXT_FIELD_CANDIDATES = ["description", "summary", "solution", "document"]

# Common German/English search terms that add noise to KB searches. Keep this
# deliberately small: domain-specific terms must remain searchable.
_KB_SEARCH_STOPWORDS = {
    "ein", "eine", "einer", "eines", "der", "die", "das", "den", "dem", "des",
    "und", "oder", "f¸r", "von", "mit", "bei", "auf", "im", "in", "zu", "an",
    "ist", "sind", "wie", "the", "a", "an", "and", "or", "for", "with", "from",
}


def _keyword_terms(query: str) -> list[str]:
    """Extract distinctive terms for the default KB search OQL.

    The MCP tool receives natural-language input quite often. iTop LIKE does
    not tokenize that input, so using the complete phrase is usually too
    restrictive. Preserve the user's terms, remove only punctuation and very
    common stopwords, and cap the result to keep the generated OQL manageable.
    """
    terms = re.findall(r"[\wƒ÷‹‰ˆ¸ﬂ-]+", query, flags=re.UNICODE)
    result = []
    seen = set()
    for term in terms:
        normalized = term.strip("-").lower()
        if len(normalized) < 2 or normalized in _KB_SEARCH_STOPWORDS:
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(term.strip("-"))
    return result[:8] or [query.strip()]


def register(mcp, client: ItopClient):
    """Register all KB tools on the given mcp instance."""

    async def _kb_class() -> str:
        """Return the confirmed KB article class, probing once if needed."""
        return await ensure_class_exists(_KB_CANDIDATES)

    async def _kb_text_field(kb_cls: str) -> str:
        """Return the confirmed text body field for kb_cls.

        Resolution order:
        1. Registry meta cache (text_field key) -- free after first call.
        2. Registry field inventory -- populated by ensure_class_exists.
           Intersect _KB_TEXT_FIELD_CANDIDATES with known fields.
        3. Live output_fields probe -- last resort for empty classes.
        4. Hard fallback to "description".
        """
        cached = registry_get_meta(kb_cls, "text_field")
        if cached:
            return cached

        known = registry_get_fields(kb_cls)
        if known:
            for field in _KB_TEXT_FIELD_CANDIDATES:
                if field in known:
                    registry_set_meta(kb_cls, "text_field", field)
                    return field

        # Live probe -- only reached when class has zero instances.
        for field in _KB_TEXT_FIELD_CANDIDATES:
            r = await client.get(
                kb_cls,
                "SELECT " + kb_cls,
                fields=field,
                limit=1,
            )
            if r.get("code") == 0:
                registry_set_meta(kb_cls, "text_field", field)
                return field

        registry_set_meta(kb_cls, "text_field", "description")
        return "description"

    def _kb_list_fields(text_field: str) -> str:
        return "id,title," + text_field + ",category_name,status"

    @mcp.tool(
        name="Search_KB_articles"
    )
    async def itop_search_kb(
        query: str,
        oql: str = "",
        limit: int = 20,
    ) -> str:
        """Search knowledge-base articles using distinctive keywords.

        IMPORTANT: query is a keyword search input, not a full natural-language
        sentence or exact phrase. Extract 1-5 meaningful technical terms from
        the user's request, such as ``Laptop TFT Bildschirm`` or
        ``Outlook Anmeldung Passwort``. Do not pass the complete user sentence,
        a symptom description, or a long phrase unchanged. The default search
        splits query into individual keywords and searches each keyword in title
        and body with OR, so terms remain findable even when article wording
        differs. Use explicit oql only when a precise iTop query is required.

        Automatically detects the available KB class and body field.
        """
        kb_cls = await _kb_class()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        text_field = await _kb_text_field(kb_cls)

        if oql:
            effective_oql = oql
        else:
            keywords = _keyword_terms(query)
            clauses = []
            for keyword in keywords:
                safe = keyword.replace("'", "")
                clauses.append(
                    "title LIKE '%" + safe + "%'"
                    " OR " + text_field + " LIKE '%" + safe + "%'"
                )
            effective_oql = "SELECT " + kb_cls + " WHERE " + " OR ".join(
                "(" + clause + ")" for clause in clauses
            )

        result = await client.get(
            kb_cls,
            effective_oql,
            fields=_kb_list_fields(text_field),
            limit=limit,
        )

        articles = extract_objects(result)
        if not articles:
            return (
                "No KB articles found for query '" + query + "'.\n"
                "OQL used: " + effective_oql + "\n"
                "Body field used: " + text_field + "\n"
                "Tip: use a few distinctive keywords or supply an explicit oql parameter."
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

        out = ["**" + kb_cls + " Articles** matching '" + query + "':", ""]
        out.append(format_table(header, rows))
        return "\n".join(out)

    @mcp.tool(
        name="Get_KB_article"
    )
    async def itop_get_kb_article(article_id: int) -> str:
        """Get the full content of a knowledge-base article by numeric ID. Auto-detects KBEntry vs FAQ."""
        kb_cls = await _kb_class()
        if not kb_cls:
            return "No KB module installed (tried KBEntry, FAQ)."

        result = await client.get(
            kb_cls,
            "SELECT " + kb_cls + " WHERE id=" + str(article_id),
            fields="*+",
        )

        if not extract_objects(result):
            return "KB article #" + str(article_id) + " not found."

        return format_and_cache(result)

    @mcp.tool(
        name="List_KB_categories"
    )
    async def itop_list_kb_categories() -> str:
        """List all knowledge-base categories. Auto-detects KBCategory vs FAQCategory."""
        kb_cls = await _kb_class()
        if not kb_cls:
            return "No KB module installed."

        cat_cls = _KB_CAT_MAP.get(kb_cls, "KBCategory")

        result = await client.get(
            cat_cls,
            "SELECT " + cat_cls,
            fields="id,name,description",
            limit=100,
        )

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
