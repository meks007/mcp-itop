#!/usr/bin/env python3
"""
MCP server for iTop ITSM - analytics, tickets, KB, assets.

Provides AI assistants (Claude Desktop, opencode, etc.) with tools to:
  - Analyse SLA compliance, agent workload, service quality
  - Query and update tickets, CI, KB articles via iTop REST API
  - Apply lifecycle transitions (assign, resolve, close)

Based on josephstreeter/mcp_itop (CRUD + stimulus) with extended analytics.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

# Load config: global (~/.config/mcp-itop/.env) overrides project (.env)
_GLOBAL_ENV = os.path.expanduser("~/.config/mcp-itop/.env")
if os.path.isfile(_GLOBAL_ENV):
    load_dotenv(_GLOBAL_ENV, override=True)
load_dotenv()  # project .env (lower priority)

# -- Debug flag -----------------------------------------------------------
# Set MCP_DEBUG=true to log full request/response payloads for:
#   - every MCP tool call between client <-> mcp (via FastMCP middleware)
#   - every iTop REST/JSON API call between mcp <-> iTop
# Auth credentials (token, password) are always redacted from log output.
MCP_DEBUG = os.getenv("MCP_DEBUG", "false").lower() in ("true", "1", "yes")

# -- Logging --------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if MCP_DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-itop")

if MCP_DEBUG:
    logger.debug("MCP_DEBUG is enabled - request/response payloads will be logged (secrets redacted).")

# -- Config ---------------------------------------------------------------
# NOTE: iTop authentication is no longer configured via environment
# variables. Each client must supply its own iTop REST API token as an
# HTTP "Authorization: Bearer <itop_token>" header when connecting to
# this MCP server. The server validates only that a non-empty bearer
# token was presented at connection time (MCP "initialize" handshake);
# the token's actual validity is enforced by iTop itself on every
# REST call (see _itop_request / ItopBearerVerifier below).
ITOP_URL = os.getenv("ITOP_URL", "").rstrip("/")
ITOP_VERSION = os.getenv("ITOP_VERSION", "1.3")
ITOP_VERIFY_SSL = os.getenv("ITOP_VERIFY_SSL", "true").lower() not in ("false", "0", "no")
ITOP_TIMEOUT = float(os.getenv("ITOP_TIMEOUT", "30"))

DEFAULT_COMMENT = "Modified via MCP"


def _redact_form_data(data: dict) -> dict:
    """Return a copy of the iTop form-data dict with auth secrets masked, for safe logging."""
    redacted = dict(data)
    for key in ("auth_token", "auth_pwd"):
        if key in redacted and redacted[key]:
            redacted[key] = "***REDACTED***"
    return redacted


# -- FastMCP --------------------------------------------------------------
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.fastmcp import FastMCP


class ItopBearerVerifier(TokenVerifier):
    """Validates presence of a bearer token at MCP handshake time.

    The token itself is the caller's iTop REST/JSON API auth_token. We do
    not (and cannot, without calling iTop) verify it is a valid iTop
    token here - only that the client presented a non-empty bearer value.
    An invalid/expired token will simply be rejected by iTop on the first
    real REST call made through itop_* tools.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not token.strip():
            return None  # causes MCP handshake to fail with 401
        return AccessToken(token=token, client_id="itop-client", scopes=[])


mcp = FastMCP(
    "iTop",
    instructions=(
        "MCP server for iTop IT Service Management with analytics. "
        "Provides SLA reports, agent workload analysis, service quality checks, "
        "ticket lifecycle, KB search, and CI impact analysis."
    ),
    token_verifier=ItopBearerVerifier(),
)

if MCP_DEBUG:
    # Log every MCP request/response exchanged between the client
    # (Claude Desktop, opencode, MCP Inspector, etc.) and this
    # server: tool calls, list_tools, initialize, etc.
    try:
        from fastmcp.server.middleware import Middleware, MiddlewareContext

        class DebugLoggingMiddleware(Middleware):
            async def on_message(self, context: MiddlewareContext, call_next):
                logger.debug("CLIENT -> MCP  method=%s message=%s", context.method, getattr(context, "message", None))
                result = await call_next(context)
                logger.debug("CLIENT <- MCP  method=%s result=%s", context.method, result)
                return result

        mcp.add_middleware(DebugLoggingMiddleware())
        logger.debug("Client<->MCP debug logging middleware attached.")
    except ImportError:
        logger.warning("MCP_DEBUG is set but fastmcp.server.middleware is unavailable; client<->mcp logging disabled.")

# -- HTTP client ----------------------------------------------------------
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(verify=ITOP_VERIFY_SSL, timeout=ITOP_TIMEOUT)
    return _http_client


# -- Helpers --------------------------------------------------------------
def _str_or(d: dict, key: str, default: str = "") -> str:
    v = d.get(key)
    return str(v) if v is not None else default


def _parse_key(key: str) -> Any:
    try:
        return json.loads(key)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return int(key)
    except ValueError:
        return key


def _parse_json_arg(raw: str, arg_name: str) -> dict | str:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return f"Invalid JSON in '{arg_name}': {e.msg} at position {e.pos}"


def _get_bearer_token() -> str:
    """Return the iTop auth_token supplied by the connected client.

    Each client authenticates to this MCP server with its own iTop REST
    API token via "Authorization: Bearer <itop_token>". FastMCP verifies
    a token was presented at handshake time (see ItopBearerVerifier) and
    exposes it here, per request, via the access-token context.
    """
    access_token = mcp.get_context().request_context.access_token
    if access_token is None or not access_token.token:
        raise ValueError(
            "No iTop auth token found on this connection. Connect with an "
            "'Authorization: Bearer <itop_token>' header."
        )
    return access_token.token


