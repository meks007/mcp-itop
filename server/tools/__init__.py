"""
tools/__init__.py - registers all tool sub-modules with the mcp instance.

Import this module after creating the mcp instance to attach all tools.
"""

from tools import analytics, attachments, comments, crud, kb, transitions


def register_all(mcp, client):
    """Register every tool module with the given mcp instance."""
    crud.register(mcp, client)
    transitions.register(mcp, client)
    analytics.register(mcp, client)
    attachments.register(mcp, client)
    comments.register(mcp, client)
    kb.register(mcp, client)
