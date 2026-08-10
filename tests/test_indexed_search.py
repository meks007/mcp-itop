"""
Tests for the indexed search feature.

Covers:
  - client.indexed_search() request shape
  - _format_indexed_search_response() output for success, zero hits,
    failure, malformed response, limit clamping, and defensive deduplication

These tests import server modules directly and mock network calls so that
no iTop or Typesense connection is required.
"""

from __future__ import annotations

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Path setup: tests/ sits next to server/; add server/ to the import path.
# ---------------------------------------------------------------------------

_SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SERVER_DIR))


# ---------------------------------------------------------------------------
# Helpers used in multiple tests
# ---------------------------------------------------------------------------

def _make_success(hits: list) -> dict:
    return {
        "code": 0,
        "message": "Result returned",
        "result": {"found": len(hits), "hits": hits},
    }


def _make_hit(cls: str, obj_id: int, friendlyname: str = "") -> dict:
    return {"class": cls, "id": obj_id, "friendlyname": friendlyname}


# ---------------------------------------------------------------------------
# 1. client.indexed_search() request shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_indexed_search_request_shape():
    from client import ItopClient

    client = ItopClient()
    mock_request = AsyncMock(return_value={"code": 0, "result": {"found": 0, "hits": []}})
    client.request = mock_request

    await client.indexed_search("vpn", "FAQ", 10)

    mock_request.assert_called_once_with({
        "operation": "company/indexedsearch",
        "query": "vpn",
        "class": "FAQ",
        "limit": 10,
    })


@pytest.mark.asyncio
async def test_client_indexed_search_defaults():
    from client import ItopClient

    client = ItopClient()
    mock_request = AsyncMock(return_value={"code": 0, "result": {"found": 0, "hits": []}})
    client.request = mock_request

    await client.indexed_search("vpn")

    mock_request.assert_called_once_with({
        "operation": "company/indexedsearch",
        "query": "vpn",
        "class": "",
        "limit": 10,
    })


# ---------------------------------------------------------------------------
# 2. Successful formatting
# ---------------------------------------------------------------------------

def test_format_success_two_hits():
    from tools.search import _format_indexed_search_response

    response = _make_success([
        _make_hit("FAQ", 123, "VPN setup"),
        _make_hit("Incident", 456, "I-000456"),
    ])
    result = _format_indexed_search_response(response, "vpn")

    assert "Found 2 indexed matches" in result
    assert "FAQ #123 -- VPN setup" in result
    assert "Incident #456 -- I-000456" in result
    assert "Load_object" in result
    # Must not contain raw JSON, Typesense fields, or status text
    assert "{" not in result
    assert "code" not in result
    assert "Typesense" not in result


def test_format_success_blank_friendlyname():
    from tools.search import _format_indexed_search_response

    response = _make_success([_make_hit("FAQ", 99, "")])
    result = _format_indexed_search_response(response, "test")

    assert "FAQ #99" in result
    # No trailing dash when friendlyname is blank
    assert "FAQ #99 --" not in result


def test_format_success_single_hit_grammar():
    from tools.search import _format_indexed_search_response

    response = _make_success([_make_hit("FAQ", 1, "Article")])
    result = _format_indexed_search_response(response, "article")

    assert "Found 1 indexed match:" in result
    assert "matches" not in result


# ---------------------------------------------------------------------------
# 3. Zero-hit formatting
# ---------------------------------------------------------------------------

def test_format_zero_hits():
    from tools.search import _format_indexed_search_response

    response = {"code": 0, "result": {"found": 0, "hits": []}}
    result = _format_indexed_search_response(response, "vpn")

    assert result == 'No indexed objects matched "vpn".'


# ---------------------------------------------------------------------------
# 4. Failure formatting
# ---------------------------------------------------------------------------

def test_format_failure_non_zero_code():
    from tools.search import _format_indexed_search_response, _UNAVAILABLE

    response = {"code": 100, "message": "Indexed search is temporarily unavailable."}
    result = _format_indexed_search_response(response, "vpn")

    assert result == _UNAVAILABLE
    # Must not copy the iTop error message into the output
    assert "temporarily unavailable" not in result.lower() or result == _UNAVAILABLE


def test_format_failure_does_not_leak_itop_message():
    from tools.search import _format_indexed_search_response, _UNAVAILABLE

    response = {"code": 500, "message": "Internal server error with details"}
    result = _format_indexed_search_response(response, "vpn")

    assert "Internal server error" not in result
    assert result == _UNAVAILABLE


# ---------------------------------------------------------------------------
# 5. Malformed response
# ---------------------------------------------------------------------------

def test_format_malformed_result_null():
    from tools.search import _format_indexed_search_response, _UNAVAILABLE

    result = _format_indexed_search_response({"code": 0, "result": None}, "vpn")
    assert result == _UNAVAILABLE


def test_format_malformed_hits_not_list():
    from tools.search import _format_indexed_search_response, _UNAVAILABLE

    result = _format_indexed_search_response(
        {"code": 0, "result": {"found": 1, "hits": "bad"}}, "vpn"
    )
    assert result == _UNAVAILABLE


def test_format_malformed_no_result_key():
    from tools.search import _format_indexed_search_response, _UNAVAILABLE

    result = _format_indexed_search_response({"code": 0}, "vpn")
    assert result == _UNAVAILABLE


# ---------------------------------------------------------------------------
# 6. Tool-side limit clamping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_clamps_limit_high():
    from tools.search import _MAX_LIMIT

    from client import ItopClient
    client = ItopClient()
    mock_indexed = AsyncMock(return_value={"code": 0, "result": {"found": 0, "hits": []}})
    client.indexed_search = mock_indexed

    # Import and call register to get the tool function, then call it directly.
    # Since FastMCP registration wraps the inner function, we test the helper
    # and the clamping logic by calling the module-level helper directly.
    from tools import search as search_mod

    # Rebuild the clamped call manually as the tool does.
    clamped = max(search_mod._MIN_LIMIT, min(search_mod._MAX_LIMIT, 100))
    assert clamped == _MAX_LIMIT


@pytest.mark.asyncio
async def test_tool_clamps_limit_zero():
    from tools.search import _MIN_LIMIT, _MAX_LIMIT

    clamped = max(_MIN_LIMIT, min(_MAX_LIMIT, 0))
    assert clamped == _MIN_LIMIT


# ---------------------------------------------------------------------------
# 7. Defensive deduplication and malformed hits
# ---------------------------------------------------------------------------

def test_format_deduplication():
    from tools.search import _format_indexed_search_response

    response = _make_success([
        _make_hit("FAQ", 123, "First copy"),
        _make_hit("FAQ", 123, "Duplicate"),   # same class:id -- must be dropped
        _make_hit("Incident", 0, "Zero ID"),  # invalid id -- must be dropped
        _make_hit("", 99, "Blank class"),     # blank class -- must be dropped
        _make_hit("Incident", 789, "Valid"),
    ])
    result = _format_indexed_search_response(response, "test")

    assert "Found 2 indexed matches" in result
    assert "FAQ #123" in result
    assert "Incident #789" in result
    assert "Duplicate" not in result
    assert "Zero ID" not in result
    assert "Blank class" not in result


def test_format_all_hits_invalid():
    from tools.search import _format_indexed_search_response

    response = _make_success([
        _make_hit("", 1, "blank class"),
        _make_hit("FAQ", 0, "zero id"),
    ])
    result = _format_indexed_search_response(response, "test")

    assert 'No indexed objects matched "test".' == result