async def _itop_request(operation: dict) -> dict:
    """Send request to iTop REST/JSON API."""
    if not ITOP_URL:
        raise ValueError("ITOP_URL is not configured. Set it in .env or environment.")

    token = _get_bearer_token()

    url = f"{ITOP_URL}/webservices/rest.php"
    data: dict[str, str] = {
        "version": ITOP_VERSION,
        "json_data": json.dumps(operation),
        "auth_token": token,
    }

    if MCP_DEBUG:
        logger.debug("MCP -> iTop  POST %s  data=%s", url, _redact_form_data(data))

    try:
        resp = await _get_http_client().post(url, data=data)
        resp.raise_for_status()
        result: dict = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning("iTop HTTP %s for op=%s", e.response.status_code, operation.get("operation"))
        if MCP_DEBUG:
            logger.debug("MCP <- iTop  HTTP %s  body=%s", e.response.status_code, e.response.text[:2000])
        return {"code": e.response.status_code, "message": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
    except httpx.HTTPError as e:
        logger.warning("iTop network error: %s", e)
        if MCP_DEBUG:
            logger.debug("MCP <- iTop  network error: %s", e)
        return {"code": -1, "message": f"Network error: {e}"}

    if result.get("code", 0) != 0:
        logger.warning("iTop error code=%s op=%s msg=%s", result.get("code"), operation.get("operation"), result.get("message"))

    if MCP_DEBUG:
        logger.debug("MCP <- iTop  status=%s  response=%s", resp.status_code, json.dumps(result, ensure_ascii=False)[:4000])

    return result


def _extract_objects(result: dict) -> list[dict]:
    """Extract list of {class, key, fields} from iTop response."""
    objs = result.get("objects")
    if not objs:
        return []
    out = []
    for obj_key, obj_data in objs.items():
        out.append({
            "class": obj_data.get("class", "?"),
            "key": obj_data.get("key", "?"),
            "fields": obj_data.get("fields", {}),
        })
    return out


def _format_objects(result: dict) -> str:
    """Format iTop response objects into readable string."""
    if result.get("code", -1) != 0:
        return f"Error (code {result.get('code')}): {_str_or(result, 'message', 'Unknown error')}"
    objects = result.get("objects")
    if not objects:
        return _str_or(result, "message", "No objects found.")
    lines = [_str_or(result, "message", "")]
    for obj_key, obj_data in objects.items():
        cls = _str_or(obj_data, "class", "?")
        oid = _str_or(obj_data, "key", "?")
        fields = obj_data.get("fields", {})
        lines.append(f"\n--- {cls}::{oid} ---")
        for fn, fv in fields.items():
            if isinstance(fv, (dict, list)):
                fv = json.dumps(fv, indent=2, ensure_ascii=False)
            lines.append(f"  {fn}: {fv}")
    return "\n".join(lines)


def _format_table(header: list[str], rows: list[list[str]]) -> str:
    """Simple aligned table formatter."""
    if not rows:
        return "(no data)"
    col_widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))
    lines = []
    sep = " | ".join(h.ljust(w) for h, w in zip(header, col_widths))
    lines.append(sep)
    lines.append("-+-".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(" | ".join(c.ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


def _parse_date_range(start: str, end: str) -> Tuple[str, str]:
    """Normalize date strings; return (start, end) ISO format."""
    try:
        dt_start = datetime.fromisoformat(start) if start else (datetime.now(timezone.utc) - timedelta(days=30))
        dt_end = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    except ValueError:
        raise ValueError(f"Invalid date format. Use ISO 8601: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    return dt_start.strftime("%Y-%m-%d %H:%M:%S"), dt_end.strftime("%Y-%m-%d %H:%M:%S")


SLA_ANALYSIS_FIELDS = (
    "id,ref,title,status,service_name,org_name,agent_name,caller_name,"
    "start_date,assignment_date,resolution_date,close_date,"
    "sla_tto_passed,sla_ttr_passed,time_spent,"
    "last_update"
)

_SLA_PASSED_VALUES = {"true", "yes", "1"}
_SLA_BREACHED_VALUES = {"false", "no", "0"}


def _sla_is_passed(val: str) -> bool:
    return val.strip().lower() in _SLA_PASSED_VALUES if val else False


def _sla_is_breached(val: str) -> bool:
    return val.strip().lower() in _SLA_BREACHED_VALUES if val else False

# ========================================================================
# ANALYTICS TOOLS
# ========================================================================


@mcp.tool()
async def itop_sla_report(
    service_name: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 500,
) -> str:
    """SLA compliance report for a service (or all) over a period.

    Calculates TTO (Time To Own) and TTR (Time To Resolve) metrics:
    - passed: SLA target met
    - breached: SLA target exceeded
    - N/A: SLA not applicable for this ticket
    - Median resolution time and mean time_spent

    Args:
        service_name: Filter by service name (empty = all services).
        start_date: Start of period (ISO 8601, default: 30 days ago).
        end_date: End of period (ISO 8601, default: now).
        limit: Max tickets to fetch (default 500).
    """
    s, e = _parse_date_range(start_date, end_date)

    oql = f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'"
    if service_name:
        oql += f" AND service_name = '{service_name}'"

    result = await _itop_request({
        "operation": "core/get",
        "class": "UserRequest",
        "key": oql,
        "output_fields": SLA_ANALYSIS_FIELDS,
        "limit": str(limit),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return f"No tickets found for period {s[:10]} to {e[:10]}." + (f" Service: {service_name}" if service_name else "")

    total = len(tickets)
    tto_passed = tto_breached = tto_na = 0
    ttr_passed = ttr_breached = ttr_na = 0
    resolution_times = []
    spent_times = []

    for t in tickets:
        f = t["fields"]
        # TTO
        tto = _str_or(f, "sla_tto_passed")
        if _sla_is_passed(tto):
            tto_passed += 1
        elif _sla_is_breached(tto):
            tto_breached += 1
        else:
            tto_na += 1
        # TTR
        ttr = _str_or(f, "sla_ttr_passed")
        if _sla_is_passed(ttr):
            ttr_passed += 1
        elif _sla_is_breached(ttr):
            ttr_breached += 1
        else:
            ttr_na += 1

        # Resolution time
        st = f.get("start_date")
        rt = f.get("resolution_date")
        if st and rt:
            try:
                diff = (datetime.fromisoformat(rt) - datetime.fromisoformat(st)).total_seconds()
                if diff > 0:
                    resolution_times.append(diff)
            except (ValueError, TypeError):
                pass

        # Time spent
        ts = f.get("time_spent")
        if ts:
            try:
                spent_times.append(float(ts))
            except (ValueError, TypeError):
                pass

    lines = [f"**SLA Report**", f"Period: {s[:10]} - {e[:10]}"]
    if service_name:
        lines.append(f"Service: {service_name}")
    lines.extend([
        f"Total tickets: {total}",
        "",
        "**TTO (Time To Own):**",
        f"  Passed:   {tto_passed} ({tto_passed/total*100:.1f}%)" if total else "  Passed:   0",
        f"  Breached: {tto_breached} ({tto_breached/total*100:.1f}%)" if total else "  Breached: 0",
        f"  N/A:      {tto_na} ({tto_na/total*100:.1f}%)" if total else "  N/A:      0",
        "",
        "**TTR (Time To Resolve):**",
        f"  Passed:   {ttr_passed} ({ttr_passed/total*100:.1f}%)" if total else "  Passed:   0",
        f"  Breached: {ttr_breached} ({ttr_breached/total*100:.1f}%)" if total else "  Breached: 0",
        f"  N/A:      {ttr_na} ({ttr_na/total*100:.1f}%)" if total else "  N/A:      0",
    ])

    if resolution_times:
        resolution_times.sort()
        median = resolution_times[len(resolution_times) // 2]
        lines.extend([
            "",
            "**Resolution time:**",
            f"  Median: {_format_duration(median)}",
            f"  Min:    {_format_duration(resolution_times[0])}",
            f"  Max:    {_format_duration(resolution_times[-1])}",
        ])

    if spent_times:
        avg_spent = sum(spent_times) / len(spent_times)
        lines.append(f"  Avg time_spent: {_format_duration(avg_spent)}")

    return "\n".join(lines)


@mcp.tool()
async def itop_agent_workload(
    agent_name: str = "",
    team_name: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 500,
) -> str:
    """Agent workload analysis.

    Shows how many tickets each agent handled (or a specific agent),
    open/closed breakdown, total time_spent, and current backlog.

    Args:
        agent_name: Filter by agent name (empty = all agents).
        team_name: Filter by team (empty = all teams).
        start_date: Start of period (ISO 8601, default: 30 days ago).
        end_date: End of period (ISO 8601, default: now).
        limit: Max tickets to fetch (default 500).
    """
    s, e = _parse_date_range(start_date, end_date)

    oql = f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'"
    if team_name:
        oql += f" AND team_name = '{team_name}'"
    if agent_name:
        oql += f" AND agent_name = '{agent_name}'"

    result = await _itop_request({
        "operation": "core/get",
        "class": "UserRequest",
        "key": oql,
        "output_fields": "id,ref,title,status,agent_name,team_name,time_spent,start_date,resolution_date",
        "limit": str(limit),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return "No tickets found for the specified filters."

    agents: dict[str, dict] = {}
    for t in tickets:
        f = t["fields"]
        agent = _str_or(f, "agent_name") or "(unassigned)"
        if agent not in agents:
            agents[agent] = {
                "closed": 0, "open": 0, "total_time": 0.0,
                "team": _str_or(f, "team_name"),
                "tickets_open": [],
            }
        a = agents[agent]
        status = _str_or(f, "status")
        if status in ("closed", "resolved"):
            a["closed"] += 1
        else:
            a["open"] += 1
            a["tickets_open"].append(_str_or(f, "ref"))

        ts = f.get("time_spent")
        if ts:
            try:
                a["total_time"] += float(ts)
            except (ValueError, TypeError):
                pass

    header = ["Agent", "Team", "Closed", "Open", "Time Spent", "Backlog"]
    rows = []
    for agent, data in sorted(agents.items()):
        backlog = ", ".join(data["tickets_open"][:5])
        if len(data["tickets_open"]) > 5:
            backlog += f" ... +{len(data['tickets_open']) - 5} more"
        flag = " (overloaded)" if data["open"] > 10 else ""
        rows.append([
            agent,
            data["team"] or "-",
            str(data["closed"]),
            str(data["open"]),
            _format_duration(data["total_time"]),
            backlog + flag,
        ])

    lines = [f"**Agent Workload** ({s[:10]} - {e[:10]})", ""]
    lines.append(_format_table(header, rows))
    return "\n".join(lines)


@mcp.tool()
async def itop_idle_agents(
    hours: int = 2,
    status: str = "assigned",
    limit: int = 50,
) -> str:
    """Find tickets where agent has been idle (no action) for N hours.

    Detects tickets that are assigned but have no recent updates,
    indicating the agent may not be responding.

    Args:
        hours: Idle threshold in hours (default: 2).
        status: Ticket status to check (default: 'assigned').
        limit: Max tickets to check (default: 50).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    oql = f"SELECT UserRequest WHERE status='{status}' AND assignment_date < '{cutoff}'"
    result = await _itop_request({
        "operation": "core/get",
        "class": "UserRequest",
        "key": oql,
        "output_fields": "id,ref,title,agent_name,team_name,assignment_date,last_update,start_date",
        "limit": str(limit),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return f"No idle tickets found (status={status}, idle >{hours}h)."

    # For each ticket, check if it has recent comments/public_log activity
    idle_list = []
    for t in tickets:
        f = t["fields"]
        agent = _str_or(f, "agent_name") or "?"
        last_upd = f.get("last_update")
        assigned_at = f.get("assignment_date")

        # If last_update is older than the cutoff, agent hasn't acted
        is_idle = False
        if last_upd:
            try:
                lu = datetime.fromisoformat(last_upd)
                if lu < datetime.now(timezone.utc) - timedelta(hours=hours):
                    is_idle = True
            except (ValueError, TypeError):
                is_idle = True
        else:
            is_idle = True

        if is_idle:
            idle_list.append({
                "ref": _str_or(f, "ref"),
                "title": _str_or(f, "title"),
                "agent": agent,
                "team": _str_or(f, "team_name") or "-",
                "assigned": _str_or(f, "assignment_date", "?")[:16],
                "last_update": _str_or(f, "last_update", "?")[:16],
            })

    if not idle_list:
        return f"No truly idle agents found - all tickets have recent activity."

    header = ["Ticket", "Title", "Agent", "Team", "Assigned", "Last Action"]
    rows = []
    for t in idle_list[:30]:
        rows.append([t["ref"], t["title"][:40], t["agent"], t["team"], t["assigned"], t["last_update"]])

    remaining = len(idle_list) - 30
    out = [f"**Idle Agents** (>{hours}h without action, status={status})", ""]
    out.append(f"Found {len(idle_list)} idle tickets:")
    out.append("")
    out.append(_format_table(header, rows))
    if remaining > 0:
        out.append(f"\n... and {remaining} more (use higher limit)")
    return "\n".join(out)


@mcp.tool()
async def itop_service_quality(
    days: int = 30,
    min_similar: int = 3,
    limit: int = 200,
) -> str:
    """Detect service selection mismatches for similar tickets.

    Groups tickets by common keywords in title, then checks if they
    were assigned to different services. Helps identify classification
    quality issues.

    Args:
        days: Lookback period in days (default: 30).
        min_similar: Minimum similar tickets to report (default: 3).
        limit: Max tickets to fetch (default: 200).
    """
    s = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    e = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    result = await _itop_request({
        "operation": "core/get",
        "class": "UserRequest",
        "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'",
        "output_fields": "id,ref,title,service_name,service_id,caller_name,agent_name,status",
        "limit": str(limit),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return "No tickets found."

    # Extract keywords from titles (simple approach: split and filter stop words)
    STOP_WORDS = {"the", "a", "an",
                   "of", "in", "to", "for", "with", "on", "at", "is", "it", "as"}

    def extract_keywords(title: str) -> set:
        words = set()
        for w in re.findall(r'\w{3,}', title.lower()):
            if w not in STOP_WORDS:
                words.add(w)
        return words

    # Group tickets by shared keywords
    ticket_kw = []
    for t in tickets:
        title = _str_or(t["fields"], "title", "")
        kw = extract_keywords(title)
        ticket_kw.append((t, kw))

    # Find groups with overlapping keywords
    groups = []
    used = set()
    for i, (t1, kw1) in enumerate(ticket_kw):
        if i in used:
            continue
        group = [i]
        used.add(i)
        for j, (t2, kw2) in enumerate(ticket_kw):
            if j in used:
                continue
            if len(kw1 & kw2) >= 2:  # at least 2 shared keywords
                group.append(j)
                used.add(j)
        if len(group) >= min_similar:
            # Check if multiple services in group
            services = set()
            for idx in group:
                services.add(_str_or(tickets[idx]["fields"], "service_name", "(none)"))
            if len(services) > 1:
                groups.append(group)

    if not groups:
        return "No significant service mismatches found."

    out_lines = [f"**Service Quality Check** (last {days} days)", ""]
    for g in groups:
        # Group keywords
        kw_sample = ticket_kw[g[0]][1]
        kw_str = ", ".join(sorted(kw_sample)[:5])
        out_lines.append(f"**Similar tickets (keywords: {kw_str})**")
        header = ["Ticket", "Caller", "Service", "Agent", "Status"]
        rows = []
        for idx in g:
            t = tickets[idx]
            f = t["fields"]
            rows.append([
                _str_or(f, "ref"),
                _str_or(f, "caller_name", "?"),
                _str_or(f, "service_name", "(none)"),
                _str_or(f, "agent_name", "-"),
                _str_or(f, "status"),
            ])
        out_lines.append(_format_table(header, rows))
        out_lines.append("")

    return "\n".join(out_lines)


@mcp.tool()
async def itop_caller_quality(
    min_tickets: int = 5,
    days: int = 60,
    limit: int = 500,
) -> str:
    """Analyse caller service selection accuracy.

    For each caller with enough tickets, checks how often the service
    they selected was changed by an agent. High correction rate = caller
    often picks wrong service.

    Args:
        min_tickets: Minimum tickets per caller to include (default: 5).
        days: Lookback period (default: 60).
        limit: Max tickets (default: 500).
    """
    s = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    e = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # We need to detect if service was changed - iTop REST doesn't expose
    # change history via core/get. We'll use a heuristic:
    # if the ticket has been resolved/closed and has service_name,
    # we assume the final service is the one set by agent.
    # For deeper analysis we'd need CMDBChange log.
    result = await _itop_request({
        "operation": "core/get",
        "class": "UserRequest",
        "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'",
        "output_fields": "id,ref,title,service_name,caller_name,agent_name,status,start_date",
        "limit": str(limit),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return "No tickets found."

    # Group by caller
    callers: dict[str, dict] = {}
    for t in tickets:
        f = t["fields"]
        caller = _str_or(f, "caller_name") or "(unknown)"
        if caller not in callers:
            callers[caller] = {"total": 0, "services": Counter()}
        callers[caller]["total"] += 1
        callers[caller]["services"][_str_or(f, "service_name", "(none)")] += 1

    # For quality analysis: if a caller uses multiple services,
    # they might be picking wrong ones. Calculate "primary service"
    # usage rate. Low primary rate = likely misclassification.
    rows = []
    for caller, data in sorted(callers.items()):
        if data["total"] < min_tickets:
            continue
        most_common = data["services"].most_common(1)
        primary_svc, primary_count = most_common[0] if most_common else ("-", 0)
        primary_rate = primary_count / data["total"] * 100
        other = data["total"] - primary_count

        if primary_rate < 50:
            flag = "systematically wrong"
        elif primary_rate < 70:
            flag = "often wrong"
        elif primary_rate < 90:
            flag = "sometimes wrong"
        else:
            flag = "OK"

        total_services = len(data["services"])
        rows.append([
            caller,
            str(data["total"]),
            str(total_services),
            primary_svc,
            f"{primary_rate:.0f}%",
            flag,
        ])

    if not rows:
        return f"No callers with >={min_tickets} tickets in the last {days} days."

    header = ["Caller", "Tickets", "Services Used", "Primary Service", "Primary Rate", "Assessment"]
    out = [f"**Caller Service Selection Quality** (last {days} days, >={min_tickets} tickets)", ""]
    out.append(_format_table(header, rows))
    return "\n".join(out)


@mcp.tool()
async def itop_agent_correction_rate(
    min_tickets: int = 10,
    days: int = 60,
    limit: int = 500,
) -> str:
    """Analyse which agents correct service assignments.

    Shows how often each agent handles tickets where the final service
    differs from what the caller initially selected. A high correction
    rate means the agent actively fixes classification; a low rate may
    mean they accept incorrect service selections.

    Args:
        min_tickets: Minimum tickets per agent to include (default: 10).
        days: Lookback period (default: 60).
        limit: Max tickets (default: 500).
    """
    s = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    e = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    result = await _itop_request({
        "operation": "core/get",
        "class": "UserRequest",
        "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date < '{e}'",
        "output_fields": "id,ref,title,service_name,service_id,caller_name,agent_name,status",
        "limit": str(limit),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return "No tickets found."

    # Heuristic: we can't directly detect if agent changed the service
    # without CMDBChange history. Instead we measure:
    # For each agent, how many services they handle across tickets.
    # Agents handling many different services = likely making corrections.
    # Agents with few services = sticking to their lane.
    # A better approach would need audit log - this is a proxy.
    agents: dict[str, dict] = {}
    for t in tickets:
        f = t["fields"]
        agent = _str_or(f, "agent_name") or "(unassigned)"
        if agent not in agents:
            agents[agent] = {"total": 0, "services": Counter(), "callers": set()}
        agents[agent]["total"] += 1
        agents[agent]["services"][_str_or(f, "service_name", "(none)")] += 1
        agents[agent]["callers"].add(_str_or(f, "caller_name", "?"))

    rows = []
    for agent, data in sorted(agents.items()):
        if data["total"] < min_tickets:
            continue
        # Primary service ratio
        most_common = data["services"].most_common(1)
        primary_count = most_common[0][1] if most_common else 0
        primary_rate = primary_count / data["total"] * 100
        total_services = len(data["services"])

        # More services per ticket = more correction
        correction_score = total_services / data["total"] * 100 if data["total"] > 0 else 0
        if correction_score > 50:
            flag = "corrects often (many different services)"
        elif correction_score > 30:
            flag = "corrects sometimes"
        else:
            flag = "rarely changes services"

        rows.append([
            agent,
            str(data["total"]),
            str(total_services),
            f"{primary_rate:.0f}%",
            f"{correction_score:.1f}",
            flag,
        ])

    if not rows:
        return f"No agents with >={min_tickets} tickets in the last {days} days."

    header = ["Agent", "Tickets", "Services", "Primary Rate", "Diversity Score", "Assessment"]
    out = [
        f"**Agent Service Correction Analysis** (last {days} days, >={min_tickets} tickets)",
        "Note: Diversity score = unique services / total tickets x 100. Higher = more correction.",
        "",
    ]
    out.append(_format_table(header, rows))
    return "\n".join(out)


@mcp.tool()
async def itop_ticket_summary(
    days: int = 30,
    limit: int = 500,
) -> str:
    """High-level ticket summary dashboard.

    Shows created, resolved, closed, open tickets with SLA stats
    and average resolution times.

    Args:
        days: Lookback period (default: 30).
        limit: Max tickets (default: 500).
    """
    s = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    e = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    result = await _itop_request({
        "operation": "core/get",
        "class": "UserRequest",
        "key": f"SELECT UserRequest WHERE start_date >= '{s}' AND start_date <= '{e}'",
        "output_fields": SLA_ANALYSIS_FIELDS,
        "limit": str(limit),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return "No tickets found."

    total = len(tickets)
    by_status = Counter()
    sla_breaches = 0
    resolution_times = []

    for t in tickets:
        f = t["fields"]
        by_status[_str_or(f, "status", "?")] += 1

        if _sla_is_breached(_str_or(f, "sla_tto_passed")) or _sla_is_breached(_str_or(f, "sla_ttr_passed")):
            sla_breaches += 1

        st = f.get("start_date")
        rt = f.get("resolution_date")
        if st and rt:
            try:
                diff = (datetime.fromisoformat(rt) - datetime.fromisoformat(st)).total_seconds()
                if diff > 0:
                    resolution_times.append(diff)
            except (ValueError, TypeError):
                pass

    status_lines = []
    for st, cnt in by_status.most_common():
        status_lines.append(f"  {st}: {cnt}")

    lines = [
        f"**Ticket Summary** (last {days} days)",
        "",
        f"Created:      {total}",
        f"Resolved:     {sum(cnt for st, cnt in by_status.items() if st in ('resolved', 'closed'))}",
        f"Still open:   {sum(cnt for st, cnt in by_status.items() if st not in ('resolved', 'closed'))}",
        "",
        "**By status:**",
        *status_lines,
        "",
        f"SLA breaches: {sla_breaches} ({sla_breaches/total*100:.1f}%)" if total else "",
    ]

    if resolution_times:
        resolution_times.sort()
        median = resolution_times[len(resolution_times) // 2]
        avg_res = sum(resolution_times) / len(resolution_times)
        lines.extend([
            f"Avg resolution: {_format_duration(avg_res)}",
            f"Median resolution: {_format_duration(median)}",
        ])

    return "\n".join(lines)


# ========================================================================
# KNOWLEDGE BASE TOOLS
# ========================================================================


# KB_CLASS and KB_CATEGORY_CLASS are auto-detected: try KBEntry/KBCategory
# first (standard), fall back to FAQ/FAQCategory (light module).
_KB_CLASS: str | None = None
_KB_CATEGORY_CLASS: str | None = None


async def _detect_kb_class() -> tuple[str, str]:
    """Detect available KB class (KBEntry or FAQ)."""
    global _KB_CLASS, _KB_CATEGORY_CLASS
    if _KB_CLASS is not None:
        return _KB_CLASS, _KB_CATEGORY_CLASS  # type: ignore

    for cls, cat_cls in [("KBEntry", "KBCategory"), ("FAQ", "FAQCategory")]:
        r = await _itop_request({
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
    """Return appropriate output_fields for the detected KB class."""
    if _KB_CLASS == "FAQ":
        return "id,title,summary,category_name,status"
    return "id,title,summary,category_name,status"


@mcp.tool()
async def itop_search_kb(
    query: str,
    limit: int = 20,
) -> str:
    """Search knowledge base articles by text in title or summary.

    Auto-detects KB class (KBEntry or FAQ).

    Args:
        query: Search text.
        limit: Max results (default: 20).
    """
    kb_cls, _ = await _detect_kb_class()
    if not kb_cls:
        return "No KB module installed (tried KBEntry, FAQ)."

    safe = query.replace("'", "\\'")
    if kb_cls == "FAQ":
        oql = f"SELECT FAQ WHERE title LIKE '%{safe}%' OR summary LIKE '%{safe}%'"
    else:
        oql = f"SELECT {kb_cls} WHERE title LIKE '%{safe}%' OR summary LIKE '%{safe}%'"

    result = await _itop_request({
        "operation": "core/get",
        "class": kb_cls,
        "key": oql,
        "output_fields": _get_kb_fields(),
        "limit": str(limit),
    })

    articles = _extract_objects(result)
    if not articles:
        return f"No KB articles found for query '{query}'."

    header = ["ID", "Title", "Category", "Status"]
    rows = []
    for a in articles:
        f = a["fields"]
        rows.append([
            str(a["key"]),
            _str_or(f, "title", "?")[:60],
            _str_or(f, "category_name", "-"),
            _str_or(f, "status", "?"),
        ])

    out = [f"**{kb_cls} Articles** matching '{query}':", ""]
    out.append(_format_table(header, rows))
    return "\n".join(out)


@mcp.tool()
async def itop_get_kb_article(article_id: int) -> str:
    """Get full knowledge base article by ID.

    Args:
        article_id: Article ID (KBEntry or FAQ).
    """
    kb_cls, _ = await _detect_kb_class()
    if not kb_cls:
        return "No KB module installed (tried KBEntry, FAQ)."

    result = await _itop_request({
        "operation": "core/get",
        "class": kb_cls,
        "key": f"SELECT {kb_cls} WHERE id={article_id}",
        "output_fields": "*+",
    })

    articles = _extract_objects(result)
    if not articles:
        return f"KB article #{article_id} not found."

    return _format_objects(result)


@mcp.tool()
async def itop_list_kb_categories() -> str:
    """List all knowledge base categories."""
    _, cat_cls = await _detect_kb_class()
    if not cat_cls:
        return "No KB module installed."

    result = await _itop_request({
        "operation": "core/get",
        "class": cat_cls,
        "key": f"SELECT {cat_cls}",
        "output_fields": "id,name,description",
        "limit": "100",
    })

    cats = _extract_objects(result)
    if not cats:
        return "No KB categories found."

    header = ["ID", "Name", "Description"]
    rows = []
    for c in cats:
        f = c["fields"]
        rows.append([
            str(c["key"]),
            _str_or(f, "name", "?"),
            _str_or(f, "description", "")[:60],
        ])

    out = ["**KB Categories:**", ""]
    out.append(_format_table(header, rows))
    return "\n".join(out)


# ========================================================================
# BASE CRUD TOOLS (from josephstreeter/mcp_itop)
# ========================================================================


@mcp.tool()
async def itop_get(
    obj_class: str,
    key: str,
    output_fields: str = "*",
    limit: int = 0,
    page: int = 0,
) -> str:
    """Search for objects in iTop.

    Args:
        obj_class: iTop class (e.g. Server, UserRequest, Person, Organization).
        key: OQL query (e.g. "SELECT Server WHERE name LIKE '%web%'"),
             numeric ID, or JSON criteria.
        output_fields: Comma-separated fields, or "*" for all, or "*+" for subclass fields.
        limit: Max results (0 = no limit).
        page: Page number (starts at 1).
    """
    op: dict = {
        "operation": "core/get",
        "class": obj_class,
        "key": _parse_key(key),
        "output_fields": output_fields,
    }
    if limit > 0:
        op["limit"] = str(limit)
        if page > 0:
            op["page"] = str(page)

    result = await _itop_request(op)
    return _format_objects(result)


@mcp.tool()
async def itop_create(
    obj_class: str,
    fields: str,
    output_fields: str = "id, friendlyname",
    comment: str = "",
) -> str:
    """Create a new object in iTop.

    Args:
        obj_class: iTop class (e.g. UserRequest, Server, Person).
        fields: JSON string of field values.
        output_fields: Comma-separated fields to return.
        comment: Optional comment for change tracking.
    """
    parsed = _parse_json_arg(fields, "fields")
    if isinstance(parsed, str):
        return parsed

    result = await _itop_request({
        "operation": "core/create",
        "class": obj_class,
        "fields": parsed,
        "output_fields": output_fields,
        "comment": comment or DEFAULT_COMMENT,
    })
    return _format_objects(result)


@mcp.tool()
async def itop_update(
    obj_class: str,
    key: str,
    fields: str,
    output_fields: str = "id, friendlyname",
    comment: str = "",
) -> str:
    """Update an existing object in iTop.

    Use this to modify fields on tickets, CI, etc.
    For lifecycle transitions (assign/resolve/close), use itop_apply_stimulus.

    Args:
        obj_class: iTop class.
        key: Object ID, OQL, or JSON criteria (must match exactly one).
        fields: JSON of fields to update.
        output_fields: Fields to return.
        comment: Optional comment for change tracking.
    """
    parsed = _parse_json_arg(fields, "fields")
    if isinstance(parsed, str):
        return parsed

    result = await _itop_request({
        "operation": "core/update",
        "class": obj_class,
        "key": _parse_key(key),
        "fields": parsed,
        "output_fields": output_fields,
        "comment": comment or DEFAULT_COMMENT,
    })
    return _format_objects(result)


@mcp.tool()
async def itop_delete(
    obj_class: str,
    key: str,
    comment: str = "",
    simulate: bool = True,
) -> str:
    """Delete object(s) from iTop.

    Args:
        obj_class: iTop class.
        key: Object ID, OQL, or JSON criteria.
        comment: Optional comment.
        simulate: If True, dry-run without deleting (default: True).
    """
    result = await _itop_request({
        "operation": "core/delete",
        "class": obj_class,
        "key": _parse_key(key),
        "simulate": simulate,
        "comment": comment or DEFAULT_COMMENT,
    })
    return _format_objects(result)


@mcp.tool()
async def itop_apply_stimulus(
    obj_class: str,
    key: str,
    stimulus: str,
    fields: str = "{}",
    output_fields: str = "id, friendlyname, status",
    comment: str = "",
) -> str:
    """Apply a lifecycle stimulus to an iTop object (ticket state transition).

    Common stimuli for UserRequest/Incident:
      - ev_assign:   assign to agent (fields={"agent_id": <id>, "team_id": <id>})
      - ev_reassign: reassign to another agent
      - ev_resolve:  resolve ticket (fields={"solution": "..."})
      - ev_close:    close ticket
      - ev_reopen:   reopen ticket
      - ev_pending:  put on hold (fields={"pending_reason": "..."})

    Args:
        obj_class: iTop class (e.g. UserRequest, Incident).
        key: Object ID (must match exactly one).
        stimulus: Stimulus code (e.g. ev_assign, ev_resolve).
        fields: JSON of fields required for the transition.
        output_fields: Fields to return.
        comment: Optional comment.
    """
    parsed = _parse_json_arg(fields, "fields")
    if isinstance(parsed, str):
        return parsed

    result = await _itop_request({
        "operation": "core/apply_stimulus",
        "class": obj_class,
        "key": _parse_key(key),
        "stimulus": stimulus,
        "fields": parsed,
        "output_fields": output_fields,
        "comment": comment or DEFAULT_COMMENT,
    })
    return _format_objects(result)


@mcp.tool()
async def itop_get_related(
    obj_class: str,
    key: str,
    relation: str = "impacts",
    depth: int = 4,
    direction: str = "down",
    redundancy: bool = True,
) -> str:
    """Find CIs related to a given object via impact/dependency relations.

    Args:
        obj_class: iTop class (e.g. Server, ApplicationSolution).
        key: Object ID or OQL.
        relation: "impacts" or "depends on".
        depth: Traversal depth (max 20).
        direction: "down" or "up".
        redundancy: Account for redundancy in impact analysis.
    """
    result = await _itop_request({
        "operation": "core/get_related",
        "class": obj_class,
        "key": _parse_key(key),
        "relation": relation,
        "depth": depth,
        "direction": direction,
        "redundancy": redundancy,
    })
    output = _format_objects(result)
    relations = result.get("relations")
    if relations:
        output += "\n\n--- Relations ---"
        for origin, targets in relations.items():
            for target in targets:
                output += f"\n  {origin} -> {_str_or(target, 'key', '?')}"
    return output


@mcp.tool()
async def itop_list_operations() -> str:
    """List all available REST/JSON operations on the iTop server."""
    result = await _itop_request({"operation": "list_operations"})
    if result.get("code", -1) != 0:
        return f"Error: {_str_or(result, 'message', 'Unknown error')}"
    ops = result.get("operations", [])
    lines = [f"Available operations ({len(ops)}):"]
    for op in ops:
        lines.append(f"  - {_str_or(op, 'verb', '?')}: {_str_or(op, 'description', '')} [{_str_or(op, 'extension', '')}]")
    return "\n".join(lines)


@mcp.tool()
async def itop_describe_class(obj_class: str) -> str:
    """Discover fields for an iTop class by sampling an existing object.

    Args:
        obj_class: iTop class name (e.g. Server, UserRequest, Person).
    """
    result = await _itop_request({
        "operation": "core/get",
        "class": obj_class,
        "key": f"SELECT {obj_class}",
        "output_fields": "*",
        "limit": "1",
    })

    if result.get("code", -1) != 0:
        return f"Error (code {result.get('code')}): {_str_or(result, 'message', 'Unknown error')}"

    objects = result.get("objects") or {}
    if not objects:
        return (
            f"Class '{obj_class}' has zero instances - cannot sample fields.\n"
            f"Create a test object first with minimal fields; iTop will report missing required fields."
        )

    _obj_key, obj_data = next(iter(objects.items()))
    fields = obj_data.get("fields", {}) or {}

    lines = [f"Class {obj_class} - attributes sampled from {_obj_key}:"]
    for name in sorted(fields.keys()):
        value = fields[name]
        if isinstance(value, list):
            kind = f"list[{len(value)}]"
        elif isinstance(value, dict):
            kind = "object"
        elif value is None or value == "":
            kind = "scalar (empty)"
        else:
            kind = f"scalar (e.g. {str(value)[:50]})"
        lines.append(f"  - {name}: {kind}")

    lines.append("\nNote: this is best-effort, not authoritative schema. Missing attributes may still be valid.")
    return "\n".join(lines)


# ========================================================================
# COMMENT TOOLS
# ========================================================================


@mcp.tool()
async def itop_add_comment(
    ticket_class: str,
    ticket_id: int,
    text: str,
    is_public: bool = True,
    format: str = "text",
) -> str:
    """Add a comment to a ticket (public or private log).

    Public comments are visible to end users on the portal.
    Private comments are visible only to agents.

    Args:
        ticket_class: Ticket class (UserRequest, Incident, Problem).
        ticket_id: Ticket ID number.
        text: Comment text.
        is_public: True = public_log, False = private_log.
        format: "text" or "html" (default: text).
    """
    log_field = "public_log" if is_public else "private_log"

    result = await _itop_request({
        "operation": "core/update",
        "class": ticket_class,
        "key": ticket_id,
        "fields": {
            log_field: {
                "add_item": {
                    "message": text,
                    "format": format,
                }
            }
        },
        "output_fields": "id, ref, friendlyname",
        "comment": f"MCP: added {'public' if is_public else 'private'} comment",
    })
    return _format_objects(result)


@mcp.tool()
async def itop_get_log(
    ticket_class: str,
    ticket_id: int,
    log_type: str = "both",
) -> str:
    """Read log entries (comments) from a ticket.

    Args:
        ticket_class: Ticket class (UserRequest, Incident, Problem).
        ticket_id: Ticket ID number.
        log_type: "public", "private", or "both" (default: both).
    """
    fields = []
    if log_type in ("public", "both"):
        fields.append("public_log")
    if log_type in ("private", "both"):
        fields.append("private_log")

    result = await _itop_request({
        "operation": "core/get",
        "class": ticket_class,
        "key": f"SELECT {ticket_class} WHERE id={ticket_id}",
        "output_fields": ",".join(fields),
    })

    tickets = _extract_objects(result)
    if not tickets:
        return f"Ticket #{ticket_id} ({ticket_class}) not found."

    f = tickets[0]["fields"]
    lines = [f"**Logs for {ticket_class} #{ticket_id}**", ""]

    for field in fields:
        if field not in f:
            continue
        lines.append(f"--- {'Public Log' if field == 'public_log' else 'Private Log'} ---")
        log_data = f[field]
        if isinstance(log_data, dict):
            # iTop 3.2.1 uses 'entries', older versions use 'items'
            items = log_data.get("entries") or log_data.get("items") or []
            if not items:
                lines.append("(empty)")
            for item in items:
                date = item.get("date", "?")[:19]
                user = item.get("user_login", "?")
                msg = item.get("message", "")
                # Strip HTML tags for readability
                msg = re.sub(r'<[^>]+>', '', msg)
                lines.append(f"[{date}] {user}: {msg[:200]}")
        elif isinstance(log_data, str):
            lines.append(log_data[:500])
        else:
            lines.append("(no entries)")
        lines.append("")

    return "\n".join(lines)


# ========================================================================
# UTILITY
# ========================================================================


def _format_duration(seconds: float) -> str:
    """Format seconds to human-readable duration."""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}min"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}min"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"


# ========================================================================

def main():
    """Run the iTop MCP server.

    Runs as a network-reachable Streamable HTTP server. iTop
    authentication is supplied per-client via an "Authorization: Bearer
    <itop_token>" header (see ItopBearerVerifier / _get_bearer_token) -
    no ITOP_TOKEN / ITOP_USER / ITOP_PASSWORD environment variables are
    read for authentication purposes anymore.
    """
    if not ITOP_URL:
        print("Error: ITOP_URL is not set.", file=sys.stderr)
        print("Create .env file with ITOP_URL (see .env.example)", file=sys.stderr)
        sys.exit(1)

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8096"))
    mcp.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
